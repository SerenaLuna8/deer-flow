"use client";

import {
  CheckCircle2Icon,
  KeyRoundIcon,
  LockKeyholeIcon,
  ShieldCheckIcon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import {
  CREDENTIAL_PAYLOAD_GROUPS,
  isSafeConfiguredProjectMcpUrl,
} from "@/core/shared-assets";
import type {
  AssetListKind,
  AssetSummary,
  AssetVersion,
  CreateConfiguredMcpInput,
  CreateCredentialInput,
  CredentialPayload,
  CredentialPayloadGroup,
  ReplaceCredentialInput,
  SkillVersionInput,
  UpdateConfiguredMcpInput,
} from "@/core/shared-assets";
import { isMcpRuntimeTransport } from "@/core/shared-assets/mcp-runtime";

type MutableKind = Exclude<AssetListKind, "credentials">;
type VersionedKind = Exclude<MutableKind, "agents">;
export type VersionAuthoringInput =
  | SkillVersionInput
  | UpdateConfiguredMcpInput;
type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;

export type CredentialSecretInitialField = {
  group: CredentialPayloadGroup;
  field: string;
};

export type CredentialSecretFieldRow = CredentialSecretInitialField & {
  id: string;
};

type CredentialFieldInputErrorCode =
  | "empty_fields"
  | "unsupported_group"
  | "empty_field"
  | "field_too_long"
  | "duplicate_field"
  | "empty_value";

type CredentialFieldInputTarget = "form" | "group" | "field" | "value";

type CredentialValidationCopy =
  Translations["adminAssets"]["dialogs"]["validation"];

const CREDENTIAL_FIELD_ERROR_MESSAGES: Record<
  CredentialFieldInputErrorCode,
  string
> = {
  empty_fields: "请至少添加一个凭据字段。",
  unsupported_group: "请选择支持的凭据字段分组。",
  empty_field: "请输入字段名。",
  field_too_long: "字段名不能超过 255 个字符。",
  duplicate_field: "同一分组内不能添加重复字段。",
  empty_value: "请输入凭据值。",
};

function credentialFieldErrorMessage(
  code: CredentialFieldInputErrorCode,
  copy?: CredentialValidationCopy,
): string {
  if (!copy) return CREDENTIAL_FIELD_ERROR_MESSAGES[code];
  return {
    empty_fields: copy.emptyFields,
    unsupported_group: copy.unsupportedGroup,
    empty_field: copy.emptyField,
    field_too_long: copy.fieldTooLong,
    duplicate_field: copy.duplicateField,
    empty_value: copy.emptyValue,
  }[code];
}

export class CredentialFieldInputError extends Error {
  constructor(
    readonly code: CredentialFieldInputErrorCode,
    readonly rowId: string | null,
    readonly target: CredentialFieldInputTarget,
    copy?: CredentialValidationCopy,
  ) {
    super(credentialFieldErrorMessage(code, copy));
    this.name = "CredentialFieldInputError";
  }
}

export function credentialValueInputName(rowId: string): string {
  return `credential_value:${rowId}`;
}

function credentialGroupInputName(rowId: string): string {
  return `credential_group:${rowId}`;
}

function credentialFieldInputName(rowId: string): string {
  return `credential_field:${rowId}`;
}

function isCredentialPayloadGroup(
  value: string,
): value is CredentialPayloadGroup {
  return CREDENTIAL_PAYLOAD_GROUPS.some((group) => group === value);
}

export function buildCredentialPayload(
  rows: readonly CredentialSecretFieldRow[],
  form: FormData,
  validationCopy?: CredentialValidationCopy,
): CredentialPayload {
  if (rows.length === 0) {
    throw new CredentialFieldInputError(
      "empty_fields",
      null,
      "form",
      validationCopy,
    );
  }

  const payload: Partial<
    Record<CredentialPayloadGroup, Record<string, string>>
  > = {};
  const seen = new Set<string>();

  for (const row of rows) {
    if (!isCredentialPayloadGroup(row.group)) {
      throw new CredentialFieldInputError(
        "unsupported_group",
        row.id,
        "group",
        validationCopy,
      );
    }
    const payloadField = row.field.trim();
    if (!payloadField) {
      throw new CredentialFieldInputError(
        "empty_field",
        row.id,
        "field",
        validationCopy,
      );
    }
    if (payloadField.length > 255) {
      throw new CredentialFieldInputError(
        "field_too_long",
        row.id,
        "field",
        validationCopy,
      );
    }
    const duplicateKey = `${row.group}\u0000${payloadField}`;
    if (seen.has(duplicateKey)) {
      throw new CredentialFieldInputError(
        "duplicate_field",
        row.id,
        "field",
        validationCopy,
      );
    }
    seen.add(duplicateKey);

    const secretValue = entry(form.get(credentialValueInputName(row.id)));
    if (!secretValue) {
      throw new CredentialFieldInputError(
        "empty_value",
        row.id,
        "value",
        validationCopy,
      );
    }
    const section = (payload[row.group] ??= {});
    Object.defineProperty(section, payloadField, {
      configurable: true,
      enumerable: true,
      value: secretValue,
      writable: true,
    });
  }

  return payload as CredentialPayload;
}

export function submitCredentialSecretForm({
  mode,
  rows,
  form,
  fixedCredentialType,
  expectedVersion,
  validationCopy,
  clear,
  onCreate,
  onReplace,
}: {
  mode: "create" | "replace";
  rows: readonly CredentialSecretFieldRow[];
  form: FormData;
  fixedCredentialType?: string;
  expectedVersion: number | undefined;
  validationCopy?: CredentialValidationCopy;
  clear: () => void;
  onCreate?: (input: CreateCredentialInput) => void;
  onReplace?: (input: ReplaceCredentialInput) => void;
}) {
  const payload = buildCredentialPayload(rows, form, validationCopy);
  if (mode === "create") {
    const input: CreateCredentialInput = {
      name: field(form, "name").trim(),
      display_name: field(form, "display_name").trim(),
      credential_type: (
        fixedCredentialType ?? field(form, "credential_type")
      ).trim(),
      payload,
    };
    clear();
    onCreate?.(input);
    return;
  }

  const input: ReplaceCredentialInput = {
    payload,
    expected_credential_version: expectedVersion ?? 1,
  };
  clear();
  onReplace?.(input);
}

function initialCredentialFieldRows(
  idPrefix: string,
  initialFields: readonly CredentialSecretInitialField[] | undefined,
): CredentialSecretFieldRow[] {
  const fields =
    initialFields && initialFields.length > 0
      ? initialFields
      : [{ group: "env" as const, field: "" }];
  return fields.map((item, index) => ({
    id: `${idPrefix}-initial-${index}`,
    group: item.group,
    field: item.field,
  }));
}

function credentialInitialFieldsSignature(
  initialFields: readonly CredentialSecretInitialField[] | undefined,
): string {
  return JSON.stringify(initialFields ?? []);
}

function showCredentialFieldError(
  form: HTMLFormElement,
  error: CredentialFieldInputError,
) {
  if (!error.rowId || error.target === "form") {
    return;
  }
  const name =
    error.target === "group"
      ? credentialGroupInputName(error.rowId)
      : error.target === "field"
        ? credentialFieldInputName(error.rowId)
        : credentialValueInputName(error.rowId);
  const control = form.elements.namedItem(name);
  if (
    control &&
    "setCustomValidity" in control &&
    typeof control.setCustomValidity === "function"
  ) {
    control.setCustomValidity(error.message);
    if (
      "reportValidity" in control &&
      typeof control.reportValidity === "function"
    ) {
      control.reportValidity();
    }
  }
}

const KIND_LABEL: Record<AssetListKind, string> = {
  agents: "Agent",
  skills: "Skill",
  "mcp-servers": "MCP",
  credentials: "Credential",
};

function entry(value: FormDataEntryValue | null, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function field(form: FormData, name: string, fallback = ""): string {
  return entry(form.get(name), fallback);
}

function list(value: FormDataEntryValue | null): string[] {
  return entry(value)
    .split(/[\n,]/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

const PROJECT_MCP_CREDENTIAL_SLOT_GROUPS = ["headers", "query"] as const;
export type ProjectMcpCredentialSlotGroup =
  (typeof PROJECT_MCP_CREDENTIAL_SLOT_GROUPS)[number];
export type ProjectMcpAuthMode = "none" | ProjectMcpCredentialSlotGroup;

export type ProjectMcpDraft = {
  transport: "http" | "sse";
  url: string;
  authMode: ProjectMcpAuthMode;
  fields: string[];
};

export function projectMcpDraftFromVersion(
  version: McpAssetVersion,
): ProjectMcpDraft {
  const slot = version.credential_slots[0];
  const group = Object.keys(slot?.payload_schema ?? {}).find(
    (value): value is ProjectMcpCredentialSlotGroup =>
      value === "headers" || value === "query",
  );
  return {
    transport: version.definition.transport === "sse" ? "sse" : "http",
    url: version.definition.url ?? "",
    authMode: group ?? "none",
    fields: group ? (slot?.payload_schema[group] ?? []) : [],
  };
}

export type ProjectMcpCredentialOption = {
  credentialId: string;
  credentialVersionId: string;
  displayName: string;
  name: string;
};

export type ProjectMcpCredentialSelection = {
  canApprove: boolean;
  loading: boolean;
  errorMessage: string | null;
  options: readonly ProjectMcpCredentialOption[];
  selectedCredentialVersionId: string;
  onChange: (credentialVersionId: string) => void;
  onCreate: () => void;
  onRetry: () => void;
};

function isProjectMcpCredentialSlotGroup(
  value: string,
): value is ProjectMcpCredentialSlotGroup {
  return PROJECT_MCP_CREDENTIAL_SLOT_GROUPS.some((group) => group === value);
}

function isProjectMcpAuthMode(value: string): value is ProjectMcpAuthMode {
  return value === "none" || isProjectMcpCredentialSlotGroup(value);
}

function projectMcpAuthPurpose(group: ProjectMcpCredentialSlotGroup): string {
  return group === "headers"
    ? "MCP request header authentication"
    : "MCP query parameter authentication";
}

export function projectMcpAuthFieldDraft(
  nextMode: ProjectMcpAuthMode,
  current: string,
): string {
  if (
    nextMode === "headers" &&
    (current.trim().length === 0 || current === "key")
  ) {
    return "Authorization";
  }
  if (
    nextMode === "query" &&
    (current.trim().length === 0 || current === "Authorization")
  ) {
    return "key";
  }
  return current;
}

function projectMcpCredentialFieldsAreSafe(
  group: ProjectMcpCredentialSlotGroup,
  fields: readonly string[],
): boolean {
  const pattern =
    group === "headers"
      ? /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/u
      : /^[A-Za-z0-9._~-]+$/u;
  return fields.every((item) => pattern.test(item));
}

function projectMcpUrlWithoutQuery(value: string): string | null {
  try {
    const parsed = new URL(value);
    if (!value.includes("?")) return null;
    parsed.search = "";
    return parsed.toString();
  } catch {
    return null;
  }
}

export class VersionInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VersionInputError";
  }
}

type McpValidationCopy = {
  unsupportedMcpTransport: string;
  missingMcpUrl: string;
  invalidMcpUrl: string;
  mcpUrlQuery: string;
  unsupportedMcpCredentialGroup: string;
  missingMcpCredentialSlotName: string;
  missingMcpCredentialFields: string;
  missingMcpHeaderName: string;
  missingMcpQueryName: string;
  invalidMcpCredentialFieldName: string;
};

const DEFAULT_MCP_VALIDATION_COPY: McpValidationCopy = {
  unsupportedMcpTransport: "新 MCP 配置仅支持 SSE 或 HTTP",
  missingMcpUrl: "SSE 或 HTTP 传输必须填写 URL",
  invalidMcpUrl:
    "请输入 Worker 可访问且不含内嵌凭据、查询参数或片段的完整 HTTP 或 HTTPS 地址，主机仅支持精确的 localhost 或规范格式的 IPv4/IPv6 字面量，不解析普通 DNS 主机名。localhost 大小写不敏感并按 127.0.0.1 处理，IPv6 请显式填写 [::1]；IP 必须属于管理员配置的允许网段，网段由平台统一配置，无需在此表单选择。",
  mcpUrlQuery:
    "URL 不能包含查询参数。请填写基础 URL，并通过查询参数凭据槽位保存密钥。",
  unsupportedMcpCredentialGroup: "项目 MCP 凭据槽位仅支持请求头或查询参数。",
  missingMcpCredentialSlotName: "填写凭据字段时必须同时填写槽位名称。",
  missingMcpCredentialFields: "填写槽位名称后，请填写至少一个必需字段。",
  missingMcpHeaderName: "请填写请求头名称，例如 Authorization。",
  missingMcpQueryName: "请填写查询参数名称，例如 key。",
  invalidMcpCredentialFieldName:
    "这里只填写请求头名称或查询参数名称，不要粘贴 Basic、Bearer 或密钥值。",
};

function encodeBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return window.btoa(binary);
}

export function skillMarkdownTemplate(
  assetSlug: string,
  copy: {
    description: string;
    instructions: string;
  } = {
    description: "Describe when and how to use this skill.",
    instructions: "Add instructions for this skill here.",
  },
): string {
  return `---
name: ${assetSlug}
description: ${copy.description}
---

# ${assetSlug}

${copy.instructions}
`;
}

export function CreateAssetDialog({
  kind,
  scope = "system",
  open,
  pending,
  errorMessage,
  onOpenChange,
  onSubmit,
}: {
  kind: MutableKind;
  scope?: "system" | "project";
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: { slug: string; display_name: string }) => void;
}) {
  const { t } = useI18n();
  const label = KIND_LABEL[kind];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={t.adminOperations.ui.close}>
        <DialogHeader>
          <DialogTitle>
            {t.adminAssets.dialogs.createAssetTitle(label)}
          </DialogTitle>
          <DialogDescription>
            {scope === "project" && kind === "skills"
              ? t.adminAssets.dialogs.skillCreationDescription
              : t.adminAssets.dialogs.assetCreationDescription(scope)}
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            onSubmit({
              display_name: field(form, "display_name").trim(),
              slug: field(form, "slug").trim(),
            });
          }}
        >
          <label className="grid gap-2 text-sm">
            {t.adminAssets.dialogs.name}
            <Input name="display_name" required maxLength={120} />
          </label>
          <label className="grid gap-2 text-sm">
            {t.adminAssets.dialogs.assetSlug}
            <Input
              name="slug"
              required
              minLength={3}
              maxLength={63}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              placeholder={t.adminAssets.dialogs.slugHelp}
              title={t.adminAssets.dialogs.slugTitle}
            />
          </label>
          {errorMessage && (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {pending
                ? t.adminAssets.common.creating
                : t.adminAssets.common.create}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SkillVersionFields({ assetSlug }: { assetSlug: string }) {
  const { t } = useI18n();
  return (
    <>
      <dl className="bg-muted/35 grid gap-3 rounded-lg p-3 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground text-xs">
            {t.adminAssets.dialogs.filePath}
          </dt>
          <dd className="mt-1 font-mono">SKILL.md</dd>
        </div>
        <div>
          <dt className="text-muted-foreground text-xs">
            {t.adminAssets.dialogs.mediaType}
          </dt>
          <dd className="mt-1 font-mono">text/markdown</dd>
        </div>
      </dl>
      <label className="grid gap-2 text-sm">
        {t.adminAssets.dialogs.fileContent}
        <Textarea
          name="content"
          required
          rows={12}
          defaultValue={skillMarkdownTemplate(assetSlug, {
            description: t.adminAssets.dialogs.skillTemplateDescription,
            instructions: t.adminAssets.dialogs.skillTemplateInstructions,
          })}
        />
      </label>
    </>
  );
}

function ProjectMcpCredentialField({
  authMode,
  fields,
  selection,
}: {
  authMode: ProjectMcpCredentialSlotGroup;
  fields: readonly string[];
  selection: ProjectMcpCredentialSelection;
}) {
  const { t } = useI18n();
  const selected = selection.options.find(
    (item) =>
      item.credentialVersionId === selection.selectedCredentialVersionId,
  );
  const fieldSummary = fields.join(", ");
  const groupLabel =
    authMode === "headers"
      ? t.adminAssets.dialogs.headersGroup
      : t.adminAssets.dialogs.queryGroup;

  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <label className="grid min-w-0 flex-1 gap-2 text-sm">
          {t.adminAssets.dialogs.projectCredential}
          <select
            name="credential_version_id"
            value={selection.selectedCredentialVersionId}
            disabled={
              !selection.canApprove ||
              selection.loading ||
              Boolean(selection.errorMessage)
            }
            className="border-input bg-background h-10 min-w-0 rounded-md border px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
            onChange={(event) => selection.onChange(event.currentTarget.value)}
          >
            <option value="">
              {!selection.canApprove
                ? t.adminAssets.dialogs.credentialSelectedByAdmin
                : selection.loading
                  ? t.adminAssets.dialogs.approval.loadingCredentials
                  : selection.options.length === 0
                    ? t.adminAssets.dialogs.noCompatibleCredential
                    : t.adminAssets.dialogs.approval.selectCredential}
            </option>
            {selection.options.map((credential) => (
              <option
                key={credential.credentialVersionId}
                value={credential.credentialVersionId}
              >
                {credential.displayName} · {credential.name}
              </option>
            ))}
          </select>
        </label>
        {selection.canApprove ? (
          <Button
            type="button"
            variant="outline"
            disabled={selection.loading}
            onClick={selection.onCreate}
          >
            <KeyRoundIcon aria-hidden className="size-4" />
            {t.adminAssets.dialogs.createProjectCredential}
          </Button>
        ) : null}
      </div>
      {selected ? (
        <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
          <span>
            {groupLabel} · {fieldSummary}
          </span>
          <span className="bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-700 dark:text-emerald-300">
            {t.adminAssets.dialogs.credentialFieldsMatch}
          </span>
        </div>
      ) : null}
      <p className="text-muted-foreground text-xs">
        {selection.canApprove
          ? t.adminAssets.dialogs.compatibleCredentialsOnly
          : t.adminAssets.dialogs.adminCompletesApproval}
      </p>
      {selection.errorMessage ? (
        <div className="flex flex-wrap items-center gap-2">
          <p role="alert" className="text-destructive text-xs">
            {selection.errorMessage}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={selection.onRetry}
          >
            {t.adminAssets.common.retry}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function McpVersionFields({
  initialVersion = null,
  credentialSelection,
  configurationLocked = false,
  onDraftChange,
}: {
  initialVersion?: McpAssetVersion | null;
  credentialSelection?: ProjectMcpCredentialSelection;
  configurationLocked?: boolean;
  onDraftChange?: (draft: ProjectMcpDraft) => void;
} = {}) {
  const { t } = useI18n();
  const initialSlot = initialVersion?.credential_slots[0];
  const initialSlotGroup = Object.keys(initialSlot?.payload_schema ?? {}).find(
    (group): group is ProjectMcpCredentialSlotGroup =>
      isProjectMcpCredentialSlotGroup(group),
  );
  const initialTransport = isMcpRuntimeTransport(
    initialVersion?.definition.transport ?? "http",
  )
    ? (initialVersion?.definition.transport as "http" | "sse")
    : "http";
  const [transport, setTransport] = useState<"http" | "sse">(initialTransport);
  const [url, setUrl] = useState(initialVersion?.definition.url ?? "");
  const [authMode, setAuthMode] = useState<ProjectMcpAuthMode>(
    initialSlotGroup ?? "none",
  );
  const [slotFields, setSlotFields] = useState(
    initialSlotGroup
      ? (initialSlot?.payload_schema[initialSlotGroup]?.join(", ") ?? "")
      : "",
  );
  const [queryRemoved, setQueryRemoved] = useState(false);
  const fields = useMemo(() => list(slotFields), [slotFields]);
  const initialSlotPurpose = initialSlot?.purpose.trim();

  useEffect(() => {
    onDraftChange?.({ transport, url, authMode, fields });
  }, [authMode, fields, onDraftChange, transport, url]);

  return (
    <div className="space-y-4">
      <label className="grid gap-2 text-sm">
        {t.adminAssets.dialogs.description}
        <Textarea
          name="description"
          rows={2}
          readOnly={configurationLocked}
          defaultValue={initialVersion?.definition.description ?? ""}
        />
      </label>
      <div className="grid gap-4 sm:grid-cols-[15rem_minmax(0,1fr)] sm:items-start">
        <label className="grid gap-2 text-sm">
          {t.adminAssets.dialogs.transport}
          <select
            name="transport"
            value={transport}
            disabled={configurationLocked}
            className="border-input bg-background h-10 rounded-md border px-3 text-sm"
            onChange={(event) => {
              if (isMcpRuntimeTransport(event.currentTarget.value)) {
                setTransport(event.currentTarget.value);
              }
            }}
          >
            <option value="http">{t.adminAssets.dialogs.httpTransport}</option>
            <option value="sse">{t.adminAssets.dialogs.sseTransport}</option>
          </select>
          {configurationLocked ? (
            <input type="hidden" name="transport" value={transport} />
          ) : null}
        </label>
        <label className="grid gap-2 text-sm">
          {t.adminAssets.dialogs.mcpServiceUrl}
          <Input
            name="url"
            type="url"
            required
            placeholder="http://localhost:8771/api/mcp"
            value={url}
            readOnly={configurationLocked}
            onBlur={(event) => {
              event.currentTarget.setCustomValidity(
                event.currentTarget.value &&
                  !isSafeConfiguredProjectMcpUrl(event.currentTarget.value)
                  ? t.adminAssets.dialogs.invalidMcpUrl
                  : "",
              );
            }}
            onInput={(event) => {
              event.currentTarget.setCustomValidity("");
              const sanitized = projectMcpUrlWithoutQuery(
                event.currentTarget.value,
              );
              if (sanitized !== null) {
                event.currentTarget.value = sanitized;
                setQueryRemoved(true);
              }
              setUrl(event.currentTarget.value);
            }}
          />
          {queryRemoved ? (
            <span role="alert" className="text-destructive text-xs">
              {t.adminAssets.dialogs.urlQueryRemoved}
            </span>
          ) : null}
        </label>
      </div>

      <fieldset className="space-y-3">
        <legend className="text-sm font-semibold">
          {t.adminAssets.dialogs.authentication}
        </legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {(
            [
              ["headers", t.adminAssets.dialogs.headerAuthentication],
              ["query", t.adminAssets.dialogs.queryAuthentication],
              ["none", t.adminAssets.dialogs.noAuthentication],
            ] as const
          ).map(([value, label]) => (
            <label
              key={value}
              className="border-border has-[:checked]:border-primary has-[:checked]:bg-primary/5 flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 text-sm font-medium"
            >
              <input
                type="radio"
                name="auth_mode"
                value={value}
                checked={authMode === value}
                disabled={configurationLocked}
                className="accent-primary size-4"
                onChange={() => {
                  setAuthMode(value);
                  setSlotFields((current) =>
                    projectMcpAuthFieldDraft(value, current),
                  );
                }}
              />
              {label}
            </label>
          ))}
        </div>
        {configurationLocked ? (
          <input type="hidden" name="auth_mode" value={authMode} />
        ) : null}

        {authMode !== "none" ? (
          <div className="border-border/70 bg-muted/15 space-y-3 rounded-xl border p-3">
            <input
              type="hidden"
              name="slot_name"
              value={initialSlot?.name ?? "auth"}
            />
            <input
              type="hidden"
              name="slot_purpose"
              value={initialSlotPurpose ?? projectMcpAuthPurpose(authMode)}
            />
            <input type="hidden" name="slot_group" value={authMode} />
            <label className="grid gap-2 text-sm">
              {authMode === "headers"
                ? t.adminAssets.dialogs.requestHeaderName
                : t.adminAssets.dialogs.queryParameterName}
              <Input
                name="slot_fields"
                required
                maxLength={2048}
                placeholder={authMode === "headers" ? "Authorization" : "key"}
                value={slotFields}
                readOnly={configurationLocked}
                title={t.adminAssets.dialogs.credentialFieldNameTitle}
                onChange={(event) => {
                  event.currentTarget.setCustomValidity("");
                  setSlotFields(event.currentTarget.value);
                }}
                onBlur={(event) => {
                  const nextFields = list(event.currentTarget.value);
                  event.currentTarget.setCustomValidity(
                    nextFields.length > 0 &&
                      !projectMcpCredentialFieldsAreSafe(authMode, nextFields)
                      ? t.adminAssets.dialogs.invalidMcpCredentialFieldName
                      : "",
                  );
                }}
              />
              <span className="text-muted-foreground text-xs">
                {t.adminAssets.dialogs.credentialFieldNameHelp}
              </span>
            </label>
            {credentialSelection ? (
              <ProjectMcpCredentialField
                authMode={authMode}
                fields={fields}
                selection={credentialSelection}
              />
            ) : null}
          </div>
        ) : (
          <p className="text-muted-foreground text-xs">
            {t.adminAssets.dialogs.noAuthenticationHelp}
          </p>
        )}
      </fieldset>
    </div>
  );
}

export function McpCreateSafetyPreview({
  draft,
  selectedCredential,
  canApprove,
}: {
  draft: ProjectMcpDraft;
  selectedCredential: ProjectMcpCredentialOption | null;
  canApprove: boolean;
}) {
  const { t } = useI18n();
  const authLabel =
    draft.authMode === "headers"
      ? t.adminAssets.dialogs.headersGroup
      : draft.authMode === "query"
        ? t.adminAssets.dialogs.queryGroup
        : t.adminAssets.dialogs.noAuthentication;
  const fieldSummary = draft.fields.join(", ");

  return (
    <aside className="bg-muted/20 border-border/70 space-y-7 border-t p-5 lg:border-t-0 lg:border-l lg:p-6">
      <section className="space-y-3" aria-labelledby="mcp-safety-preview-title">
        <div className="flex items-center gap-2">
          <ShieldCheckIcon aria-hidden className="text-primary size-5" />
          <h3 id="mcp-safety-preview-title" className="font-semibold">
            {t.adminAssets.dialogs.safetyPreview}
          </h3>
        </div>
        <p className="text-muted-foreground text-xs">
          {t.adminAssets.dialogs.configurationPreviewReadonly}
        </p>
        <div className="border-primary/20 bg-background divide-border/70 divide-y rounded-xl border px-4">
          <div className="grid gap-1 py-3 text-sm sm:grid-cols-[8rem_1fr]">
            <span className="text-muted-foreground">
              {t.adminAssets.dialogs.serviceAddress}
            </span>
            <span className="font-medium break-all">
              {draft.url || t.adminAssets.dialogs.waitingForServiceAddress}
            </span>
          </div>
          <div className="grid gap-1 py-3 text-sm sm:grid-cols-[8rem_1fr]">
            <span className="text-muted-foreground">
              {t.adminAssets.dialogs.authentication}
            </span>
            <span className="font-medium">{authLabel}</span>
          </div>
          {draft.authMode !== "none" ? (
            <>
              <div className="grid gap-1 py-3 text-sm sm:grid-cols-[8rem_1fr]">
                <span className="text-muted-foreground">
                  {fieldSummary || t.adminAssets.dialogs.fieldName}
                </span>
                <span className="flex flex-wrap items-center gap-2 font-medium">
                  <span>
                    ←{" "}
                    {selectedCredential?.displayName ??
                      t.adminAssets.dialogs.pendingCredentialSelection}
                  </span>
                  <span className="bg-primary/10 text-primary rounded-full px-2 py-0.5 text-xs font-medium">
                    {t.adminAssets.dialogs.encryptedRead}
                  </span>
                  {selectedCredential ? (
                    <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                      {t.adminAssets.dialogs.credentialFieldsMatch}
                    </span>
                  ) : null}
                </span>
              </div>
              <div className="grid gap-1 py-3 text-sm sm:grid-cols-[8rem_1fr]">
                <span className="text-muted-foreground">
                  {t.adminAssets.dialogs.credentialSource}
                </span>
                <span className="flex items-center gap-2 font-medium">
                  <LockKeyholeIcon
                    aria-hidden
                    className="text-primary size-4"
                  />
                  {selectedCredential
                    ? t.adminAssets.dialogs.encryptedProjectCredential
                    : t.adminAssets.dialogs.pendingCredentialSelection}
                </span>
              </div>
            </>
          ) : null}
          <div className="grid gap-1 py-3 text-sm sm:grid-cols-[8rem_1fr]">
            <span className="text-muted-foreground">
              {t.adminAssets.dialogs.publicationStatus}
            </span>
            <span className="font-medium">
              {draft.authMode === "none"
                ? t.adminAssets.dialogs.publishOnSave
                : selectedCredential && canApprove
                  ? t.adminAssets.dialogs.publishAfterApproval
                  : canApprove
                    ? t.adminAssets.dialogs.pendingCredentialSelection
                    : t.adminAssets.dialogs.adminCompletesApproval}
            </span>
          </div>
        </div>
      </section>

      <section
        className="space-y-3"
        aria-labelledby="mcp-publication-flow-title"
      >
        <h3 id="mcp-publication-flow-title" className="font-semibold">
          {t.adminAssets.dialogs.publicationFlow}
        </h3>
        <ol className="space-y-3 text-sm">
          {[
            [
              t.adminAssets.dialogs.saveMcpStep,
              t.adminAssets.dialogs.saveMcpStepDetail,
            ],
            [
              t.adminAssets.dialogs.selectCredentialStep,
              t.adminAssets.dialogs.selectCredentialStepDetail,
            ],
            [
              t.adminAssets.dialogs.approvePublishStep,
              t.adminAssets.dialogs.approvePublishStepDetail,
            ],
          ].map(([step, detail]) => (
            <li key={step} className="flex items-start gap-3">
              <CheckCircle2Icon
                aria-hidden
                className="text-primary mt-0.5 size-4 shrink-0"
              />
              <div className="space-y-1">
                <p className="font-medium">{step}</p>
                <p className="text-muted-foreground text-xs">{detail}</p>
              </div>
            </li>
          ))}
        </ol>
        <p className="text-muted-foreground text-xs">
          {canApprove
            ? t.adminAssets.dialogs.approvalRunsAfterSave
            : t.adminAssets.dialogs.adminCompletesApproval}
        </p>
      </section>
    </aside>
  );
}

function mcpConfigurationInput(
  form: FormData,
  validationCopy: McpValidationCopy = DEFAULT_MCP_VALIDATION_COPY,
): Omit<CreateConfiguredMcpInput, "slug" | "display_name"> {
  const rawAuthMode = field(form, "auth_mode");
  const legacyNeedsCredential =
    field(form, "needs_project_credential") === "true";
  const legacyGroup = field(form, "slot_group", "headers");
  const authMode: ProjectMcpAuthMode = isProjectMcpAuthMode(rawAuthMode)
    ? rawAuthMode
    : legacyNeedsCredential && isProjectMcpCredentialSlotGroup(legacyGroup)
      ? legacyGroup
      : "none";
  const needsProjectCredential = authMode !== "none";
  const slotName = needsProjectCredential
    ? field(form, "slot_name", "auth").trim()
    : "";
  const slotFields = needsProjectCredential
    ? list(form.get("slot_fields"))
    : [];
  const transport = field(form, "transport", "http");
  if (!isMcpRuntimeTransport(transport)) {
    throw new VersionInputError(validationCopy.unsupportedMcpTransport);
  }
  const url = field(form, "url").trim();
  if (!url) {
    throw new VersionInputError(validationCopy.missingMcpUrl);
  }
  if (projectMcpUrlWithoutQuery(url) !== null) {
    throw new VersionInputError(validationCopy.mcpUrlQuery);
  }
  if (!isSafeConfiguredProjectMcpUrl(url)) {
    throw new VersionInputError(validationCopy.invalidMcpUrl);
  }
  if (needsProjectCredential && !slotName) {
    throw new VersionInputError(validationCopy.missingMcpCredentialSlotName);
  }
  if (slotName && slotFields.length === 0) {
    throw new VersionInputError(
      authMode === "query"
        ? validationCopy.missingMcpQueryName
        : validationCopy.missingMcpHeaderName,
    );
  }
  const slotGroupValue =
    authMode === "none" ? field(form, "slot_group", "headers") : authMode;
  if (slotName && !isProjectMcpCredentialSlotGroup(slotGroupValue)) {
    throw new VersionInputError(validationCopy.unsupportedMcpCredentialGroup);
  }
  const slotGroup = isProjectMcpCredentialSlotGroup(slotGroupValue)
    ? slotGroupValue
    : "headers";
  if (slotName && !projectMcpCredentialFieldsAreSafe(slotGroup, slotFields)) {
    throw new VersionInputError(validationCopy.invalidMcpCredentialFieldName);
  }
  return {
    description: field(form, "description"),
    transport,
    command: null,
    args: [],
    url,
    env: {},
    headers: {},
    oauth: {},
    routing: {},
    tool_overrides: {},
    timeout_seconds: 30,
    credential_slots: slotName
      ? [
          {
            name: slotName,
            purpose:
              field(form, "slot_purpose").trim() ||
              projectMcpAuthPurpose(slotGroup),
            payload_schema: { [slotGroup]: slotFields },
            required: true,
          },
        ]
      : [],
  };
}

export function configuredMcpInput(
  form: FormData,
  validationCopy: McpValidationCopy = DEFAULT_MCP_VALIDATION_COPY,
): CreateConfiguredMcpInput {
  return {
    display_name: field(form, "display_name").trim(),
    slug: field(form, "slug").trim(),
    ...mcpConfigurationInput(form, validationCopy),
  };
}

export function versionInput(
  kind: VersionedKind,
  form: FormData,
  expectedAssetVersion: number,
  validationCopy: McpValidationCopy = DEFAULT_MCP_VALIDATION_COPY,
): VersionAuthoringInput {
  if (kind === "skills") {
    return {
      files: [
        {
          path: "SKILL.md",
          content_base64: encodeBase64(field(form, "content")),
          media_type: "text/markdown",
        },
      ],
      expected_asset_version: expectedAssetVersion,
    };
  }
  return {
    ...mcpConfigurationInput(form, validationCopy),
    expected_asset_version: expectedAssetVersion,
  };
}

export function AddProjectMcpDialogContent({
  pending,
  errorMessage,
  editConfiguration,
  credentialSelection,
  configurationLocked = false,
  submitLabel,
  footerNote,
  onCancel,
  onDraftChange,
  onSubmit,
}: {
  pending: boolean;
  errorMessage: string | null;
  editConfiguration?: {
    asset: AssetSummary;
    version: McpAssetVersion;
  };
  credentialSelection?: ProjectMcpCredentialSelection;
  configurationLocked?: boolean;
  submitLabel?: string;
  footerNote?: string;
  onCancel?: () => void;
  onDraftChange?: (draft: ProjectMcpDraft) => void;
  onSubmit: (
    input: CreateConfiguredMcpInput | UpdateConfiguredMcpInput,
  ) => void;
}) {
  const { t } = useI18n();
  const [validationError, setValidationError] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProjectMcpDraft>(() =>
    editConfiguration
      ? projectMcpDraftFromVersion(editConfiguration.version)
      : {
          transport: "http",
          url: "",
          authMode: "none",
          fields: [],
        },
  );
  const handleDraftChange = useCallback(
    (nextDraft: ProjectMcpDraft) => {
      setDraft(nextDraft);
      onDraftChange?.(nextDraft);
    },
    [onDraftChange],
  );
  const selectedCredential =
    credentialSelection?.options.find(
      (item) =>
        item.credentialVersionId ===
        credentialSelection.selectedCredentialVersionId,
    ) ?? null;
  const resolvedSubmitLabel =
    submitLabel ??
    (editConfiguration
      ? t.adminAssets.dialogs.saveMcpConfig
      : draft.authMode === "none"
        ? t.adminAssets.dialogs.addAndPublish
        : t.adminAssets.dialogs.addAndSubmitApproval);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <DialogHeader className="border-border/70 border-b px-6 pt-6 pb-4">
        <DialogTitle>
          {editConfiguration
            ? t.adminAssets.dialogs.editMcpConfigTitle
            : t.adminAssets.dialogs.addMcpTitle}
        </DialogTitle>
        <DialogDescription>
          {editConfiguration?.asset.display_name ??
            t.adminAssets.dialogs.addMcpDescription}
        </DialogDescription>
      </DialogHeader>
      <form
        className="flex min-h-0 flex-1 flex-col"
        onSubmit={(event) => {
          event.preventDefault();
          setValidationError(null);
          try {
            const form = new FormData(event.currentTarget);
            const validationCopy = {
              unsupportedMcpTransport:
                t.adminAssets.dialogs.unsupportedMcpTransport,
              missingMcpUrl: t.adminAssets.dialogs.missingMcpUrl,
              invalidMcpUrl: t.adminAssets.dialogs.invalidMcpUrl,
              mcpUrlQuery: t.adminAssets.dialogs.mcpUrlQuery,
              unsupportedMcpCredentialGroup:
                t.adminAssets.dialogs.unsupportedMcpCredentialGroup,
              missingMcpCredentialSlotName:
                t.adminAssets.dialogs.missingMcpCredentialSlotName,
              missingMcpCredentialFields:
                t.adminAssets.dialogs.missingMcpCredentialFields,
              missingMcpHeaderName: t.adminAssets.dialogs.missingMcpHeaderName,
              missingMcpQueryName: t.adminAssets.dialogs.missingMcpQueryName,
              invalidMcpCredentialFieldName:
                t.adminAssets.dialogs.invalidMcpCredentialFieldName,
            };
            onSubmit(
              editConfiguration
                ? {
                    ...mcpConfigurationInput(form, validationCopy),
                    expected_asset_version: editConfiguration.asset.version,
                  }
                : configuredMcpInput(form, validationCopy),
            );
          } catch (error) {
            if (error instanceof VersionInputError) {
              setValidationError(error.message);
              return;
            }
            throw error;
          }
        }}
      >
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="grid min-h-full lg:grid-cols-[minmax(0,1fr)_minmax(24rem,0.95fr)]">
            <section className="space-y-4 p-5 sm:p-6">
              <h3 className="font-semibold">
                {t.adminAssets.dialogs.connectionAndAuthentication}
              </h3>
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="grid content-start gap-2 text-sm">
                  {t.adminAssets.dialogs.name}
                  <Input
                    name="display_name"
                    required
                    readOnly={configurationLocked || Boolean(editConfiguration)}
                    maxLength={120}
                    defaultValue={editConfiguration?.asset.display_name ?? ""}
                  />
                </label>
                <label className="grid content-start gap-2 text-sm">
                  {t.adminAssets.dialogs.assetSlug}
                  <Input
                    name="slug"
                    required
                    readOnly={configurationLocked || Boolean(editConfiguration)}
                    minLength={3}
                    maxLength={63}
                    pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                    placeholder={t.adminAssets.dialogs.slugHelp}
                    title={t.adminAssets.dialogs.slugTitle}
                    defaultValue={editConfiguration?.asset.slug ?? ""}
                  />
                </label>
              </div>
              <McpVersionFields
                initialVersion={editConfiguration?.version}
                credentialSelection={credentialSelection}
                configurationLocked={configurationLocked}
                onDraftChange={handleDraftChange}
              />
              {(validationError ?? errorMessage) && (
                <p role="alert" className="text-destructive text-sm">
                  {validationError ?? errorMessage}
                </p>
              )}
            </section>
            <McpCreateSafetyPreview
              draft={draft}
              selectedCredential={selectedCredential}
              canApprove={credentialSelection?.canApprove ?? false}
            />
          </div>
        </div>
        <div className="border-border/70 bg-background flex flex-col gap-3 border-t px-5 py-4 sm:px-6 lg:flex-row lg:items-center lg:justify-between">
          <p className="text-muted-foreground flex items-center gap-2 text-xs">
            <LockKeyholeIcon aria-hidden className="size-4 shrink-0" />
            {footerNote ?? t.adminAssets.dialogs.secretNeverDisplayed}
          </p>
          <DialogFooter className="sm:justify-end">
            {onCancel ? (
              <Button type="button" variant="outline" onClick={onCancel}>
                {t.adminAssets.dialogs.cancel}
              </Button>
            ) : null}
            <Button type="submit" disabled={pending}>
              {pending
                ? editConfiguration
                  ? t.adminAssets.dialogs.savingMcpConfig
                  : t.adminAssets.dialogs.addingMcp
                : resolvedSubmitLabel}
            </Button>
          </DialogFooter>
        </div>
      </form>
    </div>
  );
}

export function AddProjectMcpDialog({
  open,
  pending,
  errorMessage,
  editConfiguration,
  credentialSelection,
  configurationLocked = false,
  submitLabel,
  footerNote,
  onOpenChange,
  onDraftChange,
  onSubmit,
}: {
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  editConfiguration?: {
    asset: AssetSummary;
    version: McpAssetVersion;
  };
  credentialSelection?: ProjectMcpCredentialSelection;
  configurationLocked?: boolean;
  submitLabel?: string;
  footerNote?: string;
  onOpenChange: (open: boolean) => void;
  onDraftChange?: (draft: ProjectMcpDraft) => void;
  onSubmit: (
    input: CreateConfiguredMcpInput | UpdateConfiguredMcpInput,
  ) => void;
}) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        closeLabel={t.adminOperations.ui.close}
        className="flex max-h-[calc(100dvh-2rem)] min-h-0 flex-col gap-0 overflow-hidden p-0 sm:max-w-[calc(100vw-3rem)] xl:max-w-[92rem]"
      >
        <AddProjectMcpDialogContent
          pending={pending}
          errorMessage={errorMessage}
          editConfiguration={editConfiguration}
          credentialSelection={credentialSelection}
          configurationLocked={configurationLocked}
          submitLabel={submitLabel}
          footerNote={footerNote}
          onCancel={() => onOpenChange(false)}
          onDraftChange={onDraftChange}
          onSubmit={onSubmit}
        />
      </DialogContent>
    </Dialog>
  );
}

export function projectMcpConfigurationDialogCopy(
  pending: boolean,
  dialogs: Translations["adminAssets"]["dialogs"],
): { title: string; submit: string } {
  return {
    title: dialogs.editMcpConfigTitle,
    submit: pending ? dialogs.savingMcpConfig : dialogs.saveMcpConfig,
  };
}

export function CreateVersionDialog({
  kind,
  asset,
  open,
  pending,
  errorMessage,
  initialMcpVersion = null,
  mcpCredentialSelection,
  mcpConfigurationLocked = false,
  mcpSubmitLabel,
  mcpFooterNote,
  onMcpDraftChange,
  onOpenChange,
  onSubmit,
}: {
  kind: VersionedKind;
  asset: AssetSummary;
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  initialMcpVersion?: McpAssetVersion | null;
  mcpCredentialSelection?: ProjectMcpCredentialSelection;
  mcpConfigurationLocked?: boolean;
  mcpSubmitLabel?: string;
  mcpFooterNote?: string;
  onMcpDraftChange?: (draft: ProjectMcpDraft) => void;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: VersionAuthoringInput) => void;
}) {
  const { t } = useI18n();
  const [validationError, setValidationError] = useState<string | null>(null);
  const mcpDialogCopy = projectMcpConfigurationDialogCopy(
    pending,
    t.adminAssets.dialogs,
  );
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setValidationError(null);
        onOpenChange(next);
      }}
    >
      <DialogContent
        closeLabel={t.adminOperations.ui.close}
        className="max-h-[90vh] overflow-y-auto sm:max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle>
            {kind === "mcp-servers"
              ? mcpDialogCopy.title
              : t.adminAssets.dialogs.createVersionTitle(KIND_LABEL[kind])}
          </DialogTitle>
          <DialogDescription>{asset.display_name}</DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            setValidationError(null);
            try {
              onSubmit(
                versionInput(
                  kind,
                  new FormData(event.currentTarget),
                  asset.version,
                  {
                    unsupportedMcpTransport:
                      t.adminAssets.dialogs.unsupportedMcpTransport,
                    missingMcpUrl: t.adminAssets.dialogs.missingMcpUrl,
                    invalidMcpUrl: t.adminAssets.dialogs.invalidMcpUrl,
                    mcpUrlQuery: t.adminAssets.dialogs.mcpUrlQuery,
                    unsupportedMcpCredentialGroup:
                      t.adminAssets.dialogs.unsupportedMcpCredentialGroup,
                    missingMcpCredentialSlotName:
                      t.adminAssets.dialogs.missingMcpCredentialSlotName,
                    missingMcpCredentialFields:
                      t.adminAssets.dialogs.missingMcpCredentialFields,
                    missingMcpHeaderName:
                      t.adminAssets.dialogs.missingMcpHeaderName,
                    missingMcpQueryName:
                      t.adminAssets.dialogs.missingMcpQueryName,
                    invalidMcpCredentialFieldName:
                      t.adminAssets.dialogs.invalidMcpCredentialFieldName,
                  },
                ),
              );
            } catch (error) {
              if (error instanceof VersionInputError) {
                setValidationError(error.message);
                return;
              }
              throw error;
            }
          }}
        >
          {kind === "skills" ? (
            <SkillVersionFields assetSlug={asset.slug} />
          ) : (
            <McpVersionFields
              initialVersion={initialMcpVersion}
              credentialSelection={mcpCredentialSelection}
              configurationLocked={mcpConfigurationLocked}
              onDraftChange={onMcpDraftChange}
            />
          )}
          {(validationError ?? errorMessage) && (
            <p role="alert" className="text-destructive text-sm">
              {validationError ?? errorMessage}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {kind === "mcp-servers"
                ? (mcpSubmitLabel ?? mcpDialogCopy.submit)
                : pending
                  ? t.adminAssets.common.creatingVersion
                  : t.adminAssets.common.createVersion}
            </Button>
          </DialogFooter>
          {kind === "mcp-servers" && mcpFooterNote ? (
            <p className="text-muted-foreground text-xs">{mcpFooterNote}</p>
          ) : null}
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function CredentialSecretDialog({
  mode,
  open,
  expectedVersion,
  pending,
  disabled = false,
  fixedFields = false,
  fixedCredentialType,
  errorMessage,
  initialFields,
  onRetry,
  onOpenChange,
  onCreate,
  onReplace,
}: {
  mode: "create" | "replace";
  open: boolean;
  expectedVersion?: number;
  pending: boolean;
  disabled?: boolean;
  fixedFields?: boolean;
  fixedCredentialType?: string;
  errorMessage: string | null;
  initialFields?: readonly CredentialSecretInitialField[];
  onRetry?: () => void;
  onOpenChange: (open: boolean) => void;
  onCreate?: (input: CreateCredentialInput) => void;
  onReplace?: (input: ReplaceCredentialInput) => void;
}) {
  const { t } = useI18n();
  const fieldIdPrefix = useId();
  const nextFieldId = useRef(0);
  const [formKey, setFormKey] = useState(0);
  const [rows, setRows] = useState<CredentialSecretFieldRow[]>(() =>
    initialCredentialFieldRows(fieldIdPrefix, initialFields),
  );
  const initialFieldsSignature =
    credentialInitialFieldsSignature(initialFields);
  const previousInitialFieldsSignature = useRef(initialFieldsSignature);
  const fieldsOutOfSync =
    previousInitialFieldsSignature.current !== initialFieldsSignature;
  const latestInitialFields = useRef(initialFields);
  latestInitialFields.current = initialFields;

  const resetForm = useCallback(() => {
    setRows(
      initialCredentialFieldRows(fieldIdPrefix, latestInitialFields.current),
    );
    setFormKey((value) => value + 1);
  }, [fieldIdPrefix]);

  useEffect(() => {
    if (previousInitialFieldsSignature.current === initialFieldsSignature) {
      return;
    }
    previousInitialFieldsSignature.current = initialFieldsSignature;
    resetForm();
  }, [initialFieldsSignature, resetForm]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) resetForm();
        onOpenChange(next);
      }}
    >
      <DialogContent
        closeLabel={t.adminOperations.ui.close}
        className="max-h-[90vh] overflow-y-auto sm:max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle>
            {mode === "create"
              ? t.adminAssets.dialogs.secretCreateTitle
              : t.adminAssets.dialogs.secretReplaceTitle}
          </DialogTitle>
          <DialogDescription>
            {t.adminAssets.dialogs.secretDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          key={formKey}
          className="space-y-4"
          autoComplete="off"
          onSubmit={(event) => {
            event.preventDefault();
            if (pending || disabled || fieldsOutOfSync) {
              return;
            }
            const formElement = event.currentTarget;
            const form = new FormData(event.currentTarget);
            try {
              submitCredentialSecretForm({
                mode,
                rows,
                form,
                fixedCredentialType,
                expectedVersion,
                validationCopy: t.adminAssets.dialogs.validation,
                clear: () => {
                  event.currentTarget.reset();
                  resetForm();
                },
                onCreate,
                onReplace,
              });
            } catch (error) {
              if (error instanceof CredentialFieldInputError) {
                showCredentialFieldError(formElement, error);
                return;
              }
              throw error;
            }
          }}
        >
          {mode === "create" && (
            <>
              <label className="grid gap-2 text-sm">
                {t.adminAssets.dialogs.name}
                <Input name="display_name" required maxLength={120} />
              </label>
              <label className="grid gap-2 text-sm">
                {t.adminAssets.dialogs.credentialSlug}
                <Input
                  name="name"
                  required
                  maxLength={63}
                  pattern="[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?"
                  placeholder="github-token"
                />
              </label>
              {fixedCredentialType ? null : (
                <label className="grid gap-2 text-sm">
                  {t.adminAssets.common.type}
                  <Input
                    name="credential_type"
                    required
                    maxLength={32}
                    pattern="[a-z][a-z0-9._-]{0,31}"
                    placeholder="token"
                  />
                </label>
              )}
            </>
          )}
          <section
            className="space-y-3"
            aria-labelledby="credential-fields-title"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="space-y-1">
                <h3
                  id="credential-fields-title"
                  className="text-sm font-medium"
                >
                  {t.adminAssets.dialogs.credentialFields}
                </h3>
                <p className="text-muted-foreground text-xs">
                  {fixedFields
                    ? t.adminAssets.dialogs.fixedCredentialFieldsHelp
                    : t.adminAssets.dialogs.credentialFieldsHelp}
                </p>
              </div>
              {!fixedFields ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={pending || disabled || fieldsOutOfSync}
                  onClick={() => {
                    const id = `${fieldIdPrefix}-added-${nextFieldId.current}`;
                    nextFieldId.current += 1;
                    setRows((current) => [
                      ...current,
                      { id, group: "env", field: "" },
                    ]);
                  }}
                >
                  {t.adminAssets.dialogs.addField}
                </Button>
              ) : null}
            </div>
            <div className="space-y-3">
              {rows.map((row, index) => (
                <div
                  key={row.id}
                  className="border-border/70 bg-muted/15 grid gap-3 rounded-xl border p-3 sm:grid-cols-[10rem_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
                >
                  <label className="grid gap-2 text-sm">
                    {t.adminAssets.dialogs.group}
                    <select
                      name={credentialGroupInputName(row.id)}
                      value={row.group}
                      disabled={
                        pending || disabled || fieldsOutOfSync || fixedFields
                      }
                      className="border-input bg-background h-9 rounded-md border px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                      onChange={(event) => {
                        event.currentTarget.setCustomValidity("");
                        if (!isCredentialPayloadGroup(event.target.value)) {
                          event.currentTarget.setCustomValidity(
                            t.adminAssets.dialogs.validation.unsupportedGroup,
                          );
                          return;
                        }
                        const group = event.target.value;
                        setRows((current) =>
                          current.map((item) =>
                            item.id === row.id ? { ...item, group } : item,
                          ),
                        );
                      }}
                    >
                      <option value="env">
                        {t.adminAssets.dialogs.envGroup}
                      </option>
                      <option value="headers">
                        {t.adminAssets.dialogs.headersGroup}
                      </option>
                      <option value="query">
                        {t.adminAssets.dialogs.queryGroup}
                      </option>
                      <option value="oauth">OAuth</option>
                    </select>
                  </label>
                  <label className="grid gap-2 text-sm">
                    {t.adminAssets.dialogs.fieldName}
                    <Input
                      name={credentialFieldInputName(row.id)}
                      required
                      maxLength={255}
                      autoComplete="off"
                      disabled={pending || disabled || fieldsOutOfSync}
                      readOnly={fixedFields}
                      value={row.field}
                      onChange={(event) => {
                        event.currentTarget.setCustomValidity("");
                        const fieldName = event.target.value;
                        setRows((current) =>
                          current.map((item) =>
                            item.id === row.id
                              ? { ...item, field: fieldName }
                              : item,
                          ),
                        );
                      }}
                    />
                  </label>
                  <label className="grid gap-2 text-sm">
                    {t.adminAssets.dialogs.credentialValue}
                    <Input
                      name={credentialValueInputName(row.id)}
                      required
                      type="password"
                      autoComplete="new-password"
                      disabled={pending || disabled || fieldsOutOfSync}
                      onInput={(event) =>
                        event.currentTarget.setCustomValidity("")
                      }
                    />
                  </label>
                  {!fixedFields ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={
                        rows.length === 1 ||
                        pending ||
                        disabled ||
                        fieldsOutOfSync
                      }
                      aria-label={t.adminAssets.dialogs.removeField(index + 1)}
                      onClick={() =>
                        setRows((current) =>
                          current.filter((item) => item.id !== row.id),
                        )
                      }
                    >
                      {t.adminAssets.dialogs.remove}
                    </Button>
                  ) : null}
                </div>
              ))}
            </div>
          </section>
          {errorMessage && (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p role="alert" className="text-destructive text-sm">
                {errorMessage}
              </p>
              {disabled && onRetry && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={onRetry}
                >
                  {t.adminAssets.common.reload}
                </Button>
              )}
            </div>
          )}
          <DialogFooter>
            <Button
              type="submit"
              disabled={pending || disabled || fieldsOutOfSync}
            >
              {pending
                ? t.adminAssets.dialogs.writing
                : mode === "create"
                  ? t.adminAssets.dialogs.encryptWrite
                  : t.adminAssets.common.replaceCredential}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function CredentialRevokeDialog({
  open,
  credentialName,
  pending,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  credentialName: string;
  pending: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={t.adminOperations.ui.close}>
        <DialogHeader>
          <DialogTitle>{t.adminAssets.dialogs.revokeTitle}</DialogTitle>
          <DialogDescription>
            {t.adminAssets.dialogs.revokeDescription(credentialName)}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            {t.adminAssets.dialogs.cancel}
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={pending}
            onClick={onConfirm}
          >
            {pending
              ? t.adminAssets.dialogs.revoking
              : t.adminAssets.dialogs.confirmRevoke}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function CredentialGrantMigrationDialog({
  open,
  credentialName,
  pending,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  credentialName: string;
  pending: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={t.adminOperations.ui.close}>
        <DialogHeader>
          <DialogTitle>{t.adminAssets.dialogs.migrateTitle}</DialogTitle>
          <DialogDescription>
            {t.adminAssets.dialogs.migrateDescription(credentialName)}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            {t.adminAssets.dialogs.cancel}
          </Button>
          <Button type="button" disabled={pending} onClick={onConfirm}>
            {pending
              ? t.adminAssets.dialogs.migrating
              : t.adminAssets.dialogs.confirmMigrate}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
