"use client";

import { ChevronDownIcon } from "lucide-react";

import { versionWorkflowActions } from "@/components/admin/assets/admin-asset-view-model";
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

function versionStatus(version: AssetVersion) {
  if (
    "governance_status" in version &&
    version.governance_status === "revoked"
  ) {
    return "revoked";
  }
  return "workflow_status" in version
    ? version.workflow_status
    : version.relation;
}

export function AssetVersionHistory({
  kind,
  scope,
  versions,
  pending = false,
  onActivate,
  onPublish,
  currentVersionId,
}: {
  kind: Exclude<AssetListKind, "agents">;
  scope: AssetScope;
  versions: AssetVersion[];
  pending?: boolean;
  onActivate?: (version: AssetVersion) => void;
  onPublish?: (version: AssetVersion) => void;
  currentVersionId?: string | null;
}) {
  const { locale, t } = useI18n();

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
        {t.adminAssets.version.currentUnconfirmed}
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
    <div className="space-y-3">
      {displayedVersions.map((version, index) => {
        const isMcp = "mcp_server_id" in version;
        const runtimeBlockReason = isMcp
          ? mcpVersionRuntimeBlockReason(version, scope, t.adminAssets.runtime)
          : null;
        const actions = !("workflow_status" in version)
          ? []
          : versionWorkflowActions(
              kind,
              version.workflow_status,
              isMcp && version.secret_slots.length > 0,
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
              </div>
            )}
            {!isMcp &&
            "relation" in version &&
            version.relation === "candidate" &&
            onActivate ? (
              <Button
                type="button"
                size="sm"
                disabled={pending}
                onClick={() => onActivate(version)}
              >
                {t.adminAssets.version.activate}
              </Button>
            ) : null}
            {runtimeBlockReason ? (
              <p role="alert" className="text-destructive text-sm">
                {runtimeBlockReason}
              </p>
            ) : null}
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
  );
}
