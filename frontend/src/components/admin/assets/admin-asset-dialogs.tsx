"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

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
import { CREDENTIAL_PAYLOAD_GROUPS } from "@/core/shared-assets";
import type {
  AssetListKind,
  AssetSummary,
  CreateCredentialInput,
  CredentialPayload,
  CredentialPayloadGroup,
  McpVersionInput,
  ReplaceCredentialInput,
  SkillVersionInput,
} from "@/core/shared-assets";
import { isMcpRuntimeTransport } from "@/core/shared-assets/mcp-runtime";

type MutableKind = Exclude<AssetListKind, "credentials">;
type VersionedKind = Exclude<MutableKind, "agents">;
export type VersionAuthoringInput = SkillVersionInput | McpVersionInput;

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

export class CredentialFieldInputError extends Error {
  constructor(
    readonly code: CredentialFieldInputErrorCode,
    readonly rowId: string | null,
    readonly target: CredentialFieldInputTarget,
  ) {
    super(CREDENTIAL_FIELD_ERROR_MESSAGES[code]);
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
): CredentialPayload {
  if (rows.length === 0) {
    throw new CredentialFieldInputError("empty_fields", null, "form");
  }

  const payload: Partial<
    Record<CredentialPayloadGroup, Record<string, string>>
  > = {};
  const seen = new Set<string>();

  for (const row of rows) {
    if (!isCredentialPayloadGroup(row.group)) {
      throw new CredentialFieldInputError("unsupported_group", row.id, "group");
    }
    const payloadField = row.field.trim();
    if (!payloadField) {
      throw new CredentialFieldInputError("empty_field", row.id, "field");
    }
    if (payloadField.length > 255) {
      throw new CredentialFieldInputError("field_too_long", row.id, "field");
    }
    const duplicateKey = `${row.group}\u0000${payloadField}`;
    if (seen.has(duplicateKey)) {
      throw new CredentialFieldInputError("duplicate_field", row.id, "field");
    }
    seen.add(duplicateKey);

    const secretValue = entry(form.get(credentialValueInputName(row.id)));
    if (!secretValue) {
      throw new CredentialFieldInputError("empty_value", row.id, "value");
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
  expectedVersion,
  clear,
  onCreate,
  onReplace,
}: {
  mode: "create" | "replace";
  rows: readonly CredentialSecretFieldRow[];
  form: FormData;
  expectedVersion: number | undefined;
  clear: () => void;
  onCreate?: (input: CreateCredentialInput) => void;
  onReplace?: (input: ReplaceCredentialInput) => void;
}) {
  const payload = buildCredentialPayload(rows, form);
  if (mode === "create") {
    const input: CreateCredentialInput = {
      name: field(form, "name").trim(),
      display_name: field(form, "display_name").trim(),
      credential_type: field(form, "credential_type").trim(),
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

function encodeBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return window.btoa(binary);
}

export function skillMarkdownTemplate(assetSlug: string): string {
  return `---
name: ${assetSlug}
description: Describe when and how to use this skill.
---

# ${assetSlug}

Add instructions for this skill here.
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
  const label = KIND_LABEL[kind];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>创建 {label}</DialogTitle>
          <DialogDescription>
            {scope === "project" && kind === "skills"
              ? "创建后会自动生成 SKILL.md 草稿版本（已填入基础模板），Skill 默认停用。"
              : `先创建${scope === "system" ? "系统级" : "项目级"}资产，再在资产中创建并发布版本。`}
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
            名称
            <Input name="display_name" required maxLength={120} />
          </label>
          <label className="grid gap-2 text-sm">
            资产标识
            <Input
              name="slug"
              required
              minLength={3}
              maxLength={63}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              placeholder="lowercase-slug"
              title="使用 3–63 位小写字母、数字和单个连字符"
            />
            <span className="text-muted-foreground text-xs">
              3–63 位小写字母、数字或连字符；创建后作为稳定标识使用。
            </span>
          </label>
          {errorMessage && (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {pending ? "创建中…" : "创建"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SkillVersionFields({ assetSlug }: { assetSlug: string }) {
  return (
    <>
      <dl className="bg-muted/35 grid gap-3 rounded-lg p-3 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground text-xs">文件路径</dt>
          <dd className="mt-1 font-mono">SKILL.md</dd>
        </div>
        <div>
          <dt className="text-muted-foreground text-xs">媒体类型</dt>
          <dd className="mt-1 font-mono">text/markdown</dd>
        </div>
      </dl>
      <label className="grid gap-2 text-sm">
        文件内容
        <Textarea
          name="content"
          required
          rows={12}
          defaultValue={skillMarkdownTemplate(assetSlug)}
        />
      </label>
    </>
  );
}

export function McpVersionFields() {
  return (
    <>
      <label className="grid gap-2 text-sm">
        描述
        <Textarea name="description" />
      </label>
      <label className="grid gap-2 text-sm">
        传输方式
        <select
          name="transport"
          defaultValue="sse"
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
        >
          <option value="sse">服务器推送（sse）</option>
          <option value="http">HTTP</option>
        </select>
      </label>
      <label className="grid gap-2 text-sm">
        URL
        <Input
          name="url"
          type="url"
          required
          placeholder="https://mcp.example.com"
        />
        <span className="text-muted-foreground text-xs">
          该地址由 Worker 访问，须填写平台批准的精确 HTTPS 地址。
        </span>
      </label>
      <p className="text-muted-foreground text-xs">
        实际超时由平台控制，版本中的兼容值不可在此修改。
      </p>
      <div className="border-border/70 space-y-3 rounded-lg border p-3">
        <p className="text-sm font-medium">Credential 槽位（可选）</p>
        <p className="text-muted-foreground text-xs">
          创建槽位后，本版本只能通过提交和批准流程发布。
        </p>
        <label className="grid gap-2 text-sm">
          槽位名称
          <Input
            name="slot_name"
            maxLength={63}
            pattern="[a-z][a-z0-9._-]{0,62}"
            placeholder="api-token"
            title="以小写字母开头，只能使用小写字母、数字、点、下划线或连字符"
          />
          <span className="text-muted-foreground text-xs">
            以小写字母开头，最多 63 位；可使用数字、点、下划线和连字符。
          </span>
        </label>
        <label className="grid gap-2 text-sm">
          用途
          <Input name="slot_purpose" />
        </label>
        <p className="text-sm">凭据字段分组：请求头（headers）</p>
        <label className="grid gap-2 text-sm">
          必需字段（逗号或换行分隔）
          <Textarea
            name="slot_fields"
            placeholder="Authorization"
            maxLength={2048}
          />
          <span className="text-muted-foreground text-xs">
            填写所选分组中 Credential
            必须包含的字段名；多个字段用逗号或换行分隔。
          </span>
        </label>
      </div>
    </>
  );
}

export function versionInput(
  kind: VersionedKind,
  form: FormData,
  expectedAssetVersion: number,
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
  const slotName = field(form, "slot_name").trim();
  const slotFields = list(form.get("slot_fields"));
  const transport = field(form, "transport", "sse");
  if (!isMcpRuntimeTransport(transport)) {
    throw new Error("新 MCP 版本仅支持 SSE 或 HTTP");
  }
  const url = field(form, "url").trim();
  if (!url) {
    throw new Error("SSE 或 HTTP 传输必须填写 URL");
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
            purpose: field(form, "slot_purpose"),
            payload_schema: { headers: slotFields },
            required: true,
          },
        ]
      : [],
    expected_asset_version: expectedAssetVersion,
  };
}

export function CreateVersionDialog({
  kind,
  asset,
  open,
  pending,
  errorMessage,
  onOpenChange,
  onSubmit,
}: {
  kind: VersionedKind;
  asset: AssetSummary;
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: VersionAuthoringInput) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>创建 {KIND_LABEL[kind]} 版本</DialogTitle>
          <DialogDescription>{asset.display_name}</DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit(
              versionInput(
                kind,
                new FormData(event.currentTarget),
                asset.version,
              ),
            );
          }}
        >
          {kind === "skills" ? (
            <SkillVersionFields assetSlug={asset.slug} />
          ) : (
            <McpVersionFields />
          )}
          {errorMessage && (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {pending ? "创建中…" : "创建版本"}
            </Button>
          </DialogFooter>
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
  errorMessage: string | null;
  initialFields?: readonly CredentialSecretInitialField[];
  onRetry?: () => void;
  onOpenChange: (open: boolean) => void;
  onCreate?: (input: CreateCredentialInput) => void;
  onReplace?: (input: ReplaceCredentialInput) => void;
}) {
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
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {mode === "create" ? "创建 Credential" : "替换凭据"}
          </DialogTitle>
          <DialogDescription>
            凭据值只用于本次加密写入，提交后不会回显。
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
                expectedVersion,
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
                名称
                <Input name="display_name" required maxLength={120} />
              </label>
              <label className="grid gap-2 text-sm">
                Credential 标识
                <Input
                  name="name"
                  required
                  maxLength={63}
                  pattern="[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?"
                  placeholder="github-token"
                />
              </label>
              <label className="grid gap-2 text-sm">
                类型
                <Input
                  name="credential_type"
                  required
                  maxLength={32}
                  pattern="[a-z][a-z0-9._-]{0,31}"
                  placeholder="token"
                />
              </label>
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
                  凭据字段
                </h3>
                <p className="text-muted-foreground text-xs">
                  可添加多个环境变量、请求头或 OAuth 字段；每个值仅写入一次。
                </p>
              </div>
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
                添加字段
              </Button>
            </div>
            <div className="space-y-3">
              {rows.map((row, index) => (
                <div
                  key={row.id}
                  className="border-border/70 bg-muted/15 grid gap-3 rounded-xl border p-3 sm:grid-cols-[10rem_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
                >
                  <label className="grid gap-2 text-sm">
                    分组
                    <select
                      name={credentialGroupInputName(row.id)}
                      value={row.group}
                      disabled={pending || disabled || fieldsOutOfSync}
                      className="border-input bg-background h-9 rounded-md border px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                      onChange={(event) => {
                        event.currentTarget.setCustomValidity("");
                        if (!isCredentialPayloadGroup(event.target.value)) {
                          event.currentTarget.setCustomValidity(
                            CREDENTIAL_FIELD_ERROR_MESSAGES.unsupported_group,
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
                      <option value="env">环境变量（env）</option>
                      <option value="headers">请求头（headers）</option>
                      <option value="oauth">OAuth</option>
                    </select>
                  </label>
                  <label className="grid gap-2 text-sm">
                    字段名
                    <Input
                      name={credentialFieldInputName(row.id)}
                      required
                      maxLength={255}
                      autoComplete="off"
                      disabled={pending || disabled || fieldsOutOfSync}
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
                    凭据值
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
                    aria-label={`移除字段 ${index + 1}`}
                    onClick={() =>
                      setRows((current) =>
                        current.filter((item) => item.id !== row.id),
                      )
                    }
                  >
                    移除
                  </Button>
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
                  重新加载
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
                ? "写入中…"
                : mode === "create"
                  ? "加密写入"
                  : "替换凭据"}
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
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>确认撤销 Credential</DialogTitle>
          <DialogDescription>
            此操作不可恢复。撤销“{credentialName}”后，Credential
            的所有版本与相关 active Grant 将在同一事务中失效，MCP
            详情不再显示为已授权。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            取消
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={pending}
            onClick={onConfirm}
          >
            {pending ? "撤销中…" : "确认永久撤销"}
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
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>迁移 Credential Grant</DialogTitle>
          <DialogDescription>
            替换 Credential 只会创建新版本，不会自动轮换既有
            Grant。系统将仅在字段结构完全兼容时，把“
            {credentialName}”仍固定到 retired version 的 active Grant
            原子迁移到当前版本；任一 Grant 不兼容都会整体拒绝。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            取消
          </Button>
          <Button type="button" disabled={pending} onClick={onConfirm}>
            {pending ? "迁移中…" : "确认迁移 Grant"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
