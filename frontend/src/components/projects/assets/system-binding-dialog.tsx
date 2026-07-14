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
  useUpgradeProjectSystemBinding,
  type AssetKind,
  type AssetListKind,
  type ProjectAssetItem,
} from "@/core/shared-assets";

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

  useEffect(() => {
    if (!open) return;
    setSelectedVersionId(
      item.current_published_version_id ?? item.binding?.version_id ?? "",
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

  function save() {
    if (!selectedVersionId) return;
    if (!item.binding?.enabled) {
      enable.mutate({
        asset_id: item.id,
        version_id: selectedVersionId,
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
          <DialogTitle>管理系统绑定</DialogTitle>
          <DialogDescription>
            {item.display_name}。系统发布新版本不会自动改变当前固定版本。
          </DialogDescription>
        </DialogHeader>
        {history.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : history.error ? (
          <p role="alert" className="text-destructive text-sm">
            {adminAssetErrorMessage(history.error)}
          </p>
        ) : canMove ? (
          <label className="grid gap-2 text-sm">
            固定到已发布版本
            <select
              value={selectedVersionId}
              onChange={(event) => setSelectedVersionId(event.target.value)}
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            >
              <option value="">请选择版本</option>
              {published.map((version) => (
                <option key={version.id} value={version.id}>
                  版本 {version.version_number} · {version.id}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <dl className="grid gap-2 text-sm">
          <div>
            <dt className="text-muted-foreground text-xs">当前固定版本</dt>
            <dd
              className="font-mono text-xs"
              data-testid="binding-pinned-version"
            >
              {item.binding?.version_id ?? "未绑定"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">绑定修订版本</dt>
            <dd data-testid="binding-revision">
              {item.binding?.version ?? "—"}
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
              关闭绑定
            </Button>
          )}
          {canMove && (
            <Button
              type="button"
              disabled={
                pending ||
                !selectedVersionId ||
                (item.binding?.enabled &&
                  item.binding.version_id === selectedVersionId)
              }
              onClick={save}
            >
              {!item.binding?.enabled
                ? "启用并固定"
                : target &&
                    pinned &&
                    target.version_number < pinned.version_number
                  ? "回退固定版本"
                  : "升级固定版本"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
