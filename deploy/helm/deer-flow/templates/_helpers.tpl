{{/*
Common helpers for the ActWeave chart.
*/}}

{{- define "deer-flow.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "deer-flow.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "deer-flow.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "deer-flow.labels" -}}
helm.sh/chart: {{ include "deer-flow.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "deer-flow.selectorLabels" -}}
app.kubernetes.io/name: {{ include "deer-flow.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "deer-flow.namespace" -}}
{{- default .Release.Namespace .Values.namespace -}}
{{- end -}}

{{- define "deer-flow.imagePullSecrets" -}}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end -}}

{{/* Fully-qualified image refs for the three ActWeave images.
     When `image.registry` is empty, omit the prefix so the ref is
     `deer-flow-gateway:latest` (local-image mode, imagePullPolicy: Never). */}}
{{- define "deer-flow.gatewayImage" -}}
{{- if .Values.image.registry -}}{{- printf "%s/%s:%s" .Values.image.registry .Values.image.gatewayImage .Values.image.tag -}}
{{- else -}}{{- printf "%s:%s" .Values.image.gatewayImage .Values.image.tag -}}{{- end -}}
{{- end -}}

{{- define "deer-flow.frontendImage" -}}
{{- if .Values.image.registry -}}{{- printf "%s/%s:%s" .Values.image.registry .Values.image.frontendImage .Values.image.tag -}}
{{- else -}}{{- printf "%s:%s" .Values.image.frontendImage .Values.image.tag -}}{{- end -}}
{{- end -}}

{{- define "deer-flow.provisionerImage" -}}
{{- if .Values.image.registry -}}{{- printf "%s/%s:%s" .Values.image.registry .Values.image.provisionerImage .Values.image.tag -}}
{{- else -}}{{- printf "%s:%s" .Values.image.provisionerImage .Values.image.tag -}}{{- end -}}
{{- end -}}

{{- define "deer-flow.nginxImage" -}}
{{- printf "%s:%s" .Values.nginx.image.repository .Values.nginx.image.tag -}}
{{- end -}}

{{/* PVC name for the .deer-flow home directory. */}}
{{- define "deer-flow.homePVC" -}}
{{- printf "%s-home" (include "deer-flow.fullname" .) -}}
{{- end -}}

{{/* Name of the Secret holding generated app secrets (auth token, better-auth). */}}
{{- define "deer-flow.appSecret" -}}
{{- if .Values.existingAppSecret -}}{{- .Values.existingAppSecret -}}
{{- else -}}{{- printf "%s-app" (include "deer-flow.fullname" .) -}}{{- end -}}
{{- end -}}

{{/* Name of the postgres StatefulSet/Service. */}}
{{- define "deer-flow.postgresFullname" -}}
{{- printf "%s-postgres" (include "deer-flow.fullname" .) -}}
{{- end -}}

{{/* Name of the Secret holding DATABASE_URL (and, in bundled mode, the
     postgres superuser password). Resolution order:
       1. postgresql.external.existingSecret (user-managed, key=database-url)
       2. postgresql.existingSecret          (user-managed, bundled image)
       3. chart-managed secret `<release>-postgres`
     Only #3 is created by this chart; #1/#2 must exist already. */}}
{{- define "deer-flow.databaseUrlSecret" -}}
{{- if .Values.postgresql.external.existingSecret -}}{{- .Values.postgresql.external.existingSecret -}}
{{- else if .Values.postgresql.existingSecret -}}{{- .Values.postgresql.existingSecret -}}
{{- else -}}{{- include "deer-flow.postgresFullname" . -}}{{- end -}}
{{- end -}}

{{/* SHA256 checksums of the ConfigMaps. Mount these as pod-template
     annotations: ConfigMaps mounted via subPath do NOT receive live updates,
     so a `helm upgrade` that only changes a ConfigMap would leave pods on stale
     config. A checksum annotation makes any content change alter the pod spec,
     which triggers a rolling restart. */}}
{{- define "deer-flow.configChecksum" -}}
{{- include (print $.Template.BasePath "/configmap-config.yaml") . | sha256sum -}}
{{- end -}}

{{- define "deer-flow.nginxChecksum" -}}
{{- include (print $.Template.BasePath "/configmap-nginx.yaml") . | sha256sum -}}
{{- end -}}

{{/*
Environment shared by the three backend process roles.  The values live in
separate keys even when the chart generates them in one Secret: JWT signing,
audit correlation, credential encryption, internal calls, proxy attestation,
and Provisioner control are independent trust domains and must never reuse key
material.

This is a closed platform-secret contract. Model definitions and provider
Credentials are PostgreSQL-backed and must not be broadcast through envFrom.
*/}}
{{- define "deer-flow.backendSecretEnv" -}}
- name: AUTH_JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: AUTH_JWT_SECRET
- name: DEER_FLOW_AUDIT_ACTIVE_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: DEER_FLOW_AUDIT_ACTIVE_KEY_ID
- name: DEER_FLOW_AUDIT_KEYRING_JSON
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: DEER_FLOW_AUDIT_KEYRING_JSON
- name: DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID
- name: DEER_FLOW_CREDENTIAL_KEYRING_JSON
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: DEER_FLOW_CREDENTIAL_KEYRING_JSON
- name: DEER_FLOW_INTERNAL_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: DEER_FLOW_INTERNAL_AUTH_TOKEN
- name: DEER_FLOW_PROXY_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: DEER_FLOW_PROXY_AUTH_TOKEN
- name: PROVISIONER_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.appSecret" . }}
      key: PROVISIONER_API_KEY
{{- end -}}

{{- define "deer-flow.backendDatabaseEnv" -}}
{{- $pgConfigured := or .Values.postgresql.enabled .Values.postgresql.external.databaseUrl .Values.postgresql.external.existingSecret .Values.postgresql.existingSecret -}}
{{- if $pgConfigured }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "deer-flow.databaseUrlSecret" . }}
      key: database-url
{{- end }}
{{- end -}}

{{/* Percent-encode a string for safe interpolation into a URL userinfo
     (password) segment of a DSN. Sprig lacks urlqueryescape, and
     regexReplaceAllLiteral treats `replacement` as a regex template so chars
     like `[`, `]`, `?` break it - so we chain plain `replace` calls instead.
     `%` is encoded first to avoid double-encoding the percent signs emitted
     for the other characters. Covers the URL-special chars a managed-DB
     password might contain (`@ : / # ? % [ ]` and space). */}}
{{- define "deer-flow.urlEscape" -}}
{{- $s := . -}}
{{- $s = replace "%" "%25" $s -}}
{{- $s = replace "@" "%40" $s -}}
{{- $s = replace ":" "%3A" $s -}}
{{- $s = replace "/" "%2F" $s -}}
{{- $s = replace "#" "%23" $s -}}
{{- $s = replace "?" "%3F" $s -}}
{{- $s = replace "[" "%5B" $s -}}
{{- $s = replace "]" "%5D" $s -}}
{{- $s = replace " " "%20" $s -}}
{{- $s -}}
{{- end -}}
