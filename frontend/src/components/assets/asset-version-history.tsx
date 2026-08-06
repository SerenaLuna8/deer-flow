"use client";

import { ChevronDownIcon } from "lucide-react";
import { useState } from "react";

import { versionWorkflowActions } from "@/components/admin/assets/admin-asset-view-model";
import {
  McpApprovalDialog,
  type CredentialVersionOption,
} from "@/components/projects/assets/mcp-approval-dialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type {
  AssetListKind,
  AssetScope,
  AssetVersion,
} from "@/core/shared-assets";
import { resolveMcpCurrentConfiguration } from "@/core/shared-assets/mcp-current";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

import { AssetStatusBadge } from "./asset-status-badge";
import { AssetVersionDiff } from "./asset-version-diff";

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

export function activeCredentialGrantVersions(
  version: McpVersion,
): Record<string, number> {
  const slotNames = new Map(
    version.credential_slots.map((slot) => [slot.id, slot.name]),
  );
  return Object.fromEntries(
    version.credential_grants
      .filter((grant) => grant.status === "active")
      .map((grant) => {
        const slotName = slotNames.get(grant.credential_slot_id);
        return slotName ? ([slotName, grant.version] as const) : null;
      })
      .filter((entry): entry is readonly [string, number] => entry !== null),
  );
}

function versionStatus(version: AssetVersion) {
  return "workflow_status" in version
    ? version.workflow_status
    : version.status;
}

export function AssetVersionHistory({
  kind,
  scope,
  versions,
  pending = false,
  onPublish,
  onSubmit,
  onApprove,
  onConfigureCredentialGrants,
  approvalCredentials = [],
  approvalCredentialScope = "project",
  approvalCredentialsLoading = false,
  approvalCredentialsError,
  approvalError,
  configureCredentialGrantsVersionId,
  currentVersionId,
  onRetryApprovalCredentials,
}: {
  kind: AssetListKind;
  scope: AssetScope;
  versions: AssetVersion[];
  pending?: boolean;
  onPublish?: (version: AssetVersion) => void;
  onSubmit?: (version: McpVersion) => void;
  onApprove?: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ) => boolean | void | Promise<boolean | void>;
  onConfigureCredentialGrants?: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
    expectedActiveGrantVersions: Record<string, number>,
  ) => boolean | void | Promise<boolean | void>;
  approvalCredentials?: CredentialVersionOption[];
  approvalCredentialScope?: "system" | "project";
  approvalCredentialsLoading?: boolean;
  approvalCredentialsError?: unknown;
  approvalError?: unknown;
  configureCredentialGrantsVersionId?: string | null;
  currentVersionId?: string | null;
  onRetryApprovalCredentials?: () => void;
}) {
  const { locale, t } = useI18n();
  const [approvalVersion, setApprovalVersion] = useState<McpVersion | null>(
    null,
  );
  const [approvalMode, setApprovalMode] = useState<
    "publish" | "configure-grants"
  >("publish");

  if (versions.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        {kind === "mcp-servers"
          ? t.adminAssets.version.mcpNone
          : t.adminAssets.version.none}
      </p>
    );
  }
  const mcpConfiguration =
    kind === "mcp-servers"
      ? resolveMcpCurrentConfiguration(versions, scope, currentVersionId)
      : null;
  if (kind === "mcp-servers" && mcpConfiguration?.state === "unconfirmed") {
    return (
      <p role="alert" className="text-destructive text-sm">
        当前配置无法确认
      </p>
    );
  }
  const displayedVersions =
    kind === "mcp-servers"
      ? mcpConfiguration?.version
        ? [mcpConfiguration.version]
        : []
      : versions;

  return (
    <>
      <div className="space-y-3">
        {displayedVersions.map((version, index) => {
          const isMcp = "mcp_server_id" in version;
          const runtimeBlockReason = isMcp
            ? mcpVersionRuntimeBlockReason(
                version,
                scope,
                t.adminAssets.runtime,
              )
            : null;
          const actions =
            kind === "credentials" || !("workflow_status" in version)
              ? []
              : versionWorkflowActions(
                  kind,
                  version.workflow_status,
                  isMcp && version.credential_slots.length > 0,
                );
          const detail = (
            <div
              className={
                isMcp
                  ? "space-y-4 p-3"
                  : "border-border/70 space-y-4 border-t p-3"
              }
            >
              {actions.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {actions.includes("publish") && onPublish && (
                    <Button
                      type="button"
                      size="sm"
                      disabled={pending || Boolean(runtimeBlockReason)}
                      title={runtimeBlockReason ?? undefined}
                      onClick={() => onPublish?.(version)}
                    >
                      {isMcp
                        ? t.adminAssets.version.publishMcp
                        : t.adminAssets.version.publish}
                    </Button>
                  )}
                  {actions.includes("submit") && isMcp && onSubmit && (
                    <Button
                      type="button"
                      size="sm"
                      disabled={pending || Boolean(runtimeBlockReason)}
                      title={runtimeBlockReason ?? undefined}
                      onClick={() => onSubmit?.(version)}
                    >
                      {t.adminAssets.version.submit}
                    </Button>
                  )}
                  {actions.includes("approve") && isMcp && onApprove && (
                    <Button
                      type="button"
                      size="sm"
                      disabled={pending || Boolean(runtimeBlockReason)}
                      title={runtimeBlockReason ?? undefined}
                      onClick={() => setApprovalVersion(version)}
                    >
                      {t.adminAssets.version.approveMcp}
                    </Button>
                  )}
                </div>
              )}
              {runtimeBlockReason ? (
                <p role="alert" className="text-destructive text-sm">
                  {runtimeBlockReason}
                </p>
              ) : null}
              {isMcp &&
                version.workflow_status === "published" &&
                version.id === configureCredentialGrantsVersionId &&
                version.credential_slots.length > 0 &&
                onConfigureCredentialGrants && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={pending}
                    onClick={() => {
                      setApprovalMode("configure-grants");
                      setApprovalVersion(version);
                    }}
                  >
                    {t.adminAssets.version.configureGrants}
                  </Button>
                )}
              <AssetVersionDiff
                previous={isMcp ? null : (displayedVersions[index + 1] ?? null)}
                current={version}
              />
            </div>
          );
          if (isMcp) {
            return (
              <div
                key={version.id}
                className="border-border/70 rounded-lg border"
              >
                <div className="flex items-center gap-3 px-3 pt-3">
                  <AssetStatusBadge status={versionStatus(version)} />
                  <time className="text-muted-foreground ml-auto text-xs">
                    {new Date(version.created_at).toLocaleString(locale)}
                  </time>
                </div>
                {detail}
              </div>
            );
          }
          return (
            <details
              key={version.id}
              open={index === 0}
              className="border-border/70 rounded-lg border"
            >
              <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-3">
                <ChevronDownIcon aria-hidden className="size-4 shrink-0" />
                <span className="font-medium">
                  {t.adminAssets.version.number(version.version_number)}
                </span>
                <AssetStatusBadge status={versionStatus(version)} />
                <time className="text-muted-foreground ml-auto text-xs">
                  {new Date(version.created_at).toLocaleString(locale)}
                </time>
              </summary>
              {detail}
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
        approvalError={approvalError}
        onRetryCredentials={onRetryApprovalCredentials}
        mode={approvalMode}
        onOpenChange={(open) => {
          if (!open) {
            setApprovalVersion(null);
            setApprovalMode("publish");
          }
        }}
        onApprove={(version, bindings) => {
          if (approvalMode === "configure-grants") {
            return onConfigureCredentialGrants?.(
              version,
              bindings,
              activeCredentialGrantVersions(version),
            );
          }
          return onApprove?.(version, bindings);
        }}
      />
    </>
  );
}
