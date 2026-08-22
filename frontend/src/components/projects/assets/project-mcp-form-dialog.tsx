"use client";

import { useEffect, useMemo, useState } from "react";

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
import type {
  CreateConfiguredMcpInput,
  McpSecretPayload,
  ProjectMcpEditableConfigurationResponse,
  UpdateConfiguredMcpInput,
} from "@/core/shared-assets";

type AuthMode = "none" | "headers" | "query";

function formString(form: FormData, name: string, fallback = ""): string {
  const value = form.get(name);
  return typeof value === "string" ? value : fallback;
}

export type ProjectMcpFormSubmission = {
  input: CreateConfiguredMcpInput | UpdateConfiguredMcpInput;
  secret: { slotName: string; payload: McpSecretPayload } | null;
};

function initialSecretShape(
  configuration?: ProjectMcpEditableConfigurationResponse,
): { mode: AuthMode; slotName: string; fields: string[] } {
  const slot = configuration?.version.secret_slots[0];
  const group = Object.keys(slot?.payload_schema ?? {})[0];
  const mode: AuthMode = group === "headers" || group === "query" ? group : "none";
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
  const initial = useMemo(() => initialSecretShape(configuration), [configuration]);
  const [mode, setMode] = useState<AuthMode>(initial.mode);
  const [slotName, setSlotName] = useState(initial.slotName);
  const [fieldsText, setFieldsText] = useState(initial.fields.join("\n"));
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [validationError, setValidationError] = useState<string | null>(null);
  const fields = fieldsText
    .split(/[\n,]/u)
    .map((value) => value.trim())
    .filter(Boolean);

  useEffect(() => {
    if (!open) setSecretValues({});
  }, [open]);

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError(null);
    const form = new FormData(event.currentTarget);
    const url = formString(form, "url").trim();
    if (!url || url.includes("?") || url.includes("#")) {
      setValidationError("MCP URL 必须是没有 query 或 fragment 的有效地址。");
      return;
    }
    if (mode !== "none" && (slotName.trim() === "" || fields.length === 0)) {
      setValidationError("声明秘密时必须填写槽位名和至少一个字段名。");
      return;
    }
    if (new Set(fields).size !== fields.length) {
      setValidationError("秘密字段名不能重复。");
      return;
    }
    const supplied = fields.filter((field) => (secretValues[field] ?? "") !== "");
    if (supplied.length > 0 && supplied.length !== fields.length) {
      setValidationError("替换一个槽位时必须填写该槽位的全部秘密字段。");
      return;
    }
    const secretSlots =
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
                fields.map((field) => [field, secretValues[field]]),
              ),
            } as McpSecretPayload,
          }
        : null;
    setSecretValues({});
    onSubmit({ input, secret });
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{configuration ? "编辑 Project MCP" : "新增 Project MCP"}</DialogTitle>
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
            <Input name="description" defaultValue={configuration?.version.definition.description ?? ""} />
          </label>
          <div className="grid gap-4 sm:grid-cols-[10rem_1fr]">
            <label className="grid gap-2 text-sm">
              Transport
              <select
                name="transport"
                className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                defaultValue={configuration?.version.definition.transport === "sse" ? "sse" : "http"}
              >
                <option value="http">HTTP</option>
                <option value="sse">SSE</option>
              </select>
            </label>
            <label className="grid gap-2 text-sm">
              URL
              <Input name="url" type="url" required defaultValue={configuration?.version.definition.url ?? ""} />
            </label>
          </div>
          <fieldset className="space-y-4 rounded-xl border p-4">
            <legend className="px-1 text-sm font-semibold">秘密槽位</legend>
            <label className="grid gap-2 text-sm">
              注入位置
              <select
                className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                value={mode}
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
                  <Input value={slotName} pattern="[a-z][a-z0-9._-]{0,62}" onChange={(event) => setSlotName(event.target.value)} />
                </label>
                <label className="grid gap-2 text-sm">
                  字段名（每行一个）
                  <textarea
                    className="border-input bg-background min-h-20 rounded-md border px-3 py-2 font-mono text-sm"
                    value={fieldsText}
                    onChange={(event) => setFieldsText(event.target.value)}
                  />
                </label>
                {fields.map((field) => (
                  <label key={field} className="grid gap-2 text-sm">
                    <code>{mode}.{field}</code>
                    <Input
                      type="password"
                      autoComplete="new-password"
                      value={secretValues[field] ?? ""}
                      placeholder={configuration ? "留空以保留" : "输入秘密值"}
                      onChange={(event) =>
                        setSecretValues((current) => ({
                          ...current,
                          [field]: event.target.value,
                        }))
                      }
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
            <Button type="button" variant="outline" disabled={pending} onClick={() => onOpenChange(false)}>
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
