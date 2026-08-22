"use client";

import { useMemo, useState } from "react";

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

type AuthMode = "none" | "headers" | "query";
type ProjectMcpSecretSlot =
  ProjectMcpEditableConfigurationResponse["version"]["definition"]["secret_slots"][number];

function projectMcpPayloadSchemasEqual(
  left: ProjectMcpSecretSlot["payload_schema"],
  right: ProjectMcpSecretSlot["payload_schema"],
): boolean {
  const groups = Object.keys(left);
  return (
    groups.length === Object.keys(right).length &&
    groups.every((group) => {
      const leftFields = left[group] ?? [];
      const rightFields = right[group];
      return (
        rightFields?.length === leftFields.length &&
        leftFields.every((field, index) => rightFields[index] === field)
      );
    })
  );
}

export function projectMcpSecretSlotsForSubmission(
  existing: readonly ProjectMcpSecretSlot[],
  edited: readonly ProjectMcpSecretSlot[],
): ProjectMcpSecretSlot[] {
  const existingSingle = existing.length === 1 ? existing[0] : undefined;
  const editedSingle = edited.length === 1 ? edited[0] : undefined;
  const preservesHiddenSingleSlotFields =
    existingSingle !== undefined &&
    editedSingle?.name === existingSingle.name &&
    projectMcpPayloadSchemasEqual(
      existingSingle.payload_schema,
      editedSingle.payload_schema,
    );
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

export function projectMcpSecretInputName(index: number): string {
  return `project_mcp_secret_${index}`;
}

export function buildProjectMcpFormSubmission({
  form,
  configuration,
  mode,
  slotName,
  fields,
  clearSecretValues,
}: {
  form: FormData;
  configuration?: ProjectMcpEditableConfigurationResponse;
  mode: AuthMode;
  slotName: string;
  fields: string[];
  clearSecretValues: () => void;
}): ProjectMcpFormSubmission {
  const submittedSecretValues = consumeWriteOnlyInput(
    Object.fromEntries(
      fields.map((field, index) => [
        field,
        formString(form, projectMcpSecretInputName(index)),
      ]),
    ),
    clearSecretValues,
  );
  const url = formString(form, "url").trim();
  if (!url || url.includes("?") || url.includes("#")) {
    throw new Error("MCP URL 必须是没有 query 或 fragment 的有效地址。");
  }
  if (mode !== "none" && (slotName.trim() === "" || fields.length === 0)) {
    throw new Error("声明秘密时必须填写槽位名和至少一个字段名。");
  }
  if (new Set(fields).size !== fields.length) {
    throw new Error("秘密字段名不能重复。");
  }
  const supplied = fields.filter(
    (field) => (submittedSecretValues[field] ?? "") !== "",
  );
  if (supplied.length > 0 && supplied.length !== fields.length) {
    throw new Error("替换一个槽位时必须填写该槽位的全部秘密字段。");
  }
  const editedSecretSlots: ProjectMcpSecretSlot[] =
    mode === "none"
      ? []
      : [
          {
            name: slotName.trim(),
            purpose:
              mode === "headers"
                ? "MCP request header secrets"
                : "MCP query parameter secrets",
            payload_schema: { [mode]: fields },
            required: true,
          },
        ];
  const secretSlots = projectMcpSecretSlotsForSubmission(
    configuration?.version.definition.secret_slots ?? [],
    editedSecretSlots,
  );
  const definition = {
    description: formString(form, "description"),
    transport: formString(form, "transport", "http") as "http" | "sse",
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
    mode !== "none" && supplied.length === fields.length
      ? {
          slotName: slotName.trim(),
          payload: {
            [mode]: Object.fromEntries(
              fields.map((field) => [field, submittedSecretValues[field]]),
            ),
          } as McpSecretPayload,
        }
      : null;
  return { input, secret };
}

function initialSecretShape(
  configuration?: ProjectMcpEditableConfigurationResponse,
): { mode: AuthMode; slotName: string; fields: string[] } {
  const slot = configuration?.version.secret_slots[0];
  const group = Object.keys(slot?.payload_schema ?? {})[0];
  const mode: AuthMode =
    group === "headers" || group === "query" ? group : "none";
  return {
    mode,
    slotName: slot?.name ?? "auth",
    fields: mode === "none" ? [] : (slot?.payload_schema[mode] ?? []),
  };
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
    () => initialSecretShape(configuration),
    [configuration],
  );
  const [mode, setMode] = useState<AuthMode>(initial.mode);
  const [slotName, setSlotName] = useState(initial.slotName);
  const [fieldsText, setFieldsText] = useState(initial.fields.join("\n"));
  const [validationError, setValidationError] = useState<string | null>(null);
  const preservesMultipleSecretSlots =
    (configuration?.version.definition.secret_slots.length ?? 0) > 1;
  const fields = fieldsText
    .split(/[\n,]/u)
    .map((value) => value.trim())
    .filter(Boolean);

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError(null);
    const formElement = event.currentTarget;
    try {
      const submission = buildProjectMcpFormSubmission({
        form: new FormData(formElement),
        configuration,
        mode,
        slotName,
        fields,
        clearSecretValues: () => {
          for (const input of formElement.querySelectorAll<HTMLInputElement>(
            "input[data-project-mcp-secret]",
          )) {
            input.value = "";
          }
        },
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
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {configuration ? "编辑 Project MCP" : "新增 Project MCP"}
          </DialogTitle>
          <DialogDescription>
            MCP 定义只声明秘密槽位；秘密值由当前 Project 独立加密保存。
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-5" onSubmit={submit}>
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
            <legend className="px-1 text-sm font-semibold">秘密槽位</legend>
            <label className="grid gap-2 text-sm">
              注入位置
              <select
                className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                value={mode}
                disabled={preservesMultipleSecretSlots}
                onChange={(event) => setMode(event.target.value as AuthMode)}
              >
                <option value="none">不需要秘密</option>
                <option value="headers">HTTP Header</option>
                <option value="query">Query 参数</option>
              </select>
            </label>
            {mode !== "none" ? (
              <>
                <label className="grid gap-2 text-sm">
                  槽位名
                  <Input
                    value={slotName}
                    pattern="[a-z][a-z0-9._-]{0,62}"
                    disabled={preservesMultipleSecretSlots}
                    onChange={(event) => setSlotName(event.target.value)}
                  />
                </label>
                <label className="grid gap-2 text-sm">
                  字段名（每行一个）
                  <textarea
                    className="border-input bg-background min-h-20 rounded-md border px-3 py-2 font-mono text-sm"
                    value={fieldsText}
                    disabled={preservesMultipleSecretSlots}
                    onChange={(event) => setFieldsText(event.target.value)}
                  />
                </label>
                {preservesMultipleSecretSlots ? (
                  <p className="text-muted-foreground text-xs">
                    此配置包含多个秘密槽位；编辑普通字段时会原样保留全部槽位。各槽位的秘密值请在
                    MCP 详情中分别管理。
                  </p>
                ) : null}
                {fields.map((field, index) => (
                  <label key={field} className="grid gap-2 text-sm">
                    <code>
                      {mode}.{field}
                    </code>
                    <Input
                      name={projectMcpSecretInputName(index)}
                      type="password"
                      autoComplete="new-password"
                      data-project-mcp-secret
                      placeholder={configuration ? "留空以保留" : "输入秘密值"}
                    />
                  </label>
                ))}
              </>
            ) : null}
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
