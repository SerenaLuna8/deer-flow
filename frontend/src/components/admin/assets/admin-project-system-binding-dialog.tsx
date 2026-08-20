"use client";

import { useEffect, useMemo, useState } from "react";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
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
import { useI18n } from "@/core/i18n/hooks";
import {
  SharedAssetApiError,
  useAdminAssetVersions,
  useDisableAdminProjectSystemBinding,
  useEnableAdminProjectSystemBinding,
  useRollbackAdminProjectSystemBinding,
  useUpgradeAdminProjectSystemBinding,
  type AssetKind,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

import { adminAssetErrorMessage } from "./admin-asset-view-model";

const BINDING_KIND: Record<Exclude<AssetListKind, "credentials">, AssetKind> = {
  agents: "agent",
  skills: "skill",
  "mcp-servers": "mcp",
};

type AdminSystemSkillBindingVersion = Pick<
  Extract<AssetVersion, { skill_id: string }>,
  "binding_eligible" | "governance_status" | "relation"
>;

export function adminSystemSkillVersionIsBindable(
  version: AdminSystemSkillBindingVersion,
): boolean {
  return (
    version.relation === "current" &&
    version.governance_status === "active" &&
    version.binding_eligible
  );
}

export function isAdminSystemBindingConflict(error: unknown): boolean {
  return (
    error instanceof SharedAssetApiError &&
    error.status === 409 &&
    error.code === "ASSET_CONFLICT"
  );
}

export function AdminProjectSystemBindingDialog({
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
  const { t } = useI18n();
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
    "mcp",
  );
  const rollback = useRollbackAdminProjectSystemBinding(
    accountId,
    projectId,
    "mcp",
  );
  const disable = useDisableAdminProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const published = useMemo(
    () =>
      (history.data?.data ?? []).filter((version) =>
        kind === "mcp-servers"
          ? "workflow_status" in version &&
            version.workflow_status === "published"
          : "relation" in version && version.relation === "current",
      ),
    [history.data, kind],
  );
  const bindablePublished = useMemo(
    () =>
      published.filter((version) => {
        if (kind === "skills") {
          return (
            "skill_id" in version && adminSystemSkillVersionIsBindable(version)
          );
        }
        return (
          kind !== "mcp-servers" ||
          !("mcp_server_id" in version) ||
          mcpVersionRuntimeBlockReason(
            version,
            item.scope,
            t.adminAssets.runtime,
          ) === null
        );
      }),
    [item.scope, kind, published, t.adminAssets.runtime],
  );
  const firstRuntimeBlockReason = useMemo(
    () =>
      kind === "mcp-servers"
        ? (published
            .filter(
              (version) =>
                "mcp_server_id" in version &&
                mcpVersionRuntimeBlockReason(
                  version,
                  item.scope,
                  t.adminAssets.runtime,
                ) !== null,
            )
            .map((version) =>
              "mcp_server_id" in version
                ? mcpVersionRuntimeBlockReason(
                    version,
                    item.scope,
                    t.adminAssets.runtime,
                  )
                : null,
            )
            .find((reason): reason is string => reason !== null) ?? null)
        : null,
    [item.scope, kind, published, t.adminAssets.runtime],
  );
  const currentPublished = published.find(
    (version) => version.id === item.current_version_id,
  );
  const defaultUnboundVersionId =
    kind === "skills"
      ? currentPublished &&
        "skill_id" in currentPublished &&
        adminSystemSkillVersionIsBindable(currentPublished)
        ? currentPublished.id
        : ""
      : (item.current_version_id ?? "");

  useEffect(() => {
    if (!open) return;
    setSelectedVersionId(
      item.binding?.enabled
        ? item.binding.current_version_id
        : defaultUnboundVersionId,
    );
  }, [
    defaultUnboundVersionId,
    item.binding?.enabled,
    item.binding?.current_version_id,
    open,
  ]);

  const pending =
    enable.isPending ||
    upgrade.isPending ||
    rollback.isPending ||
    disable.isPending;
  const error =
    enable.error ?? upgrade.error ?? rollback.error ?? disable.error;
  const target = published.find((version) => version.id === selectedVersionId);
  const pinned = published.find(
    (version) => version.id === item.binding?.current_version_id,
  );
  const pinnedRevoked = Boolean(
    pinned && "skill_id" in pinned && pinned.governance_status === "revoked",
  );
  const canSubmit =
    !history.isLoading &&
    !history.error &&
    !pending &&
    Boolean(target) &&
    bindablePublished.some((version) => version.id === selectedVersionId) &&
    selectedVersionId !==
      (item.binding?.enabled ? item.binding.current_version_id : "");

  function mutationSucceeded() {
    onOpenChange(false);
  }

  function mutationFailed(mutationError: unknown) {
    if (!isAdminSystemBindingConflict(mutationError)) return;
    setSelectedVersionId("");
    void history.refetch();
    onConflict?.();
  }

  function save() {
    if (!canSubmit || !target) return;
    if (!item.binding?.enabled) {
      enable.mutate(
        {
          asset_id: item.id,
          ...(kind === "mcp-servers" ? { version_id: target.id } : {}),
          ...(item.binding
            ? { expected_binding_version: item.binding.version }
            : {}),
        },
        { onSuccess: mutationSucceeded, onError: mutationFailed },
      );
      return;
    }
    if (kind !== "mcp-servers") return;
    if (!pinned || target.id === pinned.id) return;
    const variables = {
      assetId: item.id,
      input: {
        version_id: target.id,
        expected_binding_version: item.binding.version,
      },
    };
    if (target.version_number > pinned.version_number) {
      upgrade.mutate(variables, {
        onSuccess: mutationSucceeded,
        onError: mutationFailed,
      });
    } else {
      rollback.mutate(variables, {
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
      <DialogContent
        closeLabel={t.adminOperations.ui.close}
        className="sm:max-w-2xl"
      >
        <DialogHeader className="min-w-0 pr-8">
          <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <DialogTitle>
                {item.binding?.enabled
                  ? t.adminAssets.dialogs.binding.switchTitle
                  : t.adminAssets.dialogs.binding.enableTitle}
              </DialogTitle>
              <DialogDescription className="mt-1.5 min-w-0 [overflow-wrap:anywhere]">
                {t.adminAssets.dialogs.binding.description(item.display_name)}
              </DialogDescription>
            </div>
            <AssetStatusBadge status={item.status} />
          </div>
        </DialogHeader>
        <div
          data-testid="admin-project-binding-summary"
          className="grid min-w-0 gap-3 sm:grid-cols-2"
        >
          <div className="border-border/70 bg-muted/20 min-w-0 rounded-lg border p-3">
            <p className="text-muted-foreground text-xs font-medium">
              {t.adminAssets.catalog.bindingStatus}
            </p>
            <p className="mt-1.5 text-sm font-semibold">
              {pinned
                ? `${t.adminAssets.version.number(pinned.version_number)}${
                    pinnedRevoked ? ` · ${t.adminAssets.status.revoked}` : ""
                  }`
                : t.adminAssets.dialogs.binding.notEnabled}
            </p>
            <p className="text-muted-foreground mt-1 min-w-0 font-mono text-xs [overflow-wrap:anywhere]">
              {item.binding?.current_version_id ?? t.adminAssets.catalog.none}
            </p>
          </div>
          <div className="border-primary/20 bg-primary/5 min-w-0 rounded-lg border p-3">
            <p className="text-muted-foreground text-xs font-medium">
              {t.adminAssets.dialogs.binding.selectPublished}
            </p>
            <p className="mt-1.5 text-sm font-semibold">
              {target
                ? t.adminAssets.version.number(target.version_number)
                : t.adminAssets.dialogs.binding.selectPlaceholder}
            </p>
            <p className="text-muted-foreground mt-1 min-w-0 font-mono text-xs [overflow-wrap:anywhere]">
              {target?.id ?? t.adminAssets.catalog.none}
            </p>
          </div>
        </div>
        {history.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-36" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : history.error ? (
          <div className="border-destructive/30 bg-destructive/5 space-y-3 rounded-lg border p-3">
            <p role="alert" className="text-destructive text-sm">
              {adminAssetErrorMessage(history.error, t.adminAssets.errors)}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={history.isFetching}
              onClick={() => void history.refetch()}
            >
              {history.isFetching
                ? t.adminAssets.common.retrying
                : t.adminAssets.common.retry}
            </Button>
          </div>
        ) : (
          <label className="grid min-w-0 gap-2 text-sm font-medium">
            <span>
              {kind === "mcp-servers"
                ? t.adminAssets.dialogs.binding.selectPublished
                : "当前版本"}
            </span>
            <select
              aria-label={
                kind === "mcp-servers"
                  ? t.adminAssets.dialogs.binding.selectPublishedAria
                  : "当前版本"
              }
              value={selectedVersionId}
              onChange={(event) => setSelectedVersionId(event.target.value)}
              disabled={pending}
              className="border-input bg-background h-10 min-w-0 rounded-md border px-3 text-sm font-normal disabled:opacity-60"
            >
              <option value="">
                {t.adminAssets.dialogs.binding.selectPlaceholder}
              </option>
              {published.map((version) => (
                <option
                  key={version.id}
                  value={version.id}
                  disabled={
                    (kind === "skills" &&
                      "skill_id" in version &&
                      !adminSystemSkillVersionIsBindable(version)) ||
                    (kind === "mcp-servers" &&
                      "mcp_server_id" in version &&
                      mcpVersionRuntimeBlockReason(
                        version,
                        item.scope,
                        t.adminAssets.runtime,
                      ) !== null)
                  }
                >
                  {t.adminAssets.version.number(version.version_number)}
                  {kind === "mcp-servers" &&
                  "mcp_server_id" in version &&
                  mcpVersionRuntimeBlockReason(
                    version,
                    item.scope,
                    t.adminAssets.runtime,
                  ) !== null
                    ? t.adminAssets.dialogs.binding.unavailableSuffix
                    : ""}
                  {kind === "skills" &&
                  "skill_id" in version &&
                  version.governance_status === "revoked"
                    ? ` · ${t.adminAssets.status.revoked}${t.adminAssets.dialogs.binding.unavailableSuffix}`
                    : ""}
                </option>
              ))}
            </select>
            {bindablePublished.length === 0 ? (
              <span className="text-muted-foreground text-xs font-normal">
                {t.adminAssets.dialogs.binding.noBindableVersions}
              </span>
            ) : null}
          </label>
        )}
        {firstRuntimeBlockReason ? (
          <p
            role="alert"
            className="border-destructive/30 bg-destructive/5 text-destructive rounded-lg border px-3 py-2 text-sm"
          >
            {firstRuntimeBlockReason}
          </p>
        ) : null}
        {pinnedRevoked && item.binding?.enabled ? (
          <p
            role="alert"
            className="border-destructive/30 bg-destructive/5 text-destructive rounded-lg border px-3 py-2 text-sm"
          >
            {t.adminAssets.status.revoked}:{" "}
            {t.adminAssets.dialogs.binding.selectPublished} /{" "}
            {t.adminAssets.dialogs.binding.disable}
          </p>
        ) : null}
        {error ? (
          <p
            role="alert"
            className="border-destructive/30 bg-destructive/5 text-destructive rounded-lg border px-3 py-2 text-sm"
          >
            {isAdminSystemBindingConflict(error)
              ? t.adminAssets.errors.conflict
              : adminAssetErrorMessage(error, t.adminAssets.errors)}
          </p>
        ) : null}
        <DialogFooter className="border-border/70 gap-2 border-t pt-4 sm:justify-between">
          {item.binding?.enabled ? (
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
              {t.adminAssets.dialogs.binding.disable}
            </Button>
          ) : null}
          <Button type="button" disabled={!canSubmit} onClick={save}>
            {!item.binding?.enabled
              ? t.adminAssets.dialogs.binding.enable
              : kind === "mcp-servers" &&
                  target &&
                  pinned &&
                  target.version_number < pinned.version_number
                ? t.adminAssets.dialogs.binding.rollback
                : kind === "mcp-servers"
                  ? t.adminAssets.dialogs.binding.switchVersion
                  : "已使用当前版本"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
