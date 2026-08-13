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
  SharedAssetApiError,
  type AssetKind,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { resolveMcpCurrentConfiguration } from "@/core/shared-assets/mcp-current";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

const BINDING_KIND: Record<Exclude<AssetListKind, "credentials">, AssetKind> = {
  agents: "agent",
  skills: "skill",
  "mcp-servers": "mcp",
};

type SystemSkillBindingVersion = Pick<
  Extract<AssetVersion, { skill_id: string }>,
  "binding_eligible" | "governance_status" | "workflow_status"
>;

export function systemSkillVersionIsBindable(
  version: SystemSkillBindingVersion,
): boolean {
  return (
    version.workflow_status === "published" &&
    version.governance_status === "active" &&
    version.binding_eligible
  );
}

export function systemSkillVersionIsRevoked(
  version: SystemSkillBindingVersion,
): boolean {
  return version.governance_status === "revoked";
}

export function systemSkillBindingVersionLabel(
  version: SystemSkillBindingVersion & { version_number: number },
): string {
  return `${bindingVersionLabel(version.version_number, "skills")}${
    systemSkillVersionIsRevoked(version) ? "（已撤销，不可绑定）" : ""
  }`;
}

export function isSystemBindingConflict(error: unknown): boolean {
  return (
    error instanceof SharedAssetApiError &&
    error.status === 409 &&
    error.code === "ASSET_CONFLICT"
  );
}

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
  onConflict,
}: {
  accountId: string;
  projectId: string;
  kind: Exclude<AssetListKind, "credentials">;
  item: ProjectAssetItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConflict?: () => void;
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
      published.filter((version) => {
        if (kind === "skills") {
          return "skill_id" in version && systemSkillVersionIsBindable(version);
        }
        return (
          kind !== "mcp-servers" ||
          !("mcp_server_id" in version) ||
          mcpVersionRuntimeBlockReason(version, item.scope) === null
        );
      }),
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
  const currentPublished = published.find(
    (version) => version.id === item.current_published_version_id,
  );
  const defaultUnboundVersionId =
    kind === "skills"
      ? currentPublished &&
        "skill_id" in currentPublished &&
        systemSkillVersionIsBindable(currentPublished)
        ? currentPublished.id
        : ""
      : (item.current_published_version_id ?? "");

  useEffect(() => {
    if (!open) return;
    setSelectedVersionId(
      item.binding?.enabled ? item.binding.version_id : defaultUnboundVersionId,
    );
  }, [
    defaultUnboundVersionId,
    item.binding?.enabled,
    item.binding?.version_id,
    open,
  ]);

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
  const pinnedRevoked = Boolean(
    pinned && "skill_id" in pinned && systemSkillVersionIsRevoked(pinned),
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

  function mutationSucceeded() {
    onOpenChange(false);
  }

  function mutationFailed(mutationError: unknown) {
    if (!isSystemBindingConflict(mutationError)) return;
    setSelectedVersionId("");
    void history.refetch();
    onConflict?.();
  }

  function save() {
    if (!availability.canSubmit || !target) return;
    if (isMcp) {
      syncCurrentMcp.mutate(
        {
          assetId: item.id,
          input: item.binding
            ? { expected_binding_version: item.binding.version }
            : {},
        },
        {
          onSuccess: mutationSucceeded,
          onError: mutationFailed,
        },
      );
      return;
    }
    if (!item.binding?.enabled) {
      enable.mutate(
        {
          asset_id: item.id,
          version_id: target.id,
          ...(item.binding
            ? { expected_binding_version: item.binding.version }
            : {}),
        },
        { onSuccess: mutationSucceeded, onError: mutationFailed },
      );
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
    if (target.version_number > pinned.version_number) {
      upgrade.mutate(input, {
        onSuccess: mutationSucceeded,
        onError: mutationFailed,
      });
    } else {
      rollback.mutate(input, {
        onSuccess: mutationSucceeded,
        onError: mutationFailed,
      });
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!pending) onOpenChange(nextOpen);
      }}
    >
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
              disabled={pending}
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            >
              <option value="">请选择版本</option>
              {published.map((version) => (
                <option
                  key={version.id}
                  value={version.id}
                  disabled={
                    "skill_id" in version &&
                    !systemSkillVersionIsBindable(version)
                  }
                >
                  {"skill_id" in version
                    ? systemSkillBindingVersionLabel(version)
                    : bindingVersionLabel(version.version_number, kind)}
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
        {pinnedRevoked && item.binding?.enabled ? (
          <p role="alert" className="text-destructive text-sm">
            当前项目固定的版本已撤销，不能继续作为新绑定目标。请选择仍可用的发布版本迁移，或从项目停用。
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
                  ? `${bindingVersionLabel(pinned.version_number, kind)}${
                      pinnedRevoked ? "（已撤销）" : ""
                    }`
                  : item.binding
                    ? bindingVersionLabel(undefined, kind)
                    : "未绑定"}
            </dd>
          </div>
        </dl>
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {isSystemBindingConflict(error)
              ? "项目绑定已发生变化，已刷新数据。请根据当前固定版本重新选择。"
              : adminAssetErrorMessage(error)}
          </p>
        )}
        <DialogFooter className="gap-2 sm:justify-between">
          {item.binding?.enabled && (
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={() =>
                disable.mutate(
                  {
                    assetId: item.id,
                    input: { expected_binding_version: item.binding!.version },
                  },
                  {
                    onSuccess: mutationSucceeded,
                    onError: mutationFailed,
                  },
                )
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
