"use client";

import { useMemo } from "react";

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
  SharedAssetApiError,
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useProjectAssetVersions,
  useSyncCurrentProjectSystemMcpBinding,
  type AssetKind,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { resolveMcpCurrentConfiguration } from "@/core/shared-assets/mcp-current";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

const BINDING_KIND: Record<AssetListKind, AssetKind> = {
  agents: "agent",
  skills: "skill",
  "mcp-servers": "mcp",
};

type SystemSkillBindingVersion = Pick<
  Extract<AssetVersion, { skill_id: string }>,
  "binding_eligible" | "governance_status" | "relation"
>;

export function systemSkillVersionIsBindable(
  version: SystemSkillBindingVersion,
): boolean {
  return (
    version.relation === "current" &&
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
    systemSkillVersionIsRevoked(version) ? "（已撤销，不可启用）" : ""
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
  kind: AssetListKind = "skills",
): string {
  if (kind === "mcp-servers") return "已启用";
  return versionNumber === undefined
    ? "当前版本"
    : `当前版本 v${versionNumber}`;
}

export function systemMcpBindingStatus(
  item: Pick<ProjectAssetItem, "binding" | "current_version_id">,
): "未启用" | "已启用" | "有配置更新" {
  if (!item.binding?.enabled) return "未启用";
  return item.current_version_id &&
    item.binding.current_version_id !== item.current_version_id
    ? "有配置更新"
    : "已启用";
}

export function systemBindingDialogAvailability({
  historyLoading,
  historyError,
  historyRetryPending,
  mutationPending,
  selectedVersionId,
  eligibleVersionIds,
  boundVersionId,
}: {
  historyLoading: boolean;
  historyError: boolean;
  historyRetryPending: boolean;
  mutationPending: boolean;
  selectedVersionId: string;
  eligibleVersionIds: readonly string[];
  boundVersionId?: string | null;
}) {
  const hasSelectedEligibleTarget =
    selectedVersionId !== "" && eligibleVersionIds.includes(selectedVersionId);
  return {
    canSubmit:
      !historyLoading &&
      !historyError &&
      !mutationPending &&
      hasSelectedEligibleTarget &&
      selectedVersionId !== boundVersionId,
    canRetryHistory: historyError && !historyRetryPending,
    hasSelectedEligibleTarget,
  };
}

type SystemBindingDialogProps = {
  accountId: string;
  projectId: string;
  kind: AssetListKind;
  item: ProjectAssetItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConflict?: () => void;
};

function AgentSystemBindingDialog({
  accountId,
  projectId,
  item,
  open,
  onOpenChange,
  onConflict,
}: Omit<SystemBindingDialogProps, "kind">) {
  const enable = useEnableProjectSystemBinding(accountId, projectId, "agent");
  const disable = useDisableProjectSystemBinding(accountId, projectId, "agent");
  const pending = enable.isPending || disable.isPending;
  const error = enable.error ?? disable.error;
  const canEnable =
    item.status === "active" &&
    Boolean(item.definition_id) &&
    item.binding?.enabled !== true;
  const mutationSucceeded = () => onOpenChange(false);
  const mutationFailed = (mutationError: unknown) => {
    if (isSystemBindingConflict(mutationError)) onConflict?.();
  };

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
            {item.binding?.enabled ? "项目使用状态" : "启用到项目"}
          </DialogTitle>
          <DialogDescription>
            {item.display_name}。项目使用该系统 Agent 的唯一只读 Definition。
          </DialogDescription>
        </DialogHeader>
        <dl className="grid gap-2 text-sm">
          <div>
            <dt className="text-muted-foreground text-xs">项目状态</dt>
            <dd>{item.binding?.enabled ? "已启用" : "未启用"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">Definition ID</dt>
            <dd className="font-mono text-xs">{item.definition_id ?? "—"}</dd>
          </div>
        </dl>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {isSystemBindingConflict(error)
              ? "项目使用状态已变化，请刷新后重试。"
              : adminAssetErrorMessage(error)}
          </p>
        ) : null}
        <DialogFooter className="gap-2 sm:justify-between">
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
                  { onSuccess: mutationSucceeded, onError: mutationFailed },
                )
              }
            >
              从项目停用
            </Button>
          ) : null}
          {canEnable ? (
            <Button
              type="button"
              disabled={pending}
              onClick={() =>
                enable.mutate(
                  {
                    asset_id: item.id,
                    ...(item.binding
                      ? { expected_binding_version: item.binding.version }
                      : {}),
                  },
                  { onSuccess: mutationSucceeded, onError: mutationFailed },
                )
              }
            >
              启用到项目
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function VersionedSystemBindingDialog({
  accountId,
  projectId,
  kind,
  item,
  open,
  onOpenChange,
  onConflict,
}: SystemBindingDialogProps & {
  kind: Exclude<AssetListKind, "agents">;
}) {
  const assetKind = BINDING_KIND[kind];
  const isMcp = kind === "mcp-servers";
  const history = useProjectAssetVersions(
    accountId,
    projectId,
    kind,
    item.id,
    true,
    item.scope,
  );
  const enable = useEnableProjectSystemBinding(accountId, projectId, assetKind);
  const disable = useDisableProjectSystemBinding(
    accountId,
    projectId,
    assetKind,
  );
  const syncCurrentMcp = useSyncCurrentProjectSystemMcpBinding(
    accountId,
    projectId,
  );

  const currentSkill = useMemo(
    () =>
      kind === "skills"
        ? (history.data?.data ?? []).find(
            (version) =>
              "skill_id" in version && version.relation === "current",
          )
        : null,
    [history.data, kind],
  );
  const skillBlocked = Boolean(
    currentSkill &&
    "skill_id" in currentSkill &&
    !systemSkillVersionIsBindable(currentSkill),
  );
  const mcpConfiguration = isMcp
    ? resolveMcpCurrentConfiguration(
        history.data?.data ?? [],
        "system",
        item.current_version_id,
      )
    : null;
  const currentMcpVersion = mcpConfiguration?.version ?? null;
  const mcpBlockedReason =
    currentMcpVersion && "mcp_server_id" in currentMcpVersion
      ? mcpVersionRuntimeBlockReason(currentMcpVersion, item.scope)
      : null;
  const currentVersionId = isMcp
    ? (currentMcpVersion?.id ?? "")
    : (item.current_version_id ?? "");
  const pending =
    enable.isPending || disable.isPending || syncCurrentMcp.isPending;
  const error = syncCurrentMcp.error ?? enable.error ?? disable.error;
  const canSubmit =
    canMoveSystemBinding(item) &&
    !pending &&
    !history.isLoading &&
    !history.error &&
    currentVersionId !== "" &&
    !skillBlocked &&
    !mcpBlockedReason &&
    (!item.binding?.enabled ||
      (isMcp && item.binding.current_version_id !== currentVersionId));

  function mutationSucceeded() {
    onOpenChange(false);
  }

  function mutationFailed(mutationError: unknown) {
    if (!isSystemBindingConflict(mutationError)) return;
    void history.refetch();
    onConflict?.();
  }

  function save() {
    if (!canSubmit) return;
    if (isMcp) {
      syncCurrentMcp.mutate(
        {
          assetId: item.id,
          input: item.binding
            ? { expected_binding_version: item.binding.version }
            : {},
        },
        { onSuccess: mutationSucceeded, onError: mutationFailed },
      );
      return;
    }
    enable.mutate(
      {
        asset_id: item.id,
        ...(item.binding
          ? { expected_binding_version: item.binding.version }
          : {}),
      },
      { onSuccess: mutationSucceeded, onError: mutationFailed },
    );
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
            {item.binding?.enabled ? "项目使用状态" : "启用到项目"}
          </DialogTitle>
          <DialogDescription>
            {item.display_name}。项目始终使用该系统资产的当前版本。
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
              disabled={history.isFetching}
              onClick={() => void history.refetch()}
            >
              {history.isFetching ? "重试中…" : "重试"}
            </Button>
          </div>
        ) : null}

        {skillBlocked ? (
          <p role="alert" className="text-destructive text-sm">
            当前 Skill 已被安全撤销，不能启用到项目。
          </p>
        ) : null}
        {isMcp && (mcpConfiguration?.state === "empty" || mcpBlockedReason) ? (
          <p role="alert" className="text-destructive text-sm">
            {mcpBlockedReason ?? "当前没有可启用的配置。"}
          </p>
        ) : null}
        <dl className="grid gap-2 text-sm">
          <div>
            <dt className="text-muted-foreground text-xs">项目状态</dt>
            <dd data-testid="binding-pinned-version">
              {isMcp
                ? systemMcpBindingStatus(item)
                : item.binding?.enabled
                  ? "已启用（自动使用当前版本）"
                  : "未启用"}
            </dd>
          </div>
        </dl>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {isSystemBindingConflict(error)
              ? "项目使用状态已变化，已刷新数据，请重试。"
              : adminAssetErrorMessage(error)}
          </p>
        ) : null}

        <DialogFooter className="gap-2 sm:justify-between">
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
                  { onSuccess: mutationSucceeded, onError: mutationFailed },
                )
              }
            >
              从项目停用
            </Button>
          ) : null}
          {canSubmit ? (
            <Button
              type="button"
              data-testid={isMcp ? "system-mcp-binding-submit" : undefined}
              disabled={!canSubmit}
              onClick={save}
            >
              {isMcp && item.binding?.enabled ? "更新到当前配置" : "启用到项目"}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function SystemBindingDialog(props: SystemBindingDialogProps) {
  if (props.kind === "agents") {
    return (
      <AgentSystemBindingDialog
        accountId={props.accountId}
        projectId={props.projectId}
        item={props.item}
        open={props.open}
        onOpenChange={props.onOpenChange}
        onConflict={props.onConflict}
      />
    );
  }
  return <VersionedSystemBindingDialog {...props} kind={props.kind} />;
}
