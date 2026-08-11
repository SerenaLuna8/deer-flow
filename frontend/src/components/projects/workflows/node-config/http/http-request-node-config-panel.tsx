"use client";

import { useState } from "react";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import { useWorkflowWorkbenchStore } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import {
  BasicJsonEditor,
  BasicPanelField,
  BasicPanelShell,
  BasicRestrictedTemplateEditor,
} from "../basic/shared";

import {
  HttpExecutionPolicyEditor,
  HttpKeyValueRowsEditor,
  HttpRestrictedJsonTemplateEditor,
  httpRecord,
  httpString,
  isHttpRecord,
} from "./http-config-editors";
import { HttpCurlImportDialog } from "./http-curl-import-dialog";
import {
  buildHttpRequestNodeConfigUpdate,
  httpConfigRecord,
  httpCredentialSlotIds,
  httpCredentialSlotState,
  httpMethodIsWrite,
  httpRequestConfigIssues,
  safeHttpMethod,
  selectHttpEndpointConfig,
  selectHttpInjectionProfileAuth,
} from "./http-node-config-helpers";

const SAFE_PROFILE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const UNAVAILABLE_ENDPOINT_VALUE = "__current_unavailable_endpoint__";
const UNAVAILABLE_METHOD_VALUE = "__current_unavailable_method__";
const UNAVAILABLE_PROFILE_VALUE = "__current_unavailable_profile__";

const positiveLimit = (value: unknown): number | null =>
  typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? value
    : null;

const bodyKind = (
  body: Record<string, unknown>,
): "none" | "json" | "form_urlencoded" | "multipart_text" | "raw_text" =>
  ["none", "json", "form_urlencoded", "multipart_text", "raw_text"].includes(
    httpString(body.kind),
  )
    ? (body.kind as
        | "none"
        | "json"
        | "form_urlencoded"
        | "multipart_text"
        | "raw_text")
    : "none";

const newBody = (kind: ReturnType<typeof bodyKind>) => {
  if (kind === "json") {
    return {
      kind,
      template: { version: 1, template: {}, bindings: {} },
    };
  }
  if (kind === "form_urlencoded" || kind === "multipart_text") {
    return { kind, fields: [] };
  }
  if (kind === "raw_text") {
    return {
      kind,
      content_type: "text/plain",
      template: { version: 1, segments: [] },
    };
  }
  return { kind: "none" };
};

const acceptedStatusRows = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value) ? value.filter(isHttpRecord) : [];

const timeoutInputValue = (value: unknown): number | "" =>
  typeof value === "number" && Number.isFinite(value) ? value : "";

export function HttpRequestNodeConfigPanel(
  props: WorkflowNodeConfigPanelProps,
) {
  const store = useWorkflowWorkbenchStore();
  const [curlOpen, setCurlOpen] = useState(false);
  const config = httpConfigRecord(props.node);
  const method = safeHttpMethod(config.method);
  const canWrite = props.capabilities.includes("workflow.http.write");
  const httpAuthoring = props.catalogEntry.http_authoring ?? null;
  const endpoints = httpAuthoring?.endpoints ?? [];
  const currentOrigin = httpString(config.base_origin);
  const exactOriginEndpoints = endpoints.filter(
    (endpoint) => endpoint.origin === currentOrigin,
  );
  const selectedEndpoint =
    exactOriginEndpoints.find((endpoint) =>
      endpoint.allowed_methods.includes(method),
    ) ??
    (exactOriginEndpoints.length === 1 ? exactOriginEndpoints[0] : undefined);
  const selectableMethods =
    selectedEndpoint?.allowed_methods.filter(
      (candidate) => canWrite || !httpMethodIsWrite(candidate),
    ) ?? [];
  const methodAvailable = selectableMethods.includes(method);
  const publicLimits = props.catalogEntry.public_limits;
  const maxTimeoutMs = positiveLimit(publicLimits?.max_timeout_ms);
  const maxRequestBytes = positiveLimit(publicLimits?.max_http_request_bytes);
  const maxResponseBytes = positiveLimit(publicLimits?.max_http_response_bytes);
  const limitsReady =
    maxTimeoutMs !== null &&
    maxRequestBytes !== null &&
    maxResponseBytes !== null;
  const authoringReady = httpAuthoring !== null && endpoints.length > 0;
  const locked =
    props.disabled ||
    props.readOnly ||
    !props.capabilities.includes("workflow.edit") ||
    !props.capabilities.includes("workflow.http.use") ||
    props.catalogEntry.availability.state !== "enabled" ||
    !limitsReady ||
    !authoringReady;
  const issues = httpRequestConfigIssues(config, canWrite, maxTimeoutMs);
  const executionPolicy = httpRecord(props.node.execution_policy);
  const retry = httpRecord(executionPolicy.retry);
  if (httpMethodIsWrite(method) && retry.mode === "bounded") {
    issues.unshift("写方法的 retry 必须为 none；当前 Draft 不能发布。");
  }
  if (!limitsReady) {
    issues.unshift(
      "HTTP public limits 不完整，Inspector 已 fail closed 为只读。",
    );
  }
  if (!authoringReady) {
    issues.unshift(
      "HTTP authoring authority 不可用，Inspector 已 fail closed 为只读。",
    );
  } else if (selectedEndpoint === undefined) {
    issues.unshift(
      "当前 endpoint 不在 effective HTTP authoring authority 中；保留为只读不可用值，请选择批准的 endpoint。",
    );
  } else if (!methodAvailable) {
    issues.unshift(
      "当前 method 不在所选 endpoint 的可用方法中；保留为只读不可用值。",
    );
  }

  const commit = (patch: Record<string, unknown>) => {
    if (locked) return;
    store.dispatch(buildHttpRequestNodeConfigUpdate(props.node, patch));
  };
  const auth = httpRecord(config.auth);
  const authMode =
    auth.mode === "endpoint_profile" ? "endpoint_profile" : "none";
  const rawProfileId = httpString(auth.injection_profile_id);
  const profileId = SAFE_PROFILE_ID.test(rawProfileId) ? rawProfileId : "";
  const injectionProfiles = selectedEndpoint?.injection_profiles ?? [];
  const selectedProfile = injectionProfiles.find(
    (profile) => profile.id === profileId,
  );
  const selectedSlotId = httpString(auth.credential_slot_id);
  const slotIds =
    selectedProfile === undefined
      ? []
      : httpCredentialSlotIds(
          props.document,
          selectedProfile.credential_payload_contract,
        );
  const profilesWithCompatibleSlots = injectionProfiles.filter(
    (profile) =>
      httpCredentialSlotIds(props.document, profile.credential_payload_contract)
        .length > 0,
  );
  if (authMode === "endpoint_profile" && selectedProfile === undefined) {
    issues.unshift(
      "当前 injection profile 不属于所选 endpoint；保留为只读不可用值。",
    );
  }
  const slotState = httpCredentialSlotState(
    auth,
    props.document,
    selectedProfile ?? null,
  );
  if (slotState === "missing") {
    issues.unshift(
      "Endpoint profile 引用的 Credential slot 声明缺失或 payload contract 不兼容。",
    );
  }
  const body = httpRecord(config.body);
  const selectedBodyKind = bodyKind(body);
  const timeout = httpRecord(config.timeout);
  const response = httpRecord(config.response);
  const responseMode = response.mode === "text" ? "text" : "json";
  const statuses = acceptedStatusRows(response.accepted_statuses);
  const sanitizedConfig = buildHttpRequestNodeConfigUpdate(
    props.node,
    {},
  ).config;

  const updateResponse = (patch: Record<string, unknown>) =>
    commit({ response: { ...response, ...patch } });
  const updateStatus = (index: number, patch: Record<string, unknown>) =>
    updateResponse({
      accepted_statuses: statuses.map((status, statusIndex) =>
        statusIndex === index
          ? {
              from: typeof patch.from === "number" ? patch.from : status.from,
              to: typeof patch.to === "number" ? patch.to : status.to,
            }
          : { from: status.from, to: status.to },
      ),
    });

  return (
    <>
      <BasicPanelShell
        disabled={locked && !props.readOnly}
        issues={issues}
        readOnly={props.readOnly}
        title="HTTP 请求配置"
      >
        <div className="border-border bg-muted/30 space-y-1 rounded-md border p-3 text-xs">
          <p>TLS 证书验证：始终开启</p>
          <p>Redirect、Cookie jar、ambient proxy：始终关闭</p>
          <p>
            请求发出后结果不可判定将进入 side_effect_unknown，不会作为普通 error
            或 cancel。
          </p>
        </div>
        <Button
          onClick={() => setCurlOpen(true)}
          type="button"
          variant="outline"
        >
          导入 cURL
        </Button>

        <section aria-label="HTTP endpoint" className="space-y-3">
          <h4 className="text-sm font-medium">Endpoint</h4>
          <BasicPanelField label="Method">
            <select
              aria-label="HTTP method"
              className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
              disabled={selectedEndpoint === undefined}
              onChange={(event) => {
                const nextMethod = safeHttpMethod(event.currentTarget.value);
                if (
                  selectedEndpoint === undefined ||
                  !selectedEndpoint.allowed_methods.includes(nextMethod) ||
                  (httpMethodIsWrite(nextMethod) && !canWrite)
                ) {
                  return;
                }
                commit({
                  method: nextMethod,
                  ...(nextMethod === "GET" || nextMethod === "HEAD"
                    ? { body: { kind: "none" } }
                    : {}),
                });
              }}
              value={methodAvailable ? method : UNAVAILABLE_METHOD_VALUE}
            >
              {!methodAvailable ? (
                <option disabled value={UNAVAILABLE_METHOD_VALUE}>
                  {method}（当前值不可用）
                </option>
              ) : null}
              {selectedEndpoint?.allowed_methods.map((allowedMethod) => (
                <option
                  disabled={httpMethodIsWrite(allowedMethod) && !canWrite}
                  key={allowedMethod}
                  value={allowedMethod}
                >
                  {allowedMethod}
                </option>
              ))}
            </select>
          </BasicPanelField>
          {!canWrite ? (
            <p className="text-muted-foreground text-xs">
              缺少 workflow.http.write；POST/PUT/PATCH/DELETE 已禁用。
            </p>
          ) : null}
          <BasicPanelField
            help="只从当前 effective locked policy 的安全 Catalog 投影选择；选择后写入 canonical base_origin。"
            label="Endpoint policy"
          >
            <select
              aria-label="HTTP endpoint policy"
              className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
              onChange={(event) => {
                const endpoint = endpoints.find(
                  (candidate) => candidate.id === event.currentTarget.value,
                );
                if (endpoint === undefined) return;
                const patch = selectHttpEndpointConfig(
                  config,
                  endpoint,
                  canWrite,
                );
                if (patch !== null) commit(patch);
              }}
              value={
                selectedEndpoint?.id ??
                (currentOrigin ? UNAVAILABLE_ENDPOINT_VALUE : "")
              }
            >
              <option value="">选择批准的 endpoint</option>
              {selectedEndpoint === undefined && currentOrigin ? (
                <option disabled value={UNAVAILABLE_ENDPOINT_VALUE}>
                  {currentOrigin}（当前值不可用）
                </option>
              ) : null}
              {endpoints.map((endpoint) => (
                <option
                  disabled={endpoint.allowed_methods.every(
                    (candidate) => httpMethodIsWrite(candidate) && !canWrite,
                  )}
                  key={endpoint.id}
                  value={endpoint.id}
                >
                  {endpoint.origin}
                </option>
              ))}
            </select>
          </BasicPanelField>
          {selectedEndpoint ? (
            <p className="text-muted-foreground text-xs">
              写请求幂等：
              {selectedEndpoint.write_idempotency === "server_derived_key"
                ? "服务端派生键"
                : "不支持"}
            </p>
          ) : null}
          <BasicRestrictedTemplateEditor
            label="Path 模板"
            onChange={(pathTemplate) => commit({ path_template: pathTemplate })}
            value={config.path_template}
          />
        </section>

        <HttpKeyValueRowsEditor
          kind="query"
          label="Query"
          onChange={(query) => commit({ query })}
          value={config.query}
        />
        <HttpKeyValueRowsEditor
          kind="header"
          label="Headers"
          onChange={(headers) => commit({ headers })}
          value={config.headers}
        />

        <section aria-label="HTTP auth" className="space-y-3">
          <h4 className="text-sm font-medium">认证与 Credential slot</h4>
          <BasicPanelField label="Auth mode">
            <select
              aria-label="HTTP auth mode"
              className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
              onChange={(event) =>
                event.currentTarget.value === "endpoint_profile"
                  ? (() => {
                      const nextProfile =
                        selectedProfile !== undefined &&
                        profilesWithCompatibleSlots.includes(selectedProfile)
                          ? selectedProfile
                          : profilesWithCompatibleSlots[0];
                      if (nextProfile === undefined) return;
                      const nextAuth = selectHttpInjectionProfileAuth(
                        nextProfile.id,
                        selectedSlotId,
                        injectionProfiles,
                        props.document,
                      );
                      if (nextAuth !== null) commit({ auth: nextAuth });
                    })()
                  : commit({ auth: { mode: "none" } })
              }
              value={authMode}
            >
              <option value="none">无认证</option>
              <option
                disabled={profilesWithCompatibleSlots.length === 0}
                value="endpoint_profile"
              >
                Endpoint profile
              </option>
            </select>
          </BasicPanelField>
          {authMode === "none" ? (
            <p className="text-muted-foreground text-xs">
              认证 profile 只能从所选 endpoint 的安全 Catalog 投影选择；禁止手工
              profile ID。
            </p>
          ) : null}
          {authMode === "endpoint_profile" ? (
            <>
              <BasicPanelField
                help="只从所选 endpoint 允许的 profile 选择；当前 unavailable 值只读保留。"
                label="Injection profile"
              >
                <select
                  aria-label="HTTP injection profile"
                  className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
                  disabled={selectedEndpoint === undefined}
                  onChange={(event) => {
                    const nextAuth = selectHttpInjectionProfileAuth(
                      event.currentTarget.value,
                      selectedSlotId,
                      injectionProfiles,
                      props.document,
                    );
                    if (nextAuth !== null) commit({ auth: nextAuth });
                  }}
                  value={
                    selectedProfile?.id ??
                    (profileId ? UNAVAILABLE_PROFILE_VALUE : "")
                  }
                >
                  <option value="">选择批准的 injection profile</option>
                  {selectedProfile === undefined && profileId ? (
                    <option disabled value={UNAVAILABLE_PROFILE_VALUE}>
                      {profileId}（当前值不可用）
                    </option>
                  ) : null}
                  {injectionProfiles.map((profile) => (
                    <option
                      disabled={
                        httpCredentialSlotIds(
                          props.document,
                          profile.credential_payload_contract,
                        ).length === 0
                      }
                      key={profile.id}
                      value={profile.id}
                    >
                      {profile.id}
                      {httpCredentialSlotIds(
                        props.document,
                        profile.credential_payload_contract,
                      ).length === 0
                        ? "（无兼容 slot）"
                        : ""}
                    </option>
                  ))}
                </select>
              </BasicPanelField>
              {selectedProfile ? (
                <p className="text-muted-foreground text-xs">
                  {selectedProfile.scheme} · {selectedProfile.target_header} ·{" "}
                  {selectedProfile.credential_payload_contract}
                </p>
              ) : null}
              <BasicPanelField label="Credential slot declaration">
                <select
                  aria-label="HTTP credential slot"
                  className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
                  disabled={selectedProfile === undefined}
                  onChange={(event) =>
                    selectedProfile === undefined
                      ? undefined
                      : (() => {
                          const nextAuth = selectHttpInjectionProfileAuth(
                            selectedProfile.id,
                            event.currentTarget.value,
                            injectionProfiles,
                            props.document,
                          );
                          if (nextAuth !== null) commit({ auth: nextAuth });
                        })()
                  }
                  value={
                    slotIds.includes(selectedSlotId)
                      ? selectedSlotId
                      : selectedSlotId
                        ? "__current_unavailable_slot__"
                        : ""
                  }
                >
                  <option value="">选择已声明的 opaque slot ID</option>
                  {!slotIds.includes(selectedSlotId) && selectedSlotId ? (
                    <option disabled value="__current_unavailable_slot__">
                      {selectedSlotId}（当前值不可用）
                    </option>
                  ) : null}
                  {slotIds.map((slotId) => (
                    <option key={slotId} value={slotId}>
                      {slotId}
                    </option>
                  ))}
                </select>
              </BasicPanelField>
              <p
                className={
                  slotState === "declared"
                    ? "text-muted-foreground text-xs"
                    : "text-destructive text-xs"
                }
                role="status"
              >
                {slotState === "declared"
                  ? "Slot 声明就绪 · required=true"
                  : "Slot 声明缺失或 payload contract 不兼容"}
              </p>
              <p className="text-muted-foreground text-xs">
                Grant 由 Published Version 独立授权；Draft/Spec/history 不保存
                Credential、version、grant 或 secret。payload schema checksum 由
                Gateway canonical 计算。
              </p>
            </>
          ) : (
            <p className="text-muted-foreground text-xs">
              当前请求不需要 Credential slot。
            </p>
          )}
        </section>

        <section aria-label="HTTP body" className="space-y-3">
          <h4 className="text-sm font-medium">Body</h4>
          <BasicPanelField label="Body 类型">
            <select
              aria-label="HTTP body type"
              className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
              onChange={(event) =>
                commit({
                  body: newBody(
                    event.currentTarget.value as ReturnType<typeof bodyKind>,
                  ),
                })
              }
              value={selectedBodyKind}
            >
              <option value="none">none</option>
              <option
                disabled={method === "GET" || method === "HEAD"}
                value="json"
              >
                json
              </option>
              <option
                disabled={method === "GET" || method === "HEAD"}
                value="form_urlencoded"
              >
                form-urlencoded
              </option>
              <option
                disabled={method === "GET" || method === "HEAD"}
                value="multipart_text"
              >
                multipart-text（无文件）
              </option>
              <option
                disabled={method === "GET" || method === "HEAD"}
                value="raw_text"
              >
                raw-text
              </option>
            </select>
          </BasicPanelField>
          {selectedBodyKind === "json" ? (
            <HttpRestrictedJsonTemplateEditor
              label="JSON Body 模板"
              onChange={(template) =>
                commit({ body: { kind: "json", template } })
              }
              value={body.template}
            />
          ) : null}
          {selectedBodyKind === "form_urlencoded" ||
          selectedBodyKind === "multipart_text" ? (
            <HttpKeyValueRowsEditor
              kind="form"
              label={
                selectedBodyKind === "multipart_text"
                  ? "Multipart 文本字段"
                  : "Form 字段"
              }
              onChange={(fields) =>
                commit({ body: { kind: selectedBodyKind, fields } })
              }
              value={body.fields}
            />
          ) : null}
          {selectedBodyKind === "raw_text" ? (
            <>
              <BasicPanelField label="Content-Type">
                <Input
                  aria-label="HTTP raw body content type"
                  onChange={(event) =>
                    commit({
                      body: {
                        kind: "raw_text",
                        content_type: event.currentTarget.value,
                        template: body.template,
                      },
                    })
                  }
                  value={httpString(body.content_type)}
                />
              </BasicPanelField>
              <BasicRestrictedTemplateEditor
                label="Raw text 模板"
                onChange={(template) =>
                  commit({
                    body: {
                      kind: "raw_text",
                      content_type:
                        httpString(body.content_type) || "text/plain",
                      template,
                    },
                  })
                }
                value={body.template}
              />
            </>
          ) : null}
        </section>

        <section aria-label="HTTP timeout" className="space-y-3">
          <h4 className="text-sm font-medium">Timeout</h4>
          <div className="grid gap-3 sm:grid-cols-3">
            {(["connect_ms", "read_ms", "write_ms"] as const).map((field) => (
              <BasicPanelField key={field} label={field}>
                <Input
                  aria-label={`HTTP ${field}`}
                  max={maxTimeoutMs ?? undefined}
                  min={1}
                  onChange={(event) =>
                    commit({
                      timeout: {
                        ...timeout,
                        [field]:
                          event.currentTarget.value === ""
                            ? null
                            : Number(event.currentTarget.value),
                      },
                    })
                  }
                  placeholder="null = policy default"
                  type="number"
                  value={timeoutInputValue(timeout[field])}
                />
              </BasicPanelField>
            ))}
          </div>
        </section>

        <section aria-label="HTTP response" className="space-y-3">
          <h4 className="text-sm font-medium">Response</h4>
          <BasicPanelField label="Response mode">
            <select
              aria-label="HTTP response mode"
              className="border-input h-9 w-full rounded-md border bg-transparent px-2 text-sm"
              onChange={(event) =>
                updateResponse({
                  mode: event.currentTarget.value,
                  ...(event.currentTarget.value === "text"
                    ? { schema: null }
                    : {}),
                })
              }
              value={responseMode}
            >
              <option value="json">json</option>
              <option value="text">text</option>
            </select>
          </BasicPanelField>
          <section aria-label="Accepted HTTP statuses" className="space-y-2">
            <h5 className="text-sm font-medium">Accepted status ranges</h5>
            {statuses.map((status, index) => (
              <div
                className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]"
                key={index}
              >
                <Input
                  aria-label={`Accepted status ${index + 1} from`}
                  max={599}
                  min={100}
                  onChange={(event) =>
                    updateStatus(index, {
                      from: Number(event.currentTarget.value),
                    })
                  }
                  type="number"
                  value={typeof status.from === "number" ? status.from : ""}
                />
                <Input
                  aria-label={`Accepted status ${index + 1} to`}
                  max={599}
                  min={100}
                  onChange={(event) =>
                    updateStatus(index, {
                      to: Number(event.currentTarget.value),
                    })
                  }
                  type="number"
                  value={typeof status.to === "number" ? status.to : ""}
                />
                <Button
                  onClick={() =>
                    updateResponse({
                      accepted_statuses: statuses.filter(
                        (_, statusIndex) => statusIndex !== index,
                      ),
                    })
                  }
                  type="button"
                  variant="ghost"
                >
                  删除
                </Button>
              </div>
            ))}
            <Button
              onClick={() =>
                updateResponse({
                  accepted_statuses: [
                    ...statuses.map((status) => ({
                      from: status.from,
                      to: status.to,
                    })),
                    { from: 200, to: 299 },
                  ],
                })
              }
              size="sm"
              type="button"
              variant="outline"
            >
              添加 status range
            </Button>
          </section>
          {responseMode === "json" ? (
            <>
              <BasicJsonEditor
                label="Response JSON Schema"
                objectOnly
                onChange={(schema) => updateResponse({ schema })}
                value={httpRecord(response.schema)}
              />
              <Button
                onClick={() => updateResponse({ schema: null })}
                size="sm"
                type="button"
                variant="ghost"
              >
                不校验 Response Schema
              </Button>
            </>
          ) : null}
        </section>

        <section aria-label="HTTP public limits" className="space-y-2">
          <h4 className="text-sm font-medium">服务端 Public limits（只读）</h4>
          <dl className="grid gap-2 text-xs sm:grid-cols-3">
            <div>
              <dt>Timeout</dt>
              <dd>{maxTimeoutMs ?? "unavailable"} ms</dd>
            </div>
            <div>
              <dt>Request</dt>
              <dd>{maxRequestBytes ?? "unavailable"} bytes</dd>
            </div>
            <div>
              <dt>Response</dt>
              <dd>{maxResponseBytes ?? "unavailable"} bytes</dd>
            </div>
          </dl>
        </section>

        <HttpExecutionPolicyEditor
          method={method}
          node={props.node}
          onCommand={(command) => {
            if (!locked) store.dispatch(command);
          }}
        />
      </BasicPanelShell>

      {curlOpen ? (
        <HttpCurlImportDialog
          canWrite={canWrite}
          currentConfig={sanitizedConfig}
          disabled={locked}
          httpAuthoring={httpAuthoring}
          maxRequestBytes={maxRequestBytes}
          onApply={(nextConfig) => {
            if (!locked) {
              store.dispatch(
                buildHttpRequestNodeConfigUpdate(props.node, nextConfig),
              );
            }
            setCurlOpen(false);
          }}
          onClose={() => setCurlOpen(false)}
        />
      ) : null}
    </>
  );
}
