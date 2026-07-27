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
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAdminAssetVersions,
  useDisableAdminProjectSystemBinding,
  useEnableAdminProjectSystemBinding,
  useRollbackAdminProjectSystemBinding,
  useUpgradeAdminProjectSystemBinding,
  type AssetKind,
  type AssetListKind,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

import { adminAssetErrorMessage } from "./admin-asset-view-model";

const BINDING_KIND: Record<Exclude<AssetListKind, "credentials">, AssetKind> = {
  agents: "agent",
  skills: "skill",
  "mcp-servers": "mcp",
};

export function AdminProjectSystemBindingDialog({
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
  const history = useAdminAssetVersions(accountId, kind, item.id);
  const enable = useEnableAdminProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const upgrade = useUpgradeAdminProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const rollback = useRollbackAdminProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const disable = useDisableAdminProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const [selectedVersionId, setSelectedVersionId] = useState("");
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
  const firstRuntimeBlockReason = useMemo(
    () =>
      kind === "mcp-servers"
        ? (published
            .filter(
              (version) =>
                "mcp_server_id" in version &&
                mcpVersionRuntimeBlockReason(version, item.scope) !== null,
            )
            .map((version) =>
              "mcp_server_id" in version
                ? mcpVersionRuntimeBlockReason(version, item.scope)
                : null,
            )
            .find((reason): reason is string => reason !== null) ?? null)
        : null,
    [item.scope, kind, published],
  );

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
    disable.isPending;
  const error =
    enable.error ?? upgrade.error ?? rollback.error ?? disable.error;
  const target = published.find((version) => version.id === selectedVersionId);
  const pinned = published.find(
    (version) => version.id === item.binding?.version_id,
  );
  const canSubmit =
    !history.isLoading &&
    !history.error &&
    !pending &&
    Boolean(target) &&
    bindablePublished.some((version) => version.id === selectedVersionId) &&
    selectedVersionId !==
      (item.binding?.enabled ? item.binding.version_id : "");

  function save() {
    if (!canSubmit || !target) return;
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
    if (!pinned || target.id === pinned.id) return;
    const variables = {
      assetId: item.id,
      input: {
        version_id: target.id,
        expected_binding_version: item.binding.version,
      },
    };
    if (target.version_number > pinned.version_number) {
      upgrade.mutate(variables);
    } else {
      rollback.mutate(variables);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {item.binding?.enabled ? "切换项目绑定版本" : "启用系统资产"}
          </DialogTitle>
          <DialogDescription>
            {item.display_name}
            。这里只治理当前项目的绑定，不修改 packaged 系统定义或版本。
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
              disabled={history.isFetching}
              onClick={() => void history.refetch()}
            >
              {history.isFetching ? "重试中…" : "重试"}
            </Button>
          </div>
        ) : (
          <label className="grid gap-2 text-sm">
            选择已发布版本
            <select
              aria-label="选择已发布版本"
              value={selectedVersionId}
              onChange={(event) => setSelectedVersionId(event.target.value)}
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            >
              <option value="">请选择版本</option>
              {published.map((version) => (
                <option
                  key={version.id}
                  value={version.id}
                  disabled={
                    kind === "mcp-servers" &&
                    "mcp_server_id" in version &&
                    mcpVersionRuntimeBlockReason(version, item.scope) !== null
                  }
                >
                  版本 {version.version_number}
                  {kind === "mcp-servers" &&
                  "mcp_server_id" in version &&
                  mcpVersionRuntimeBlockReason(version, item.scope) !== null
                    ? "（不可用）"
                    : ""}
                </option>
              ))}
            </select>
            {bindablePublished.length === 0 ? (
              <span className="text-muted-foreground text-xs">
                当前没有可绑定的已发布版本。
              </span>
            ) : null}
          </label>
        )}
        {firstRuntimeBlockReason ? (
          <p role="alert" className="text-destructive text-sm">
            {firstRuntimeBlockReason}
          </p>
        ) : null}
        <p className="text-muted-foreground text-sm">
          当前项目：{pinned ? `版本 ${pinned.version_number}` : "未启用"}
        </p>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {adminAssetErrorMessage(error)}
          </p>
        ) : null}
        <DialogFooter className="gap-2 sm:justify-between">
          {item.binding?.enabled ? (
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
              从当前项目停用
            </Button>
          ) : null}
          <Button type="button" disabled={!canSubmit} onClick={save}>
            {!item.binding?.enabled
              ? "启用到当前项目"
              : target &&
                  pinned &&
                  target.version_number < pinned.version_number
                ? "回退到此版本"
                : "切换到新版本"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
