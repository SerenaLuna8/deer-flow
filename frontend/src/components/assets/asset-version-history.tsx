"use client";

import { ChevronDownIcon } from "lucide-react";
import { useState } from "react";

import { versionWorkflowActions } from "@/components/admin/assets/admin-asset-view-model";
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
import type { AssetListKind, AssetVersion } from "@/core/shared-assets";

import { AssetStatusBadge } from "./asset-status-badge";
import { AssetVersionDiff } from "./asset-version-diff";

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

function versionStatus(version: AssetVersion) {
  return "workflow_status" in version
    ? version.workflow_status
    : version.status;
}

export function AssetVersionHistory({
  kind,
  versions,
  pending = false,
  onPublish,
  onSubmit,
  onApprove,
}: {
  kind: AssetListKind;
  versions: AssetVersion[];
  pending?: boolean;
  onPublish?: (version: AssetVersion) => void;
  onSubmit?: (version: McpVersion) => void;
  onApprove?: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ) => void;
}) {
  const [approvalVersion, setApprovalVersion] = useState<McpVersion | null>(
    null,
  );

  if (versions.length === 0) {
    return <p className="text-muted-foreground text-sm">尚未创建版本。</p>;
  }

  return (
    <>
      <div className="space-y-3">
        {versions.map((version, index) => {
          const isMcp = "mcp_server_id" in version;
          const actions =
            kind === "credentials" || !("workflow_status" in version)
              ? []
              : versionWorkflowActions(
                  kind,
                  version.workflow_status,
                  isMcp && version.credential_slots.length > 0,
                );
          return (
            <details
              key={version.id}
              open={index === 0}
              className="border-border/70 rounded-lg border"
            >
              <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-3">
                <ChevronDownIcon aria-hidden className="size-4 shrink-0" />
                <span className="font-medium">
                  版本 {version.version_number}
                </span>
                <AssetStatusBadge status={versionStatus(version)} />
                <time className="text-muted-foreground ml-auto text-xs">
                  {new Date(version.created_at).toLocaleString("zh-CN")}
                </time>
              </summary>
              <div className="border-border/70 space-y-4 border-t p-3">
                {actions.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {actions.includes("publish") && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={pending}
                        onClick={() => onPublish?.(version)}
                      >
                        发布版本
                      </Button>
                    )}
                    {actions.includes("submit") && isMcp && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={pending}
                        onClick={() => onSubmit?.(version)}
                      >
                        提交审批
                      </Button>
                    )}
                    {actions.includes("approve") && isMcp && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={pending}
                        onClick={() => setApprovalVersion(version)}
                      >
                        批准并发布
                      </Button>
                    )}
                  </div>
                )}
                <AssetVersionDiff
                  previous={versions[index + 1] ?? null}
                  current={version}
                />
              </div>
            </details>
          );
        })}
      </div>

      <Dialog
        open={approvalVersion !== null}
        onOpenChange={(open) => !open && setApprovalVersion(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>批准 MCP 版本</DialogTitle>
            <DialogDescription>
              为每个必需 Credential slot 绑定已存在的 Credential version
              ID。批准成功后版本才会发布。
            </DialogDescription>
          </DialogHeader>
          {approvalVersion && (
            <form
              className="space-y-4"
              onSubmit={(event) => {
                event.preventDefault();
                const form = new FormData(event.currentTarget);
                const bindings = Object.fromEntries(
                  approvalVersion.credential_slots
                    .map((slot) => [
                      slot.name,
                      (() => {
                        const value = form.get(`slot:${slot.name}`);
                        return typeof value === "string" ? value.trim() : "";
                      })(),
                    ])
                    .filter(([, id]) => id !== ""),
                );
                onApprove?.(approvalVersion, bindings);
                setApprovalVersion(null);
              }}
            >
              {approvalVersion.credential_slots.map((slot) => (
                <label key={slot.id} className="grid gap-2 text-sm">
                  {slot.name} Credential version ID
                  <Input
                    name={`slot:${slot.name}`}
                    required={slot.required}
                    autoComplete="off"
                    placeholder={slot.purpose || "UUID"}
                  />
                </label>
              ))}
              <DialogFooter>
                <Button type="submit" disabled={pending}>
                  批准并发布
                </Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
