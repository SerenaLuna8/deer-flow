"use client";

import { ChevronDownIcon } from "lucide-react";
import { useState } from "react";

import { versionWorkflowActions } from "@/components/admin/assets/admin-asset-view-model";
import {
  McpApprovalDialog,
  type CredentialVersionOption,
} from "@/components/projects/assets/mcp-approval-dialog";
import { Button } from "@/components/ui/button";
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
  approvalCredentials = [],
  approvalCredentialScope = "project",
  approvalCredentialsLoading = false,
  approvalCredentialsError,
  onRetryApprovalCredentials,
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
  approvalCredentials?: CredentialVersionOption[];
  approvalCredentialScope?: "system" | "project";
  approvalCredentialsLoading?: boolean;
  approvalCredentialsError?: unknown;
  onRetryApprovalCredentials?: () => void;
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
                    {actions.includes("publish") && onPublish && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={pending}
                        onClick={() => onPublish?.(version)}
                      >
                        发布版本
                      </Button>
                    )}
                    {actions.includes("submit") && isMcp && onSubmit && (
                      <Button
                        type="button"
                        size="sm"
                        disabled={pending}
                        onClick={() => onSubmit?.(version)}
                      >
                        提交审批
                      </Button>
                    )}
                    {actions.includes("approve") && isMcp && onApprove && (
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

      <McpApprovalDialog
        version={approvalVersion}
        open={approvalVersion !== null}
        pending={pending}
        credentials={approvalCredentials}
        credentialScope={approvalCredentialScope}
        credentialsLoading={approvalCredentialsLoading}
        credentialsError={approvalCredentialsError}
        onRetryCredentials={onRetryApprovalCredentials}
        onOpenChange={(open) => !open && setApprovalVersion(null)}
        onApprove={(version, bindings) => onApprove?.(version, bindings)}
      />
    </>
  );
}
