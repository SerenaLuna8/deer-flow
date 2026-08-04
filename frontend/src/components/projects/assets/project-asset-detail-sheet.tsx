"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  adminAssetErrorMessage,
  versionWorkflowActions,
} from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import type { Capability } from "@/core/projects/types";
import {
  useChangeProjectAssetStatus,
  useDeleteProjectAgent,
  useDeleteProjectMcp,
  useDeleteProjectSkill,
  useProjectAssetVersions,
  useProjectMcpEditableConfiguration,
  usePublishProjectAssetVersion,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectMcpEditableConfigurationResponse,
} from "@/core/shared-assets";
import { resolveMcpCurrentConfiguration } from "@/core/shared-assets/mcp-current";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

import {
  projectAssetCanCreateVersion,
  projectAssetCanDelete,
  projectAssetDetailLifecycleActions,
  projectAssetCanAuthor,
  projectMcpDeleteErrorMessage,
  projectSkillStatusToggleState,
} from "./project-asset-view-model";
import {
  ProjectAgentDeleteDialog,
  ProjectMcpDeleteDialog,
  ProjectSkillDeleteDialog,
} from "./project-skill-delete-dialog";
type MutableAssetKind = Exclude<AssetListKind, "credentials">;

export function projectAssetDetailShowsVersionHistory(
  kind: MutableAssetKind,
): boolean {
  return kind === "skills";
}

export function projectAssetDetailPreferredVersionId(
  kind: MutableAssetKind,
  scope: ProjectAssetItem["scope"],
  versions: readonly Pick<AssetVersion, "id">[],
  currentPublishedVersionId: string | null,
): string {
  if (kind === "mcp-servers") {
    return (
      resolveMcpCurrentConfiguration(
        versions as readonly AssetVersion[],
        scope,
        currentPublishedVersionId,
      ).version?.id ?? ""
    );
  }
  return (
    versions.find((version) => version.id === currentPublishedVersionId)?.id ??
    versions[0]?.id ??
    ""
  );
}

export function projectMcpEditableConfigurationEnabled(
  open: boolean,
  kind: MutableAssetKind,
  item: Pick<ProjectAssetItem, "scope" | "capabilities">,
): boolean {
  return (
    open &&
    kind === "mcp-servers" &&
    item.scope === "project" &&
    item.capabilities.includes("shared_assets.edit")
  );
}

export function projectAssetEditBaseVersion(
  kind: MutableAssetKind,
  selectedVersion: AssetVersion | null,
  editableConfiguration: ProjectMcpEditableConfigurationResponse | undefined,
  editableConfigurationSucceeded: boolean,
): AssetVersion | null {
  if (kind !== "mcp-servers") return selectedVersion;
  return editableConfigurationSucceeded
    ? (editableConfiguration?.version ?? null)
    : null;
}

export function projectAssetDetailContentVersion(
  kind: MutableAssetKind,
  selectedVersion: AssetVersion | null,
  editableConfiguration: ProjectMcpEditableConfigurationResponse | undefined,
  editableConfigurationSucceeded: boolean,
): AssetVersion | null {
  if (kind !== "mcp-servers" || !editableConfigurationSucceeded) {
    return selectedVersion;
  }
  return editableConfiguration?.version ?? selectedVersion;
}

export function projectMcpSystemUsageLabel(
  item: Pick<ProjectAssetItem, "binding" | "current_published_version_id">,
): "未启用" | "已启用" | "有配置更新" {
  if (!item.binding?.enabled) return "未启用";
  return item.current_published_version_id &&
    item.binding.version_id !== item.current_published_version_id
    ? "有配置更新"
    : "已启用";
}

export function projectAssetDetailSummaryGridColumns(
  kind: MutableAssetKind,
  scope: ProjectAssetItem["scope"],
): string {
  return kind === "mcp-servers" || (kind === "skills" && scope === "system")
    ? "sm:grid-cols-3"
    : "sm:grid-cols-2";
}

export function projectAssetDetailVersionTerms(
  kind: MutableAssetKind,
  scope: ProjectAssetItem["scope"],
): {
  current: string;
  history: string;
  edit: string;
  empty: string;
} {
  if (kind === "mcp-servers") {
    return {
      current: "当前配置",
      history: "",
      edit: "编辑配置",
      empty: "尚未保存配置。",
    };
  }
  return {
    current: scope === "system" ? "系统最新发布" : "当前发布",
    history: "版本",
    edit: "创建新版本",
    empty: "尚未创建版本。",
  };
}

export function projectAssetDetailRevisionCopy(kind: MutableAssetKind): {
  label: (number: number) => string;
  publishedFallback: string;
  pinnedFallback: string;
  updateAvailable: string;
  viewAria: string;
  loading: string;
  publish: string;
  technical: string;
} {
  if (kind === "mcp-servers") {
    return {
      label: () => "配置",
      publishedFallback: "已发布",
      pinnedFallback: "已启用",
      updateAvailable: "有配置更新",
      viewAria: "查看配置",
      loading: "正在加载配置，请稍候",
      publish: "发布配置",
      technical: "配置技术信息",
    };
  }
  return {
    label: (number) => `版本 ${number}`,
    publishedFallback: "已有发布版本",
    pinnedFallback: "已固定版本",
    updateAvailable: "有新版本",
    viewAria: "查看版本",
    loading: "正在加载新版本，请稍候",
    publish: "发布版本",
    technical: "版本技术信息",
  };
}

export function projectAssetDiscardCopy(kind: MutableAssetKind): {
  title: string;
  description: string;
} {
  if (kind === "agents") {
    return {
      title: "放弃未保存的 Agent 设置？",
      description:
        "关闭详情会清除四项 Agent 设置的本地修改，已保存设置不会受影响。",
    };
  }
  if (kind === "mcp-servers") {
    return {
      title: "放弃未保存的配置修改？",
      description: "关闭详情会清除当前编辑副本，已保存的 MCP 配置不会受影响。",
    };
  }
  return {
    title: "放弃未保存的文件修改？",
    description:
      "切换版本或关闭详情会清除当前编辑副本，已保存的 Skill 版本不会受影响。",
  };
}

export function effectiveAssetVersion(
  scope: ProjectAssetItem["scope"],
  bindingEnabled: boolean,
  pinnedVersion: AssetVersion | undefined,
  currentPublished: AssetVersion | undefined,
  latestVersion: AssetVersion | undefined,
): AssetVersion | null {
  return (
    (scope === "system" && bindingEnabled ? pinnedVersion : null) ??
    currentPublished ??
    latestVersion ??
    null
  );
}
type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type VersionStatus = ReturnType<typeof workflowStatus>;

export type ProjectSkillDeleteSnapshot = Readonly<{
  assetId: string;
  skillName: string;
  expectedAssetVersion: number;
  startedAt: number;
}>;

export function createProjectSkillDeleteSnapshot(
  item: Pick<ProjectAssetItem, "id" | "display_name" | "version">,
  startedAt: number,
): ProjectSkillDeleteSnapshot {
  return Object.freeze({
    assetId: item.id,
    skillName: item.display_name,
    expectedAssetVersion: item.version,
    startedAt,
  });
}

export type ProjectAgentDeleteSnapshot = Readonly<{
  assetId: string;
  agentName: string;
  expectedAssetVersion: number;
  startedAt: number;
}>;

export function createProjectAgentDeleteSnapshot(
  item: Pick<ProjectAssetItem, "id" | "display_name" | "version">,
  startedAt: number,
): ProjectAgentDeleteSnapshot {
  return Object.freeze({
    assetId: item.id,
    agentName: item.display_name,
    expectedAssetVersion: item.version,
    startedAt,
  });
}

export type ProjectMcpDeleteSnapshot = Readonly<{
  assetId: string;
  mcpName: string;
  expectedAssetVersion: number;
  startedAt: number;
}>;

export function createProjectMcpDeleteSnapshot(
  item: Pick<ProjectAssetItem, "id" | "display_name" | "version">,
  startedAt: number,
): ProjectMcpDeleteSnapshot {
  return Object.freeze({
    assetId: item.id,
    mcpName: item.display_name,
    expectedAssetVersion: item.version,
    startedAt,
  });
}

export type ProjectAssetVersionRenderContext = {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  canAuthor: boolean;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
};

const VERSION_STATUS_LABEL: Record<VersionStatus, string> = {
  draft: "草稿",
  pending_approval: "待审批",
  published: "已发布",
  rejected: "已拒绝",
  active: "启用",
  retired: "已替换",
  revoked: "已撤销",
};

export function projectMcpCurrentConfigurationLabel(
  status: McpVersion["workflow_status"],
): string {
  return status === "pending_approval"
    ? "凭据未绑定 · 尚未生效"
    : VERSION_STATUS_LABEL[status];
}

function isMcpVersion(version: AssetVersion): version is McpVersion {
  return "mcp_server_id" in version;
}

function workflowStatus(
  version: AssetVersion,
):
  | "draft"
  | "pending_approval"
  | "published"
  | "rejected"
  | "active"
  | "retired"
  | "revoked" {
  return "workflow_status" in version
    ? version.workflow_status
    : version.status;
}

export function versionPublishDisabled(
  actionPending: boolean,
  versionDirty: boolean,
  versionSelectionPending = false,
): boolean {
  return (
    versionActionDisabled(actionPending, versionSelectionPending) ||
    versionDirty
  );
}

export function versionActionDisabled(
  actionPending: boolean,
  versionSelectionPending: boolean,
): boolean {
  return actionPending || versionSelectionPending;
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error)}
    </p>
  );
}

export function ProjectAssetDetailHeader({
  kind,
  item,
  statusPending,
  optimisticSkillStatus,
  onToggleProjectSkillStatus,
}: {
  kind: MutableAssetKind;
  item: ProjectAssetItem;
  statusPending: boolean;
  optimisticSkillStatus?: boolean;
  onToggleProjectSkillStatus: (checked: boolean) => void;
}) {
  if (kind === "agents") {
    return (
      <SheetHeader className="border-border/70 border-b px-6 py-5 pr-12 text-left">
        <SheetTitle className="text-xl">{item.display_name}</SheetTitle>
        <SheetDescription>
          <time dateTime={item.updated_at}>
            {new Date(item.updated_at).toLocaleString("zh-CN")}
          </time>
        </SheetDescription>
      </SheetHeader>
    );
  }

  if (kind === "mcp-servers") {
    return (
      <SheetHeader className="border-border/70 border-b px-6 py-5 pr-12 text-left">
        {item.scope === "system" ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">系统提供</Badge>
          </div>
        ) : null}
        <SheetTitle
          className={item.scope === "system" ? "mt-2 text-xl" : "text-xl"}
        >
          {item.display_name}
        </SheetTitle>
        <SheetDescription className="font-mono">{item.slug}</SheetDescription>
      </SheetHeader>
    );
  }

  const toggleState = projectSkillStatusToggleState(item);
  const checked = optimisticSkillStatus ?? toggleState.checked;
  const showToggle = item.scope === "project";
  return (
    <SheetHeader className="border-border/70 border-b px-6 py-5 pr-12 text-left">
      <div className="flex min-w-0 items-start justify-between gap-4">
        <SheetTitle className="min-w-0 truncate text-xl">
          {item.display_name}
        </SheetTitle>
        {showToggle ? (
          <div className="flex shrink-0 flex-col items-end gap-1.5">
            <Switch
              checked={checked}
              disabled={toggleState.disabled || statusPending}
              className="data-[state=checked]:bg-success focus-visible:ring-selection/30"
              aria-busy={statusPending || undefined}
              aria-label={`${checked ? "停用" : "启用"} ${item.display_name}`}
              title={toggleState.disabledReason ?? undefined}
              onCheckedChange={onToggleProjectSkillStatus}
            />
            {toggleState.disabledReason ? (
              <span className="text-muted-foreground text-xs">
                {toggleState.disabledReason}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      <SheetDescription className="sr-only">
        Skill 详情与版本文件
      </SheetDescription>
    </SheetHeader>
  );
}

export function ProjectSkillDetailActions({
  actionPending,
  canAuthor,
  canDelete,
  editing,
  hasSelectedVersion,
  versionDirty,
  versionSelectionPending,
  onCreateVersion,
  onDelete,
}: {
  actionPending: boolean;
  canAuthor: boolean;
  canDelete: boolean;
  editing: boolean;
  hasSelectedVersion: boolean;
  versionDirty: boolean;
  versionSelectionPending: boolean;
  onCreateVersion: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      {canAuthor && hasSelectedVersion && !editing ? (
        <Button
          type="button"
          disabled={versionDirty || versionSelectionPending}
          title={
            versionDirty
              ? "请先保存或放弃当前未保存修改"
              : versionSelectionPending
                ? "正在加载新版本，请稍候"
                : undefined
          }
          onClick={onCreateVersion}
        >
          创建新版本
        </Button>
      ) : null}
      {canDelete ? (
        <Button
          type="button"
          variant="destructive"
          disabled={actionPending}
          onClick={onDelete}
        >
          删除 Skill
        </Button>
      ) : null}
    </>
  );
}

export function ProjectMcpDangerZone({
  actionPending,
  canDelete,
  onDelete,
}: {
  actionPending: boolean;
  canDelete: boolean;
  onDelete: () => void;
}) {
  if (!canDelete) return null;
  return (
    <section
      aria-label="危险区"
      className="border-destructive/35 bg-destructive/5 rounded-xl border p-4"
    >
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <h3 className="text-sm font-semibold">危险区</h3>
          <p className="text-muted-foreground text-sm">
            永久删除此 MCP 及其配置。此操作不可恢复。
          </p>
        </div>
        <Button
          type="button"
          variant="destructive"
          disabled={actionPending}
          onClick={onDelete}
        >
          删除 MCP
        </Button>
      </div>
    </section>
  );
}

export function projectAssetLifecycleActionLabel(
  kind: MutableAssetKind,
  action: "activate" | "suspend",
): string {
  if (action === "suspend") return "停用";
  return kind === "mcp-servers" ? "重新启用" : "启用";
}

export function ProjectAssetDetailSheet({
  accountId,
  projectId,
  projectCapabilities,
  kind,
  item,
  open,
  requestedVersionId,
  onOpenChange,
  onCreateVersion,
  onDeleted,
  onVersionCreated,
  onRequestedVersionHandled,
  renderAssetEditor,
  renderVersion,
}: {
  accountId: string;
  projectId: string;
  projectCapabilities: readonly Capability[];
  kind: MutableAssetKind;
  item: ProjectAssetItem;
  open: boolean;
  requestedVersionId: string | null;
  onOpenChange: (open: boolean) => void;
  onCreateVersion: (
    item: ProjectAssetItem,
    selectedVersion: AssetVersion | null,
    editableMcpConfiguration?: ProjectMcpEditableConfigurationResponse,
  ) => void;
  onDeleted: (assetId: string) => void;
  onVersionCreated: (assetId: string, versionId: string) => void;
  onRequestedVersionHandled: (assetId: string, versionId: string) => void;
  renderAssetEditor?: (
    effectiveVersion: AssetVersion | null,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  renderVersion?: (
    version: AssetVersion,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
}) {
  const history = useProjectAssetVersions(accountId, projectId, kind, item.id);
  const editableMcpConfigurationEnabled =
    projectMcpEditableConfigurationEnabled(open, kind, item);
  const editableMcpConfiguration = useProjectMcpEditableConfiguration(
    accountId,
    projectId,
    item.id,
    editableMcpConfigurationEnabled,
  );
  const showsVersionHistory = projectAssetDetailShowsVersionHistory(kind);
  const showsVersionContent = kind !== "agents";
  const versionTerms = projectAssetDetailVersionTerms(kind, item.scope);
  const revisionCopy = projectAssetDetailRevisionCopy(kind);
  const discardCopy = projectAssetDiscardCopy(kind);
  const publish = usePublishProjectAssetVersion(
    accountId,
    projectId,
    kind === "agents" ? null : kind,
  );
  const changeStatus = useChangeProjectAssetStatus(accountId, projectId, kind);
  const deleteSkill = useDeleteProjectSkill(accountId, projectId);
  const deleteAgent = useDeleteProjectAgent(accountId, projectId);
  const deleteMcp = useDeleteProjectMcp(accountId, projectId);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [versionDirty, setVersionDirty] = useState(false);
  const [versionEditing, setVersionEditing] = useState(false);
  const [skillDeleteSnapshot, setSkillDeleteSnapshot] =
    useState<ProjectSkillDeleteSnapshot | null>(null);
  const [agentDeleteSnapshot, setAgentDeleteSnapshot] =
    useState<ProjectAgentDeleteSnapshot | null>(null);
  const [mcpDeleteSnapshot, setMcpDeleteSnapshot] =
    useState<ProjectMcpDeleteSnapshot | null>(null);
  const [discardAction, setDiscardAction] = useState<
    { type: "close" } | { type: "version"; versionId: string } | null
  >(null);

  const versions = useMemo(() => history.data?.data ?? [], [history.data]);
  const mcpConfiguration =
    kind === "mcp-servers"
      ? resolveMcpCurrentConfiguration(
          versions,
          item.scope,
          item.current_published_version_id,
        )
      : null;
  const selectedVersion =
    kind === "mcp-servers"
      ? (mcpConfiguration?.version ?? null)
      : (versions.find((version) => version.id === selectedVersionId) ?? null);
  const editBaseVersion = projectAssetEditBaseVersion(
    kind,
    selectedVersion,
    editableMcpConfiguration.data,
    editableMcpConfiguration.isSuccess,
  );
  const detailContentVersion = projectAssetDetailContentVersion(
    kind,
    selectedVersion,
    editableMcpConfiguration.data,
    editableMcpConfiguration.isSuccess,
  );
  const selectedRuntimeBlockReason =
    selectedVersion && isMcpVersion(selectedVersion)
      ? mcpVersionRuntimeBlockReason(selectedVersion, item.scope)
      : null;
  const currentPublished = versions.find(
    (version) => version.id === item.current_published_version_id,
  );
  const pinnedVersion = versions.find(
    (version) => version.id === item.binding?.version_id,
  );
  const effectiveVersion = effectiveAssetVersion(
    item.scope,
    item.binding?.enabled === true,
    pinnedVersion,
    currentPublished,
    versions[0],
  );

  useEffect(() => {
    if (!open || versions.length === 0) return;
    if (kind === "mcp-servers") {
      const preferredId = projectAssetDetailPreferredVersionId(
        kind,
        item.scope,
        versions,
        item.current_published_version_id,
      );
      setSelectedVersionId(preferredId);
      if (
        requestedVersionId &&
        versions.some((version) => version.id === requestedVersionId)
      ) {
        onRequestedVersionHandled(item.id, requestedVersionId);
      }
      return;
    }
    if (
      requestedVersionId &&
      versions.some((version) => version.id === requestedVersionId)
    ) {
      setSelectedVersionId(requestedVersionId);
      onRequestedVersionHandled(item.id, requestedVersionId);
      return;
    }
    const preferredId = projectAssetDetailPreferredVersionId(
      kind,
      item.scope,
      versions,
      item.current_published_version_id,
    );
    setSelectedVersionId((current) =>
      versions.some((version) => version.id === current)
        ? current
        : preferredId,
    );
  }, [
    item.current_published_version_id,
    item.id,
    item.scope,
    kind,
    onRequestedVersionHandled,
    open,
    requestedVersionId,
    versions,
  ]);

  useEffect(() => {
    if (open) return;
    setSelectedVersionId("");
    setVersionDirty(false);
    setVersionEditing(false);
    setSkillDeleteSnapshot(null);
    setAgentDeleteSnapshot(null);
    setMcpDeleteSnapshot(null);
    setDiscardAction(null);
  }, [open]);

  useEffect(() => {
    if (kind !== "agents") setVersionEditing(false);
  }, [kind, selectedVersionId]);

  const canAuthor =
    item.scope === "project" && projectAssetCanAuthor(item, kind);
  const lifecycleActions =
    item.scope === "project"
      ? projectAssetDetailLifecycleActions(kind, item, projectCapabilities)
      : [];
  const canDeleteAsset = projectAssetCanDelete(kind, item);
  const versionActions = useMemo(() => {
    if (
      kind === "agents" ||
      item.scope !== "project" ||
      !selectedVersion ||
      !("workflow_status" in selectedVersion)
    ) {
      return [];
    }
    return versionWorkflowActions(
      kind,
      selectedVersion.workflow_status,
      isMcpVersion(selectedVersion) &&
        selectedVersion.credential_slots.length > 0,
    );
  }, [item.scope, kind, selectedVersion]);

  const actionPending =
    publish.isPending ||
    changeStatus.isPending ||
    deleteSkill.isPending ||
    deleteAgent.isPending ||
    deleteMcp.isPending;
  const versionSelectionPending = requestedVersionId !== null;
  const actionError = publish.error ?? changeStatus.error;
  const optimisticSkillStatus =
    kind === "skills" && changeStatus.isPending
      ? changeStatus.variables?.action === "activate"
      : undefined;

  const handleWorkbenchVersionCreated = useCallback(
    (versionId: string) => {
      setVersionDirty(false);
      setVersionEditing(false);
      onVersionCreated(item.id, versionId);
      void history.refetch();
    },
    [history, item.id, onVersionCreated],
  );

  function publishSelectedVersion(version: AssetVersion) {
    if (
      isMcpVersion(version) &&
      mcpVersionRuntimeBlockReason(version, item.scope)
    ) {
      return;
    }
    publish.mutate(
      {
        assetId: item.id,
        versionId: version.id,
        input: { expected_asset_version: item.version },
      },
      {
        onSuccess: (result) => {
          setSelectedVersionId(result.data.id);
          handleWorkbenchVersionCreated(result.data.id);
        },
      },
    );
  }

  function requestOpenChange(next: boolean) {
    if (!next && versionDirty) {
      setDiscardAction({ type: "close" });
      return;
    }
    if (!next) setVersionEditing(false);
    onOpenChange(next);
  }

  function requestVersionChange(versionId: string) {
    if (versionId === selectedVersionId) return;
    if (versionDirty) {
      setDiscardAction({ type: "version", versionId });
      return;
    }
    setVersionEditing(false);
    setSelectedVersionId(versionId);
  }

  function confirmDiscardNavigation() {
    const action = discardAction;
    setDiscardAction(null);
    setVersionDirty(false);
    setVersionEditing(false);
    if (action?.type === "close") {
      onOpenChange(false);
    } else if (action?.type === "version") {
      setSelectedVersionId(action.versionId);
    }
  }

  async function confirmSkillDelete() {
    const snapshot = skillDeleteSnapshot;
    if (!snapshot) return;
    try {
      await deleteSkill.mutateAsync({
        assetId: snapshot.assetId,
        input: {
          expected_asset_version: snapshot.expectedAssetVersion,
        },
      });
      setSkillDeleteSnapshot(null);
      onDeleted(snapshot.assetId);
    } catch {
      // The mutation exposes only its mapped public error inside the dialog.
    }
  }

  async function confirmAgentDelete() {
    const snapshot = agentDeleteSnapshot;
    if (!snapshot) return;
    try {
      await deleteAgent.mutateAsync({
        assetId: snapshot.assetId,
        input: {
          expected_asset_version: snapshot.expectedAssetVersion,
        },
      });
      setAgentDeleteSnapshot(null);
      onDeleted(snapshot.assetId);
    } catch {
      // The mutation exposes only its mapped public error inside the dialog.
    }
  }

  async function confirmMcpDelete() {
    const snapshot = mcpDeleteSnapshot;
    if (!snapshot) return;
    try {
      await deleteMcp.mutateAsync({
        assetId: snapshot.assetId,
        input: {
          expected_asset_version: snapshot.expectedAssetVersion,
        },
      });
      setMcpDeleteSnapshot(null);
      onDeleted(snapshot.assetId);
    } catch {
      // The mutation exposes only its mapped public error inside the dialog.
    }
  }

  function toggleProjectSkillStatus(checked: boolean) {
    if (kind !== "skills") return;
    const toggleState = projectSkillStatusToggleState(item);
    if (toggleState.disabled || toggleState.checked === checked) return;
    changeStatus.mutate({
      assetId: item.id,
      action: checked ? "activate" : "suspend",
      input: { expected_asset_version: item.version },
    });
  }

  return (
    <>
      <Sheet open={open} onOpenChange={requestOpenChange}>
        <SheetContent
          className={`w-full gap-0 p-0 ${kind === "skills" || kind === "agents" ? "sm:max-w-[1080px]" : kind === "mcp-servers" ? "sm:max-w-[900px]" : "sm:max-w-[640px]"}`}
        >
          <ProjectAssetDetailHeader
            kind={kind}
            item={item}
            statusPending={changeStatus.isPending}
            optimisticSkillStatus={optimisticSkillStatus}
            onToggleProjectSkillStatus={toggleProjectSkillStatus}
          />

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="space-y-6 px-6 py-5">
              {showsVersionContent ? (
                <section
                  className={`grid gap-3 ${projectAssetDetailSummaryGridColumns(kind, item.scope)}`}
                >
                  <div className="bg-muted/35 rounded-xl p-4">
                    <p className="text-muted-foreground text-xs">
                      {versionTerms.current}
                    </p>
                    <p className="mt-2 text-sm font-medium">
                      {kind === "mcp-servers"
                        ? mcpConfiguration?.state === "ready" &&
                          mcpConfiguration.version
                          ? projectMcpCurrentConfigurationLabel(
                              mcpConfiguration.version.workflow_status,
                            )
                          : mcpConfiguration?.state === "empty"
                            ? "尚未保存配置"
                            : "当前配置无法确认"
                        : currentPublished
                          ? revisionCopy.label(currentPublished.version_number)
                          : item.current_published_version_id
                            ? revisionCopy.publishedFallback
                            : "尚未发布"}
                    </p>
                  </div>
                  {item.scope === "system" ? (
                    <div className="bg-muted/35 rounded-xl p-4">
                      <p className="text-muted-foreground text-xs">项目使用</p>
                      <p className="mt-2 text-sm font-medium">
                        {kind === "mcp-servers"
                          ? projectMcpSystemUsageLabel(item)
                          : !item.binding
                            ? "未启用"
                            : !item.binding.enabled
                              ? "已从项目停用"
                              : pinnedVersion
                                ? `${revisionCopy.label(pinnedVersion.version_number)}${
                                    item.current_published_version_id !==
                                    pinnedVersion.id
                                      ? ` · ${revisionCopy.updateAvailable}`
                                      : ""
                                  }`
                                : revisionCopy.pinnedFallback}
                      </p>
                    </div>
                  ) : null}
                  <div className="bg-muted/35 rounded-xl p-4">
                    <p className="text-muted-foreground text-xs">最近更新</p>
                    <time className="mt-2 block text-sm">
                      {new Date(item.updated_at).toLocaleString("zh-CN")}
                    </time>
                  </div>
                  {kind === "mcp-servers" && item.scope === "project" ? (
                    <div className="bg-muted/35 rounded-xl p-4">
                      <p className="text-muted-foreground text-xs">状态</p>
                      <div className="mt-2">
                        <AssetStatusBadge status={item.status} />
                      </div>
                    </div>
                  ) : null}
                </section>
              ) : null}

              <div className="flex flex-wrap items-center gap-2">
                {projectAssetCanCreateVersion(kind, canAuthor) && (
                  <Button
                    type="button"
                    disabled={
                      versionDirty ||
                      versionSelectionPending ||
                      (kind === "mcp-servers" &&
                        !editableMcpConfiguration.isSuccess)
                    }
                    title={
                      versionDirty
                        ? "请先保存或放弃当前未保存修改"
                        : versionSelectionPending
                          ? revisionCopy.loading
                          : kind === "mcp-servers" &&
                              editableMcpConfiguration.isLoading
                            ? "正在加载可编辑配置，请稍候"
                            : kind === "mcp-servers" &&
                                editableMcpConfiguration.error
                              ? "可编辑配置加载失败，请重新打开详情后重试"
                              : undefined
                    }
                    onClick={() => {
                      if (!editBaseVersion) return;
                      onCreateVersion(
                        item,
                        editBaseVersion,
                        editableMcpConfiguration.data,
                      );
                    }}
                  >
                    {versionTerms.edit}
                  </Button>
                )}
                {lifecycleActions.map((action) => (
                  <Button
                    key={action}
                    type="button"
                    variant="outline"
                    disabled={actionPending}
                    onClick={() =>
                      changeStatus.mutate({
                        assetId: item.id,
                        action,
                        input: { expected_asset_version: item.version },
                      })
                    }
                  >
                    {projectAssetLifecycleActionLabel(kind, action)}
                  </Button>
                ))}
                {kind === "skills" ? (
                  <ProjectSkillDetailActions
                    actionPending={actionPending}
                    canAuthor={canAuthor}
                    canDelete={canDeleteAsset}
                    editing={versionEditing}
                    hasSelectedVersion={selectedVersion !== null}
                    versionDirty={versionDirty}
                    versionSelectionPending={versionSelectionPending}
                    onCreateVersion={() => setVersionEditing(true)}
                    onDelete={() => {
                      deleteSkill.reset();
                      setSkillDeleteSnapshot(
                        createProjectSkillDeleteSnapshot(item, Date.now()),
                      );
                    }}
                  />
                ) : null}
                {kind === "agents" && canDeleteAsset ? (
                  <Button
                    type="button"
                    variant="destructive"
                    disabled={actionPending}
                    onClick={() => {
                      deleteAgent.reset();
                      setAgentDeleteSnapshot(
                        createProjectAgentDeleteSnapshot(item, Date.now()),
                      );
                    }}
                  >
                    删除
                  </Button>
                ) : null}
              </div>

              {renderAssetEditor ? (
                history.isLoading ? (
                  <div
                    className="border-border/70 space-y-3 border-t pt-5"
                    aria-label="正在加载 Agent 设置"
                  >
                    <Skeleton className="h-8 w-40" />
                    <Skeleton className="h-40 w-full rounded-xl" />
                  </div>
                ) : history.error ? (
                  <div className="border-destructive/30 space-y-3 rounded-xl border p-4">
                    <ErrorNotice error={history.error} />
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
                  <div className="border-border/70 border-t pt-5">
                    {renderAssetEditor(effectiveVersion, {
                      accountId,
                      projectId,
                      item,
                      canAuthor: canAuthor && !versionSelectionPending,
                      editing: versionEditing,
                      onEditingChange: setVersionEditing,
                      onDirtyChange: setVersionDirty,
                      onVersionCreated: handleWorkbenchVersionCreated,
                    })}
                  </div>
                )
              ) : null}

              {showsVersionContent ? (
                <section className="space-y-3">
                  {showsVersionHistory ? (
                    <div className="flex items-center justify-between gap-3">
                      <h2 className="text-sm font-semibold">
                        {versionTerms.history}
                      </h2>
                      {versions.length > 0 && (
                        <label className="text-muted-foreground flex items-center gap-2 text-xs">
                          查看
                          <select
                            aria-label={revisionCopy.viewAria}
                            value={selectedVersionId}
                            disabled={versionSelectionPending}
                            onChange={(event) =>
                              requestVersionChange(event.target.value)
                            }
                            className="border-input bg-background h-8 rounded-md border px-2 text-xs"
                          >
                            {versions.map((version) => (
                              <option key={version.id} value={version.id}>
                                {revisionCopy.label(version.version_number)} ·{" "}
                                {VERSION_STATUS_LABEL[workflowStatus(version)]}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                    </div>
                  ) : null}

                  {history.isLoading ? (
                    <div
                      className="space-y-3"
                      aria-label={revisionCopy.loading}
                    >
                      <Skeleton className="h-8 w-40" />
                      <Skeleton className="h-40 w-full rounded-xl" />
                    </div>
                  ) : history.error ? (
                    <div className="border-destructive/30 space-y-3 rounded-xl border p-4">
                      <ErrorNotice error={history.error} />
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
                  ) : kind === "mcp-servers" &&
                    mcpConfiguration?.state === "unconfirmed" ? (
                    <p role="alert" className="text-destructive text-sm">
                      当前配置无法确认
                    </p>
                  ) : !selectedVersion ? (
                    <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
                      {versionTerms.empty}
                    </p>
                  ) : (
                    <div className="space-y-5">
                      {showsVersionHistory ? (
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-lg font-semibold">
                            {revisionCopy.label(selectedVersion.version_number)}
                          </span>
                          <AssetStatusBadge
                            status={workflowStatus(selectedVersion)}
                          />
                          <time className="text-muted-foreground ml-auto text-xs">
                            {new Date(
                              selectedVersion.created_at,
                            ).toLocaleString("zh-CN")}
                          </time>
                        </div>
                      ) : null}

                      {item.scope === "project" &&
                        versionActions.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {versionActions.includes("publish") &&
                              canAuthor && (
                                <Button
                                  type="button"
                                  size="sm"
                                  disabled={versionPublishDisabled(
                                    actionPending ||
                                      Boolean(selectedRuntimeBlockReason),
                                    versionDirty,
                                    versionSelectionPending,
                                  )}
                                  title={
                                    selectedRuntimeBlockReason ??
                                    (versionDirty
                                      ? "请先保存或放弃当前未保存修改"
                                      : versionSelectionPending
                                        ? revisionCopy.loading
                                        : undefined)
                                  }
                                  onClick={() =>
                                    publishSelectedVersion(selectedVersion)
                                  }
                                >
                                  {revisionCopy.publish}
                                </Button>
                              )}
                          </div>
                        )}

                      {selectedRuntimeBlockReason ? (
                        <p role="alert" className="text-destructive text-sm">
                          {selectedRuntimeBlockReason}
                        </p>
                      ) : null}

                      <div
                        key={selectedVersion.id}
                        className="border-border/70 border-t pt-5"
                      >
                        {renderVersion?.(
                          detailContentVersion ?? selectedVersion,
                          {
                            accountId,
                            projectId,
                            item,
                            canAuthor: canAuthor && !versionSelectionPending,
                            editing: versionEditing,
                            onEditingChange: setVersionEditing,
                            onDirtyChange: setVersionDirty,
                            onVersionCreated: handleWorkbenchVersionCreated,
                          },
                        )}
                      </div>

                      <details className="border-border/70 rounded-xl border px-4 py-3">
                        <summary className="cursor-pointer text-sm font-medium">
                          {revisionCopy.technical}
                        </summary>
                        <dl className="mt-3 grid gap-3 text-xs">
                          {"payload_checksum" in selectedVersion && (
                            <div>
                              <dt className="text-muted-foreground">
                                载荷校验和
                              </dt>
                              <dd className="mt-1 font-mono break-all">
                                {selectedVersion.payload_checksum}
                              </dd>
                            </div>
                          )}
                          <div>
                            <dt className="text-muted-foreground">创建者</dt>
                            <dd className="mt-1 font-mono break-all">
                              {selectedVersion.created_by_user_id}
                            </dd>
                          </div>
                        </dl>
                      </details>
                    </div>
                  )}
                </section>
              ) : null}

              <ErrorNotice error={actionError} />

              {kind === "mcp-servers" ? (
                <ProjectMcpDangerZone
                  actionPending={actionPending}
                  canDelete={canDeleteAsset}
                  onDelete={() => {
                    deleteMcp.reset();
                    setMcpDeleteSnapshot(
                      createProjectMcpDeleteSnapshot(item, Date.now()),
                    );
                  }}
                />
              ) : null}
            </div>
          </div>
        </SheetContent>
      </Sheet>

      {skillDeleteSnapshot !== null && (
        <ProjectSkillDeleteDialog
          key={`${skillDeleteSnapshot.assetId}:${skillDeleteSnapshot.startedAt}`}
          skillName={skillDeleteSnapshot.skillName}
          startedAt={skillDeleteSnapshot.startedAt}
          pending={deleteSkill.isPending}
          errorMessage={
            deleteSkill.error ? adminAssetErrorMessage(deleteSkill.error) : null
          }
          onOpenChange={(next) => {
            if (next) return;
            setSkillDeleteSnapshot(null);
            deleteSkill.reset();
          }}
          onConfirm={() => void confirmSkillDelete()}
        />
      )}

      {agentDeleteSnapshot !== null && (
        <ProjectAgentDeleteDialog
          key={`${agentDeleteSnapshot.assetId}:${agentDeleteSnapshot.startedAt}`}
          agentName={agentDeleteSnapshot.agentName}
          startedAt={agentDeleteSnapshot.startedAt}
          pending={deleteAgent.isPending}
          errorMessage={
            deleteAgent.error ? adminAssetErrorMessage(deleteAgent.error) : null
          }
          onOpenChange={(next) => {
            if (next) return;
            setAgentDeleteSnapshot(null);
            deleteAgent.reset();
          }}
          onConfirm={() => void confirmAgentDelete()}
        />
      )}

      {mcpDeleteSnapshot !== null && (
        <ProjectMcpDeleteDialog
          key={`${mcpDeleteSnapshot.assetId}:${mcpDeleteSnapshot.startedAt}`}
          mcpName={mcpDeleteSnapshot.mcpName}
          startedAt={mcpDeleteSnapshot.startedAt}
          pending={deleteMcp.isPending}
          errorMessage={
            deleteMcp.error
              ? projectMcpDeleteErrorMessage(deleteMcp.error)
              : null
          }
          onOpenChange={(next) => {
            if (next) return;
            setMcpDeleteSnapshot(null);
            deleteMcp.reset();
          }}
          onConfirm={() => void confirmMcpDelete()}
        />
      )}

      <Dialog
        open={discardAction !== null}
        onOpenChange={(next) => !next && setDiscardAction(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{discardCopy.title}</DialogTitle>
            <DialogDescription>{discardCopy.description}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDiscardAction(null)}
            >
              继续编辑
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={confirmDiscardNavigation}
            >
              放弃并继续
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
