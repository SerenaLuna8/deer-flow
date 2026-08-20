"use client";

import Link from "next/link";
import { useId } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type {
  SkillCredentialMappingStatus,
  SkillCredentialRequirement,
} from "@/core/shared-assets";

type EligibleCredential =
  SkillCredentialRequirement["eligible_credentials"][number];

export function skillCredentialMappingStatusLabel(
  status: SkillCredentialMappingStatus,
  labels: Record<SkillCredentialMappingStatus, string>,
): string {
  return labels[status];
}

export function SkillCredentialOptionSelect({
  name,
  optional,
  mappingStatus,
  options,
  credentialVersionId,
  sourceEnvFieldName,
  disabled = false,
  error = false,
  allowEmpty = true,
  onCredentialChange,
  onSourceEnvFieldChange,
  manageHref,
}: {
  name: string;
  optional: boolean;
  mappingStatus: SkillCredentialMappingStatus;
  options: readonly EligibleCredential[];
  credentialVersionId: string;
  sourceEnvFieldName: string;
  disabled?: boolean;
  error?: boolean;
  allowEmpty?: boolean;
  onCredentialChange: (credentialVersionId: string) => void;
  onSourceEnvFieldChange: (sourceEnvFieldName: string) => void;
  manageHref?: string;
}) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const errorId = useId();
  const selectedCredential = options.find(
    (credential) => credential.credential_version_id === credentialVersionId,
  );
  const sourceFields = selectedCredential?.env_fields ?? [];
  const sourceStillAvailable = sourceFields.includes(sourceEnvFieldName);
  const displayedSourceFields =
    sourceEnvFieldName && !sourceStillAvailable
      ? [sourceEnvFieldName, ...sourceFields]
      : sourceFields;
  const statusLabel = skillCredentialMappingStatusLabel(mappingStatus, {
    configured: copy.mappingStatusConfigured,
    missing: copy.mappingStatusMissing,
    invalid: copy.mappingStatusInvalid,
  });

  return (
    <div className="space-y-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 text-sm font-medium break-all">
          {name}
        </code>
        <Badge variant="secondary">
          {optional ? copy.optional : copy.required}
        </Badge>
        <Badge
          variant={mappingStatus === "configured" ? "default" : "secondary"}
        >
          {statusLabel}
        </Badge>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="grid gap-1">
          <select
            aria-label={`${copy.credentialLabel} · ${name}`}
            aria-invalid={error || undefined}
            aria-describedby={error ? errorId : undefined}
            className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
            value={credentialVersionId}
            disabled={disabled}
            onChange={(event) => onCredentialChange(event.target.value)}
          >
            <option value="" disabled={!allowEmpty}>
              {options.length === 0
                ? copy.noCompatibleCredential
                : optional
                  ? copy.optionalUnbound
                  : copy.selectCredential}
            </option>
            {credentialVersionId && !selectedCredential ? (
              <option value={credentialVersionId} disabled>
                {copy.credentialUnavailable}
              </option>
            ) : null}
            {options.map((credential) => (
              <option
                key={credential.credential_version_id}
                value={credential.credential_version_id}
              >
                {copy.credentialVersion(
                  credential.display_name,
                  credential.version_number,
                )}
              </option>
            ))}
          </select>
        </div>

        <div className="grid gap-1">
          <select
            aria-label={`${copy.sourceFieldLabel} · ${name}`}
            aria-invalid={error || undefined}
            aria-describedby={error ? errorId : undefined}
            className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
            value={sourceEnvFieldName}
            disabled={disabled || credentialVersionId === ""}
            onChange={(event) => onSourceEnvFieldChange(event.target.value)}
          >
            <option value="">
              {credentialVersionId === ""
                ? copy.selectCredentialFirst
                : copy.selectSourceField}
            </option>
            {displayedSourceFields.map((fieldName) => (
              <option key={fieldName} value={fieldName}>
                env.{fieldName}
                {fieldName === sourceEnvFieldName && !sourceStillAvailable
                  ? ` · ${copy.sourceFieldUnavailable}`
                  : ""}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error ? (
        <p id={errorId} role="alert" className="text-destructive text-xs">
          {mappingStatus === "invalid" && sourceEnvFieldName
            ? copy.invalidMapping
            : credentialVersionId
              ? copy.sourceFieldRequired
              : copy.requiredMissing}
        </p>
      ) : null}
      {options.length === 0 && manageHref ? (
        <Button asChild type="button" size="sm" variant="ghost">
          <Link href={manageHref}>{copy.manageCredential}</Link>
        </Button>
      ) : null}
    </div>
  );
}

export function skillCredentialRequirementOptions(
  requirement: SkillCredentialRequirement,
  selectedVersionId: string,
): EligibleCredential[] {
  const options = [...requirement.eligible_credentials];
  if (
    selectedVersionId !== "" &&
    !options.some(
      (credential) => credential.credential_version_id === selectedVersionId,
    ) &&
    requirement.configured &&
    requirement.credential_id !== null &&
    requirement.credential_version_id === selectedVersionId &&
    requirement.credential_display_name !== null &&
    requirement.credential_version_number !== null
  ) {
    options.unshift({
      credential_id: requirement.credential_id,
      credential_version_id: requirement.credential_version_id,
      display_name: requirement.credential_display_name,
      version_number: requirement.credential_version_number,
      env_fields: [],
    });
  }
  return options;
}
