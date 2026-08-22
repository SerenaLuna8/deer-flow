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
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import type {
  AssetSummary,
  McpVersionInput,
  SkillVersionInput,
} from "@/core/shared-assets";

type VersionedKind = "skills" | "mcp-servers";

export type VersionAuthoringInput = SkillVersionInput | McpVersionInput;

export function createVersionDialogCopy(
  t: Translations,
  kind: VersionedKind,
  assetName: string,
) {
  const copy = t.adminAssets.dialogs.authoring;
  return {
    title: copy.title(assetName),
    description:
      kind === "skills" ? copy.skillDescription : copy.mcpDescription,
    fieldDescription: copy.description,
    secretSlots: copy.secretSlots,
    invalidSecretSlots: copy.invalidSecretSlots,
    cancel: copy.cancel,
    saving: copy.saving,
    save: copy.save,
  };
}

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
  const { t } = useI18n();
  const copy = createVersionDialogCopy(t, kind, asset.display_name);
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
      setValidationError(copy.invalidSecretSlots);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
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
                {copy.fieldDescription}
                <Input name="description" />
              </label>
              <div className="grid gap-4 sm:grid-cols-[10rem_1fr]">
                <label className="grid gap-2 text-sm">
                  Transport
                  <select
                    name="transport"
                    className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                  >
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
                {copy.secretSlots}
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
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={() => onOpenChange(false)}
            >
              {copy.cancel}
            </Button>
            <Button
              type="submit"
              disabled={
                pending || (kind === "skills" && asset.scope === "system")
              }
            >
              {pending ? copy.saving : copy.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
