"use client";

import { useEffect, useMemo, useState } from "react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useProjectAssetVersions,
  useRollbackProjectSystemBinding,
  useSyncCurrentProjectSystemMcpBinding,
  useUpgradeProjectSystemBinding,
  type AssetKind,
  type AssetListKind,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { resolveMcpCurrentConfiguration } from "@/core/shared-assets/mcp-current";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

const BINDING_KIND: Record<Exclude<AssetListKind, "credentials">, AssetKind> = {
  agents: "agent",
  skills: "skill",
  "mcp-servers": "mcp",
};

export function canMoveSystemBinding(
  item: Pick<ProjectAssetItem, "status">,
): boolean {
  return item.status === "active";
}

export function bindingVersionLabel(
  versionNumber?: number,
  kind: Exclude<AssetListKind, "credentials"> = "skills",
): string {
  if (kind === "mcp-servers") {
    return "已启用";
  }
  return versionNumber === undefined ? "已固定版本" : `版本 ${versionNumber}`;
}

export function systemMcpBindingStatus(
  item: Pick<ProjectAssetItem, "binding" | "current_published_version_id">,
): "未启用" | "已启用" | "有配置更新" {
  if (!item.binding?.enabled) return "未启用";
  return item.current_published_version_id &&
    item.binding.version_id !== item.current_published_version_id
    ? "有配置更新"
    : "已启用";
}

export function systemBindingDialogAvailability({
  historyLoading,
  historyError,
  historyRetryPending,
  mutationPending,
  selectedVersionId,
  publishedVersionIds,
  boundVersionId,
}: {
  historyLoading: boolean;
  historyError: boolean;
  historyRetryPending: boolean;
  mutationPending: boolean;
  selectedVersionId: string;
  publishedVersionIds: readonly string[];
  boundVersionId?: string | null;
}) {
  const hasSelectedPublishedTarget =
    selectedVersionId !== "" && publishedVersionIds.includes(selectedVersionId);
  return {
    canSubmit:
      !historyLoading &&
      !historyError &&
      !mutationPending &&
      hasSelectedPublishedTarget &&
      selectedVersionId !== boundVersionId,
    canRetryHistory: historyError && !historyRetryPending,
    hasSelectedPublishedTarget,
  };
}

export function SystemBindingDialog({
  accountId,
  projectId,
  kind,
  item,
  open,
  onOpenChange,
}: {
  accountId: string;
  projectId: string;
  kind: Exclude<AssetListKind, "credentials">;
  item: ProjectAssetItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const assetKind = BINDING_KIND[kind];
  const isMcp = kind === "mcp-servers";
  const history = useProjectAssetVersions(accountId, projectId, kind, item.id);
  const enable = useEnableProjectSystemBinding(accountId, projectId, assetKind);
  const upgrade = useUpgradeProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const rollback = useRollbackProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const disable = useDisableProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const syncCurrentMcp = useSyncCurrentProjectSystemMcpBinding(
    accountId,
    projectId,
  );
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const canMove = canMoveSystemBinding(item);

  const published = useMemo(
    () =>
      (history.data?.data ?? []).filter(
        (version) =>
          "workflow_status" in version &&
          version.workflow_status === "published",
      ),
    [history.data],
  );
  const bindablePublished = useMemo(
    () =>
      published.filter(
        (version) =>
          kind !== "mcp-servers" ||
          !("mcp_server_id" in version) ||
          mcpVersionRuntimeBlockReason(version, item.scope) === null,
      ),
    [item.scope, kind, published],
  );
  const mcpConfiguration = isMcp
    ? resolveMcpCurrentConfiguration(
        history.data?.data ?? [],
        "system",
        item.current_published_version_id,
      )
    : null;
  const currentMcpVersion = mcpConfiguration?.version ?? null;
  const currentMcpRuntimeBlockReason =
    currentMcpVersion && "mcp_server_id" in currentMcpVersion
      ? mcpVersionRuntimeBlockReason(currentMcpVersion, item.scope)
      : null;
  const firstRuntimeBlockReason = isMcp ? currentMcpRuntimeBlockReason : null;

  useEffect(() => {
    if (!open) return;
    setSelectedVersionId(
      item.binding?.enabled
        ? item.binding.version_id
        : (item.current_published_version_id ?? ""),
    );
  }, [item, open]);

  const pending =
    enable.isPending ||
    upgrade.isPending ||
    rollback.isPending ||
    disable.isPending ||
    syncCurrentMcp.isPending;
  const error =
    syncCurrentMcp.error ??
    enable.error ??
    upgrade.error ??
    rollback.error ??
    disable.error;
  const effectiveSelectedVersionId = isMcp
    ? (currentMcpVersion?.id ?? "")
    : selectedVersionId;
  const target = published.find(
    (version) => version.id === effectiveSelectedVersionId,
  );
  const pinned = published.find(
    (version) => version.id === item.binding?.version_id,
  );
  const availability = systemBindingDialogAvailability({
    historyLoading: history.isLoading,
    historyError: Boolean(history.error),
    historyRetryPending: history.isFetching,
    mutationPending: pending,
    selectedVersionId: effectiveSelectedVersionId,
    publishedVersionIds: isMcp
      ? currentMcpRuntimeBlockReason === null && currentMcpVersion
        ? [currentMcpVersion.id]
        : []
      : bindablePublished.map((version) => version.id),
    boundVersionId: item.binding?.enabled ? item.binding.version_id : null,
  });

  function save() {
    if (!availability.canSubmit || !target) return;
    if (isMcp) {
      syncCurrentMcp.mutate({
        assetId: item.id,
        input: item.binding
          ? { expected_binding_version: item.binding.version }
          : {},
      });
      return;
    }
    if (!item.binding?.enabled) {
      enable.mutate({
        asset_id: item.id,
        version_id: target.id,
        ...(item.binding
          ? { expected_binding_version: item.binding.version }
          : {}),
      });
      return;
    }
    if (!target || !pinned || target.id === pinned.id) return;
    const input = {
      assetId: item.id,
      input: {
        version_id: target.id,
        expected_binding_version: item.binding.version,
      },
    };
    if (target.version_number > pinned.version_number) upgrade.mutate(input);
    else rollback.mutate(input);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {isMcp
              ? "项目使用"
              : item.binding?.enabled
                ? "切换启用版本"
                : "启用到项目"}
          </DialogTitle>
          <DialogDescription>
            {item.display_name}
            {isMcp
              ? ""
              : "。项目会固定使用你选择的发布版本，系统更新不会自动切换。"}
          </DialogDescription>
        </DialogHeader>
        {history.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : history.error ? (
          <div className="space-y-3">
            <p role="alert" className="text-destructive text-sm">
              {adminAssetErrorMessage(history.error)}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              data-testid="system-binding-history-retry"
              disabled={!availability.canRetryHistory}
              onClick={() => void history.refetch()}
            >
              {history.isFetching ? "重试中…" : "重试"}
            </Button>
          </div>
        ) : canMove && !isMcp ? (
          <label className="grid gap-2 text-sm">
            选择已发布版本
            <select
              value={selectedVersionId}
              onChange={(event) => setSelectedVersionId(event.target.value)}
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            >
              <option value="">请选择版本</option>
              {published.map((version) => (
                <option key={version.id} value={version.id}>
                  {bindingVersionLabel(version.version_number, kind)}
                </option>
              ))}
            </select>
            {bindablePublished.length === 0 && (
              <span className="text-muted-foreground text-xs">
                当前没有可启用的已发布版本。
              </span>
            )}
          </label>
        ) : null}
        {isMcp && !history.isLoading && !history.error ? (
          mcpConfiguration?.state === "unconfirmed" ? (
            <p role="alert" className="text-destructive text-sm">
              当前配置无法确认
            </p>
          ) : mcpConfiguration?.state === "empty" ||
            currentMcpRuntimeBlockReason ? (
            <p className="text-muted-foreground text-sm">
              当前没有可启用的已发布配置。
            </p>
          ) : null
        ) : null}
        {firstRuntimeBlockReason ? (
          <p role="alert" className="text-destructive text-sm">
            {firstRuntimeBlockReason}
          </p>
        ) : null}
        <dl className="grid gap-2 text-sm">
          <div>
            <dt className="text-muted-foreground text-xs">
              当前项目{isMcp ? "配置" : "版本"}
            </dt>
            <dd data-testid="binding-pinned-version">
              {isMcp
                ? systemMcpBindingStatus(item)
                : pinned
                  ? bindingVersionLabel(pinned.version_number, kind)
                  : item.binding
                    ? bindingVersionLabel(undefined, kind)
                    : "未绑定"}
            </dd>
          </div>
        </dl>
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {adminAssetErrorMessage(error)}
          </p>
        )}
        <DialogFooter className="gap-2 sm:justify-between">
          {item.binding?.enabled && (
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={() =>
                disable.mutate({
                  assetId: item.id,
                  input: { expected_binding_version: item.binding!.version },
                })
              }
            >
              从项目停用
            </Button>
          )}
          {canMove && (!isMcp || availability.canSubmit) && (
            <Button
              type="button"
              data-testid={isMcp ? "system-mcp-binding-submit" : undefined}
              disabled={!availability.canSubmit}
              onClick={save}
            >
              {isMcp
                ? item.binding?.enabled
                  ? "更新到当前配置"
                  : "启用当前配置"
                : !item.binding?.enabled
                  ? "启用到项目"
                  : target &&
                      pinned &&
                      target.version_number < pinned.version_number
                    ? "回退到此版本"
                    : "切换到新版本"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
