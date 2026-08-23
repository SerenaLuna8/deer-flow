"use client";

import { PlusIcon, Trash2Icon } from "lucide-react";
import { useMemo, useRef, useState } from "react";

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
import { consumeWriteOnlyInput } from "@/core/api/write-only-input";
import type {
  CreateConfiguredMcpInput,
  McpSecretPayload,
  ProjectMcpEditableConfigurationResponse,
  UpdateConfiguredMcpInput,
} from "@/core/shared-assets";
import {
  isForbiddenProjectMcpHeaderName,
  isValidProjectMcpCredentialName,
} from "@/core/shared-assets/mcp-runtime";

type CredentialTarget = "headers" | "query";
type ProjectMcpSecretSlot =
  ProjectMcpEditableConfigurationResponse["version"]["definition"]["secret_slots"][number];

export function projectMcpCredentialCopy() {
  return {
    description:
      "填写 MCP 服务地址；如服务需要 API Key 或 Token，可在下方配置访问凭证。凭证仅由当前 Project 加密保存。",
    sectionTitle: "访问凭证（可选）",
    listHelp: "每行对应一个请求参数，可同时配置请求头和 URL 查询参数。",
    editValueHelp:
      "仅当参数结构、Transport 和服务地址来源均未变化时，留空才会保留已有值；否则请重新填写全部凭证值。",
    targetLabel: "发送位置",
    targetOptions: {
      headers: "请求头（Header）",
      query: "URL 查询参数（Query）",
    },
    addLabel: "添加凭证参数",
    fields: {
      headers: {
        itemLabel: "请求头",
        nameLabel: "请求头名称",
        namePlaceholder: "Authorization",
        valueLabel: "凭证值",
        help: "按 MCP 服务文档填写，例如 Authorization 或 X-API-Key。",
      },
      query: {
        itemLabel: "查询参数",
        nameLabel: "查询参数名称",
        namePlaceholder: "api_key",
        valueLabel: "凭证值",
        help: "仅在 MCP 服务明确要求时使用，例如 api_key。请勿把凭证直接写入上方 URL。",
      },
    },
    valueLabel: (field: string) => `${field} 的凭证值`,
  } as const;
}

export function projectMcpSecretSlotsForSubmission(
  existing: readonly ProjectMcpSecretSlot[],
  edited: readonly ProjectMcpSecretSlot[],
): ProjectMcpSecretSlot[] {
  const existingSingle = existing.length === 1 ? existing[0] : undefined;
  const editedSingle = edited.length === 1 ? edited[0] : undefined;
  const preservesHiddenSingleSlotFields =
    existingSingle !== undefined && editedSingle?.name === existingSingle.name;
  const selected =
    existing.length > 1
      ? existing
      : preservesHiddenSingleSlotFields
        ? [
            {
              ...editedSingle,
              purpose: existingSingle.purpose,
              required: existingSingle.required,
            },
          ]
        : edited;
  return selected.map((slot) => ({
    ...slot,
    payload_schema: Object.fromEntries(
      Object.entries(slot.payload_schema).map(([group, fields]) => [
        group,
        [...fields],
      ]),
    ),
  }));
}

function formString(form: FormData, name: string, fallback = ""): string {
  const value = form.get(name);
  return typeof value === "string" ? value : fallback;
}

export type ProjectMcpFormSubmission = {
  input: CreateConfiguredMcpInput | UpdateConfiguredMcpInput;
  secret: { slotName: string; payload: McpSecretPayload } | null;
};

export type ProjectMcpCredentialFieldRow = {
  id: number;
  target: CredentialTarget;
  name: string;
};

export function projectMcpSecretInputName(index: number): string {
  return `project_mcp_secret_${index}`;
}

function secretSlotSchemasMatch(
  existing: readonly ProjectMcpSecretSlot[],
  next: readonly ProjectMcpSecretSlot[],
): boolean {
  if (existing.length !== next.length) return false;
  const nextByName = new Map(next.map((slot) => [slot.name, slot]));
  return existing.every((slot) => {
    const candidate = nextByName.get(slot.name);
    if (candidate?.required !== slot.required) return false;
    const groups = Object.keys(slot.payload_schema).sort();
    if (
      groups.length !== Object.keys(candidate.payload_schema).length ||
      groups.some(
        (group) =>
          !Object.prototype.hasOwnProperty.call(
            candidate.payload_schema,
            group,
          ) ||
          (slot.payload_schema[group] ?? []).some(
            (field, index) =>
              field !== (candidate.payload_schema[group] ?? [])[index],
          ) ||
          (slot.payload_schema[group] ?? []).length !==
            (candidate.payload_schema[group] ?? []).length,
      )
    ) {
      return false;
    }
    return true;
  });
}

function endpointOrigin(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

export function buildProjectMcpFormSubmission({
  form,
  configuration,
  fields,
  clearSecretValues,
}: {
  form: FormData;
  configuration?: ProjectMcpEditableConfigurationResponse;
  fields: readonly ProjectMcpCredentialFieldRow[];
  clearSecretValues: () => void;
}): ProjectMcpFormSubmission {
  const preservesMultipleSecretSlots =
    (configuration?.version.definition.secret_slots.length ?? 0) > 1;
  const activeFields = preservesMultipleSecretSlots ? [] : fields;
  const submittedFields = consumeWriteOnlyInput(
    activeFields.map((field) => ({
      target: field.target,
      name: field.name.trim(),
      value: formString(form, projectMcpSecretInputName(field.id)),
    })),
    clearSecretValues,
  );
  const url = formString(form, "url").trim();
  const slotName = configuration?.version.secret_slots[0]?.name ?? "auth";
  if (!url || url.includes("?") || url.includes("#")) {
    throw new Error("MCP URL 必须是没有 query 或 fragment 的有效地址。");
  }
  if (submittedFields.some((field) => field.name === "")) {
    throw new Error("请填写每一行的请求字段名称。");
  }
  for (const field of submittedFields) {
    if (isValidProjectMcpCredentialName(field.target, field.name)) continue;
    if (
      field.target === "headers" &&
      isForbiddenProjectMcpHeaderName(field.name)
    ) {
      throw new Error(
        "Host、Content-Length 等连接控制请求头不能用于凭证参数。",
      );
    }
    throw new Error(
      field.target === "headers"
        ? "请求头名称格式无效，请按 MCP 服务文档填写（例如 Authorization 或 X-API-Key）。"
        : "查询参数名称只能包含字母、数字、点、下划线、波浪线或连字符，且不超过 128 个字符。",
    );
  }
  for (const target of ["headers", "query"] as const) {
    const targetFieldNames = submittedFields
      .filter((field) => field.target === target)
      .map((field) => field.name);
    const comparableFieldNames =
      target === "headers"
        ? targetFieldNames.map((field) => field.toLowerCase())
        : targetFieldNames;
    if (new Set(comparableFieldNames).size !== comparableFieldNames.length) {
      throw new Error(
        target === "headers"
          ? "请求头名称不能重复。"
          : "查询参数名称不能重复。",
      );
    }
  }
  const supplied = submittedFields.filter((field) => field.value !== "");
  if (supplied.length > 0 && supplied.length !== submittedFields.length) {
    throw new Error("请填写全部凭证值；若暂不配置，请全部留空。");
  }
  const editedSecretSlots: ProjectMcpSecretSlot[] =
    submittedFields.length === 0
      ? []
      : [
          {
            name: slotName,
            purpose: "MCP request credentials",
            payload_schema: Object.fromEntries(
              (["headers", "query"] as const).flatMap((target) => {
                const names = submittedFields
                  .filter((field) => field.target === target)
                  .map((field) => field.name);
                return names.length > 0 ? [[target, names]] : [];
              }),
            ),
            required: true,
          },
        ];
  const secretSlots = projectMcpSecretSlotsForSubmission(
    configuration?.version.definition.secret_slots ?? [],
    editedSecretSlots,
  );
  const transport = formString(form, "transport", "http") as "http" | "sse";
  if (configuration && submittedFields.length > 0 && supplied.length === 0) {
    if (
      !secretSlotSchemasMatch(
        configuration.version.definition.secret_slots,
        secretSlots,
      )
    ) {
      throw new Error("凭证参数已发生变化，请重新填写全部凭证值。");
    }
    if (
      configuration.version.definition.transport !== transport ||
      endpointOrigin(configuration.version.definition.url) !==
        endpointOrigin(url)
    ) {
      throw new Error(
        "MCP 服务地址或 Transport 已变化，请重新填写全部凭证值。",
      );
    }
  }
  const definition = {
    description: formString(form, "description"),
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
    secret_slots: secretSlots,
  };
  const input = configuration
    ? {
        ...definition,
        expected_asset_version: configuration.item.version,
      }
    : {
        ...definition,
        display_name: formString(form, "display_name").trim(),
        slug: formString(form, "slug").trim(),
      };
  const secret =
    submittedFields.length > 0 && supplied.length === submittedFields.length
      ? {
          slotName,
          payload: Object.fromEntries(
            (["headers", "query"] as const).flatMap((target) => {
              const values = submittedFields
                .filter((field) => field.target === target)
                .map((field) => [field.name, field.value]);
              return values.length > 0
                ? [[target, Object.fromEntries(values)]]
                : [];
            }),
          ) as McpSecretPayload,
        }
      : null;
  return { input, secret };
}

function initialCredentialFields(
  configuration?: ProjectMcpEditableConfigurationResponse,
): Omit<ProjectMcpCredentialFieldRow, "id">[] {
  const slots = configuration?.version.secret_slots ?? [];
  return (["headers", "query"] as const).flatMap((target) =>
    slots.flatMap((slot) =>
      (slot.payload_schema[target] ?? []).map((name) => ({ target, name })),
    ),
  );
}

export function ProjectMcpFormDialog({
  open,
  pending,
  errorMessage,
  configuration,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  configuration?: ProjectMcpEditableConfigurationResponse;
  onOpenChange: (open: boolean) => void;
  onSubmit: (submission: ProjectMcpFormSubmission) => void;
}) {
  const initial = useMemo(
    () => initialCredentialFields(configuration),
    [configuration],
  );
  const credentialCopy = projectMcpCredentialCopy();
  const formRef = useRef<HTMLFormElement>(null);
  const nextFieldId = useRef(initial.length);
  const [fields, setFields] = useState<ProjectMcpCredentialFieldRow[]>(() =>
    initial.map((field, id) => ({ id, ...field })),
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const preservesMultipleSecretSlots =
    (configuration?.version.definition.secret_slots.length ?? 0) > 1;

  function clearFieldValue(id: number) {
    const control = formRef.current?.elements.namedItem(
      projectMcpSecretInputName(id),
    );
    if (control instanceof HTMLInputElement) control.value = "";
  }

  function clearSecretValues() {
    for (const input of formRef.current?.querySelectorAll<HTMLInputElement>(
      "input[data-project-mcp-secret]",
    ) ?? []) {
      input.value = "";
    }
  }

  function addField() {
    const id = nextFieldId.current;
    nextFieldId.current += 1;
    setFields((current) => [...current, { id, target: "headers", name: "" }]);
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError(null);
    const formElement = event.currentTarget;
    try {
      const submission = buildProjectMcpFormSubmission({
        form: new FormData(formElement),
        configuration,
        fields,
        clearSecretValues,
      });
      onSubmit(submission);
    } catch (caught) {
      setValidationError(
        caught instanceof Error ? caught.message : "MCP 配置无效。",
      );
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            {configuration ? "编辑 Project MCP" : "新增 Project MCP"}
          </DialogTitle>
          <DialogDescription>{credentialCopy.description}</DialogDescription>
        </DialogHeader>
        <form ref={formRef} className="space-y-5" onSubmit={submit}>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="grid gap-2 text-sm">
              名称
              <Input
                name="display_name"
                required
                maxLength={120}
                readOnly={Boolean(configuration)}
                defaultValue={configuration?.item.display_name ?? ""}
              />
            </label>
            <label className="grid gap-2 text-sm">
              标识
              <Input
                name="slug"
                required
                minLength={3}
                maxLength={63}
                pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                readOnly={Boolean(configuration)}
                defaultValue={configuration?.item.slug ?? ""}
              />
            </label>
          </div>
          <label className="grid gap-2 text-sm">
            说明
            <Input
              name="description"
              defaultValue={configuration?.version.definition.description ?? ""}
            />
          </label>
          <div className="grid gap-4 sm:grid-cols-[10rem_1fr]">
            <label className="grid gap-2 text-sm">
              Transport
              <select
                name="transport"
                className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                defaultValue={
                  configuration?.version.definition.transport === "sse"
                    ? "sse"
                    : "http"
                }
              >
                <option value="http">HTTP</option>
                <option value="sse">SSE</option>
              </select>
            </label>
            <label className="grid gap-2 text-sm">
              URL
              <Input
                name="url"
                type="url"
                required
                defaultValue={configuration?.version.definition.url ?? ""}
              />
            </label>
          </div>
          <fieldset className="space-y-4 rounded-xl border p-4">
            <legend className="px-1 text-sm font-semibold">
              {credentialCopy.sectionTitle}
            </legend>
            <p
              id="project-mcp-credential-fields-help"
              className="text-muted-foreground text-xs"
            >
              {credentialCopy.listHelp}
            </p>
            {configuration && fields.length > 0 ? (
              <p className="text-muted-foreground text-xs">
                {credentialCopy.editValueHelp}
              </p>
            ) : null}
            {preservesMultipleSecretSlots ? (
              <p className="text-muted-foreground text-xs">
                此历史配置包含多个独立凭证组；为避免改变已有凭证归属，这里只展示参数。请在
                MCP 详情中分别管理凭证值。
              </p>
            ) : null}
            <div className="space-y-3">
              {fields.map((field, index) => {
                const fieldCopy = credentialCopy.fields[field.target];
                const fieldHelpId = `project-mcp-credential-field-${field.id}-help`;
                return (
                  <div
                    key={field.id}
                    className="grid gap-3 rounded-lg border border-dashed p-3 sm:grid-cols-[11rem_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
                  >
                    <label className="space-y-1">
                      <span className="text-muted-foreground text-xs">
                        {credentialCopy.targetLabel}
                      </span>
                      <select
                        className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                        value={field.target}
                        disabled={preservesMultipleSecretSlots}
                        aria-label={`第 ${index + 1} 个凭证参数的发送位置`}
                        onChange={(event) => {
                          clearFieldValue(field.id);
                          const target = event.target.value as CredentialTarget;
                          setFields((current) =>
                            current.map((candidate) =>
                              candidate.id === field.id
                                ? { ...candidate, target }
                                : candidate,
                            ),
                          );
                        }}
                      >
                        <option value="headers">
                          {credentialCopy.targetOptions.headers}
                        </option>
                        <option value="query">
                          {credentialCopy.targetOptions.query}
                        </option>
                      </select>
                    </label>
                    <label className="space-y-1">
                      <span className="text-muted-foreground text-xs">
                        {fieldCopy.nameLabel}
                      </span>
                      <Input
                        required
                        value={field.name}
                        disabled={preservesMultipleSecretSlots}
                        placeholder={fieldCopy.namePlaceholder}
                        maxLength={field.target === "headers" ? 255 : 128}
                        aria-describedby={`project-mcp-credential-fields-help ${fieldHelpId}`}
                        onChange={(event) => {
                          setFields((current) =>
                            current.map((candidate) =>
                              candidate.id === field.id
                                ? { ...candidate, name: event.target.value }
                                : candidate,
                            ),
                          );
                        }}
                      />
                      <span
                        id={fieldHelpId}
                        className="text-muted-foreground block text-xs"
                      >
                        {fieldCopy.help}
                      </span>
                    </label>
                    <label className="space-y-1">
                      <span className="text-muted-foreground text-xs">
                        {fieldCopy.valueLabel}
                      </span>
                      <Input
                        name={projectMcpSecretInputName(field.id)}
                        type="password"
                        autoComplete="new-password"
                        disabled={preservesMultipleSecretSlots}
                        data-project-mcp-secret
                        aria-label={credentialCopy.valueLabel(
                          field.name.trim() ||
                            `${fieldCopy.itemLabel} ${index + 1}`,
                        )}
                        placeholder={
                          configuration ? "结构不变可留空保留" : "输入凭证值"
                        }
                      />
                    </label>
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      disabled={preservesMultipleSecretSlots}
                      aria-label={`删除第 ${index + 1} 个凭证参数`}
                      onClick={() => {
                        clearFieldValue(field.id);
                        setFields((current) =>
                          current.filter(
                            (candidate) => candidate.id !== field.id,
                          ),
                        );
                      }}
                    >
                      <Trash2Icon aria-hidden className="size-4" />
                    </Button>
                  </div>
                );
              })}
              {!preservesMultipleSecretSlots ? (
                <Button type="button" variant="outline" onClick={addField}>
                  <PlusIcon aria-hidden className="size-4" />
                  {credentialCopy.addLabel}
                </Button>
              ) : null}
            </div>
          </fieldset>
          {validationError || errorMessage ? (
            <p role="alert" className="text-destructive text-sm">
              {validationError ?? errorMessage}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? "正在保存…" : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
