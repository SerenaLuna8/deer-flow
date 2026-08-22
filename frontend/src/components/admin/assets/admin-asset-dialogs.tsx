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
import type {
  AssetSummary,
  McpVersionInput,
  SkillVersionInput,
} from "@/core/shared-assets";

type VersionedKind = "skills" | "mcp-servers";

export type VersionAuthoringInput = SkillVersionInput | McpVersionInput;

function encodeBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary);
}

function formString(form: FormData, name: string, fallback = ""): string {
  const value = form.get(name);
  return typeof value === "string" ? value : fallback;
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
  const [validationError, setValidationError] = useState<string | null>(null);

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError(null);
    const form = new FormData(event.currentTarget);
    if (kind === "skills") {
      onSubmit({
        files: [
          {
            path: "SKILL.md",
            content_base64: encodeBase64(formString(form, "content")),
            media_type: "text/markdown",
          },
        ],
        expected_asset_version: asset.revision,
      });
      return;
    }
    try {
      const secretSlots = JSON.parse(
        formString(form, "secret_slots", "[]"),
      ) as McpVersionInput["secret_slots"];
      onSubmit({
        description: formString(form, "description"),
        transport: formString(form, "transport", "http") as "http" | "sse",
        command: null,
        args: [],
        url: formString(form, "url").trim(),
        env: {},
        headers: {},
        oauth: {},
        routing: {},
        tool_overrides: {},
        timeout_seconds: 30,
        secret_slots: secretSlots,
        expected_asset_version: asset.revision,
      });
    } catch {
      setValidationError("秘密槽位必须是有效的 JSON 数组。");
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>为 {asset.display_name} 创建版本</DialogTitle>
          <DialogDescription>
            {kind === "skills"
              ? "System Skill v1 不允许升级；Project Skill 可保存新的 Candidate。"
              : "MCP 定义只声明秘密槽位，不在版本定义中保存秘密值。"}
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={submit}>
          {kind === "skills" ? (
            <label className="grid gap-2 text-sm">
              SKILL.md
              <textarea
                name="content"
                required
                className="border-input bg-background min-h-80 rounded-md border p-3 font-mono text-sm"
                defaultValue={`---\nname: ${asset.slug}\ndescription: ${asset.display_name}\n---\n\n# ${asset.display_name}\n`}
              />
            </label>
          ) : (
            <>
              <label className="grid gap-2 text-sm">
                说明
                <Input name="description" />
              </label>
              <div className="grid gap-4 sm:grid-cols-[10rem_1fr]">
                <label className="grid gap-2 text-sm">
                  Transport
                  <select name="transport" className="border-input bg-background h-9 rounded-md border px-3 text-sm">
                    <option value="http">HTTP</option>
                    <option value="sse">SSE</option>
                  </select>
                </label>
                <label className="grid gap-2 text-sm">
                  URL
                  <Input name="url" type="url" required />
                </label>
              </div>
              <label className="grid gap-2 text-sm">
                秘密槽位 JSON
                <textarea
                  name="secret_slots"
                  className="border-input bg-background min-h-36 rounded-md border p-3 font-mono text-sm"
                  defaultValue="[]"
                />
              </label>
            </>
          )}
          {validationError || errorMessage ? (
            <p role="alert" className="text-destructive text-sm">
              {validationError ?? errorMessage}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={pending} onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button type="submit" disabled={pending || (kind === "skills" && asset.scope === "system")}>
              {pending ? "正在保存…" : "保存版本"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
