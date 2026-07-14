"use client";

import { useState } from "react";

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
import type {
  AgentVersionInput,
  AssetListKind,
  AssetSummary,
  CreateCredentialInput,
  McpVersionInput,
  ReplaceCredentialInput,
  SkillVersionInput,
} from "@/core/shared-assets";

type MutableKind = Exclude<AssetListKind, "credentials">;
export type VersionAuthoringInput =
  | AgentVersionInput
  | SkillVersionInput
  | McpVersionInput;

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
            先创建{scope === "system" ? "系统级" : "项目级"}
            资产，再在资产中创建并发布版本。
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
              maxLength={120}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              placeholder="lowercase-slug"
            />
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

export function AgentVersionFields() {
  return (
    <>
      <label className="grid gap-2 text-sm">
        描述
        <Textarea name="description" />
      </label>
      <label className="grid gap-2 text-sm">
        角色设定（Soul）
        <Textarea name="soul" />
      </label>
      <label className="grid gap-2 text-sm">
        模型引用
        <Input name="model_ref" />
      </label>
      <label className="grid gap-2 text-sm">
        工具组（逗号或换行分隔）
        <Textarea name="tool_groups" />
      </label>
      <label className="grid gap-2 text-sm">
        Skill 版本 ID（逗号或换行分隔）
        <Textarea name="skill_version_ids" />
      </label>
      <label className="grid gap-2 text-sm">
        MCP 版本 ID（逗号或换行分隔）
        <Textarea name="mcp_version_ids" />
      </label>
    </>
  );
}

export function SkillVersionFields() {
  return (
    <>
      <label className="grid gap-2 text-sm">
        文件路径
        <Input name="path" required defaultValue="SKILL.md" />
      </label>
      <label className="grid gap-2 text-sm">
        媒体类型
        <Input name="media_type" required defaultValue="text/markdown" />
      </label>
      <label className="grid gap-2 text-sm">
        文件内容
        <Textarea name="content" required rows={12} />
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
          defaultValue="stdio"
          className="border-input bg-background h-9 rounded-md border px-3 text-sm"
        >
          <option value="stdio">标准输入输出（stdio）</option>
          <option value="sse">服务器推送（sse）</option>
          <option value="http">HTTP</option>
          <option value="streamable_http">流式 HTTP</option>
        </select>
      </label>
      <label className="grid gap-2 text-sm">
        命令
        <Input name="command" />
      </label>
      <label className="grid gap-2 text-sm">
        URL
        <Input name="url" type="url" />
      </label>
      <label className="grid gap-2 text-sm">
        参数（逗号或换行分隔）
        <Textarea name="args" />
      </label>
      <label className="grid gap-2 text-sm">
        超时（秒）
        <Input name="timeout_seconds" type="number" min={1} defaultValue={30} />
      </label>
      <div className="border-border/70 space-y-3 rounded-lg border p-3">
        <p className="text-sm font-medium">Credential 槽位（可选）</p>
        <p className="text-muted-foreground text-xs">
          创建槽位后，本版本只能通过提交和批准流程发布。
        </p>
        <label className="grid gap-2 text-sm">
          槽位名称
          <Input name="slot_name" />
        </label>
        <label className="grid gap-2 text-sm">
          用途
          <Input name="slot_purpose" />
        </label>
        <label className="grid gap-2 text-sm">
          凭据字段分组
          <select
            name="slot_group"
            defaultValue="headers"
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          >
            <option value="headers">请求头（headers）</option>
            <option value="env">环境变量（env）</option>
            <option value="oauth">OAuth</option>
          </select>
        </label>
        <label className="grid gap-2 text-sm">
          必需字段（逗号或换行分隔）
          <Textarea name="slot_fields" />
        </label>
      </div>
    </>
  );
}

function versionInput(
  kind: MutableKind,
  form: FormData,
  expectedAssetVersion: number,
): VersionAuthoringInput {
  if (kind === "agents") {
    return {
      description: field(form, "description"),
      soul: field(form, "soul"),
      model_ref: field(form, "model_ref"),
      tool_groups: list(form.get("tool_groups")),
      skill_version_ids: list(form.get("skill_version_ids")),
      mcp_version_ids: list(form.get("mcp_version_ids")),
      expected_asset_version: expectedAssetVersion,
    };
  }
  if (kind === "skills") {
    return {
      files: [
        {
          path: field(form, "path").trim(),
          content_base64: encodeBase64(field(form, "content")),
          media_type: field(form, "media_type").trim(),
        },
      ],
      expected_asset_version: expectedAssetVersion,
    };
  }
  const slotName = field(form, "slot_name").trim();
  const slotGroup = field(form, "slot_group", "headers");
  const slotFields = list(form.get("slot_fields"));
  return {
    description: field(form, "description"),
    transport: field(
      form,
      "transport",
      "stdio",
    ) as McpVersionInput["transport"],
    command: field(form, "command").trim() || null,
    args: list(form.get("args")),
    url: field(form, "url").trim() || null,
    env: {},
    headers: {},
    oauth: {},
    routing: {},
    tool_overrides: {},
    timeout_seconds: Number(form.get("timeout_seconds") ?? 30),
    credential_slots: slotName
      ? [
          {
            name: slotName,
            purpose: field(form, "slot_purpose"),
            payload_schema: { [slotGroup]: slotFields },
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
  kind: MutableKind;
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
          {kind === "agents" ? (
            <AgentVersionFields />
          ) : kind === "skills" ? (
            <SkillVersionFields />
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
  errorMessage,
  onOpenChange,
  onCreate,
  onReplace,
}: {
  mode: "create" | "replace";
  open: boolean;
  expectedVersion?: number;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onCreate?: (input: CreateCredentialInput) => void;
  onReplace?: (input: ReplaceCredentialInput) => void;
}) {
  const [formKey, setFormKey] = useState(0);
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setFormKey((value) => value + 1);
        onOpenChange(next);
      }}
    >
      <DialogContent>
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
            const form = new FormData(event.currentTarget);
            const group = field(form, "payload_group", "env");
            const payloadField = field(form, "payload_field").trim();
            const secretValue = field(form, "credential_value");
            const payload = { [group]: { [payloadField]: secretValue } };
            event.currentTarget.reset();
            setFormKey((value) => value + 1);
            if (mode === "create") {
              onCreate?.({
                name: field(form, "name").trim(),
                display_name: field(form, "display_name").trim(),
                credential_type: field(form, "credential_type").trim(),
                payload,
              });
            } else {
              onReplace?.({
                payload,
                expected_credential_version: expectedVersion ?? 1,
              });
            }
          }}
        >
          {mode === "create" && (
            <>
              <label className="grid gap-2 text-sm">
                名称
                <Input name="display_name" required />
              </label>
              <label className="grid gap-2 text-sm">
                Credential 标识
                <Input name="name" required />
              </label>
              <label className="grid gap-2 text-sm">
                类型
                <Input name="credential_type" required placeholder="token" />
              </label>
            </>
          )}
          <label className="grid gap-2 text-sm">
            凭据字段分组
            <select
              name="payload_group"
              defaultValue="env"
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            >
              <option value="env">环境变量（env）</option>
              <option value="headers">请求头（headers）</option>
              <option value="oauth">OAuth</option>
            </select>
          </label>
          <label className="grid gap-2 text-sm">
            字段名
            <Input name="payload_field" required autoComplete="off" />
          </label>
          <label className="grid gap-2 text-sm">
            凭据值
            <Input
              name="credential_value"
              required
              type="password"
              autoComplete="new-password"
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
