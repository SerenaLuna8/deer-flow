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
import { AssetVersionDiff } from "@/components/assets/asset-version-diff";
import {
  skillExportBlockReason,
  SkillExportButton,
} from "@/components/assets/skill-export-button";
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
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import { useModels } from "@/core/models/hooks";
import type { Capability } from "@/core/projects/types";
import {
  useChangeProjectAssetStatus,
  useDeleteProjectAgent,
  useDeleteProjectMcp,
  useDeleteProjectSkill,
  exportProjectSkillVersion,
  useProjectAssetVersions,
  useProjectMcpEditableConfiguration,
  usePublishProjectAssetVersion,
  useRestoreProjectAgentVersion,
  SharedAssetApiError,
  type AssetListKind,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectMcpEditableConfigurationResponse,
} from "@/core/shared-assets";
import { resolveMcpCurrentConfiguration } from "@/core/shared-assets/mcp-current";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

import { agentAuthoringBaseVersion } from "./agent-authoring-recovery";
import {
  projectAssetCanCreateVersion,
  projectAssetCanDelete,
  projectAssetDetailLifecycleActions,
  projectAssetCanAuthor,
  projectAgentDeleteErrorMessage,
  projectAgentVersionCanPublish,
  projectMcpDeleteErrorMessage,
  projectSkillDeleteErrorMessage,
  projectSkillCredentialSetupRequired,
  projectSkillVersionCanPublish,
  projectSkillStatusToggleState,
} from "./project-asset-view-model";
import {
  ProjectAgentDeleteDialog,
  ProjectMcpDeleteDialog,
  ProjectSkillDeleteDialog,
} from "./project-skill-delete-dialog";
import { SkillPublishDialog } from "./skill-publish-dialog";
type MutableAssetKind = Exclude<AssetListKind, "credentials">;
type ProjectAgentRecoveryVersion = Pick<
  Extract<AssetVersion, { agent_id: string }>,
  "id" | "workflow_status" | "supersedes_version_id"
>;

export function projectAssetDetailShowsVersionHistory(
  kind: MutableAssetKind,
): boolean {
  return kind === "skills" || kind === "agents";
}

export function projectAssetDetailVersionPickerPlacement(
  kind: MutableAssetKind,
): "before-editor" | "version-section" {
  return kind === "agents" ? "before-editor" : "version-section";
}

export function projectAgentVersionWorkbenchSelection<T extends { id: string }>(
  selectedVersion: T | null,
  authoringBaseVersion: T | null,
): { version: T | null; canAuthor: boolean } {
  return {
    version: selectedVersion,
    canAuthor:
      selectedVersion !== null &&
      selectedVersion.id === authoringBaseVersion?.id,
  };
}

export function ProjectAgentVersionWorkbenchSlot<T extends { id: string }>({
  selectedVersion,
  authoringBaseVersion,
  canAuthor,
  render,
}: {
  selectedVersion: T | null;
  authoringBaseVersion: T | null;
  canAuthor: boolean;
  render: (version: T | null, canAuthor: boolean) => ReactNode;
}) {
  const selection = projectAgentVersionWorkbenchSelection(
    selectedVersion,
    authoringBaseVersion,
  );

  return (
    <div
      data-agent-version-id={selection.version?.id}
      className="border-border/70 border-t pt-5"
    >
      {render(selection.version, canAuthor && selection.canAuthor)}
    </div>
  );
}

export function projectAgentVersionCanRestore(
  kind: MutableAssetKind,
  scope: ProjectAssetItem["scope"],
  canAuthor: boolean,
  version: ProjectAgentRecoveryVersion | null,
  currentPublishedVersionId: string | null,
): boolean {
  const recoverableVersion =
    version?.workflow_status === "published"
      ? version.id !== currentPublishedVersionId
      : version?.workflow_status === "draft" &&
        version.supersedes_version_id !== currentPublishedVersionId;
  return (
    kind === "agents" && scope === "project" && canAuthor && recoverableVersion
  );
}

export function projectAgentStaleDraftPublishReason(
  version: ProjectAgentRecoveryVersion | null,
  currentPublishedVersionId: string | null,
): string | null {
  return version?.workflow_status === "draft" &&
    version.supersedes_version_id !== currentPublishedVersionId
    ? "此草稿基于旧的发布版本，不能直接发布。请基于此版本创建新草稿，再发布新草稿。"
    : null;
}

export function ProjectAgentVersionRecoveryControls({
  kind,
  scope,
  canAuthor,
  version,
  currentPublishedVersionId,
  actionPending,
  dirty,
  versionSelectionPending,
  onCreateDraft,
}: {
  kind: MutableAssetKind;
  scope: ProjectAssetItem["scope"];
  canAuthor: boolean;
  version: ProjectAgentRecoveryVersion | null;
  currentPublishedVersionId: string | null;
  actionPending: boolean;
  dirty: boolean;
  versionSelectionPending: boolean;
  onCreateDraft: () => void;
}) {
  if (!canAuthor) return null;

  const reason = projectAgentStaleDraftPublishReason(
    version,
    currentPublishedVersionId,
  );
  const canCreateDraft = projectAgentVersionCanRestore(
    kind,
    scope,
    canAuthor,
    version,
    currentPublishedVersionId,
  );
  if (!reason && !canCreateDraft) return null;

  return (
    <div className="border-border/70 bg-muted/25 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3">
      {reason ? (
        <p role="status" className="text-warning-foreground text-sm">
          {reason}
        </p>
      ) : (
        <p className="text-muted-foreground text-sm">
          可复制此历史版本的全部设置，创建一个基于当前发布版本的新草稿。
        </p>
      )}
      {canCreateDraft ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={actionPending || dirty || versionSelectionPending}
          title={
            dirty
              ? "请先保存或放弃当前未保存修改"
              : versionSelectionPending
                ? "正在切换版本，请稍候"
                : undefined
          }
          onClick={onCreateDraft}
        >
          基于此版本创建新草稿
        </Button>
      ) : null}
    </div>
  );
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

export function projectAssetRequestedVersionResolution(
  versions: readonly Pick<AssetVersion, "id">[],
  requestedVersionId: string | null,
  historyReady: boolean,
): "none" | "pending" | "available" | "missing" {
  if (!requestedVersionId) return "none";
  if (!historyReady) return "pending";
  return versions.some((version) => version.id === requestedVersionId)
    ? "available"
    : "missing";
}

export function projectSkillCredentialRepairVersionId(
  error: unknown,
  currentPublishedVersionId: string | null,
): string | null {
  return projectSkillCredentialSetupRequired(error)
    ? currentPublishedVersionId
    : null;
}

export function projectAgentPreviousVersion(
  versions: readonly AssetVersion[],
  selectedVersion: AssetVersion | null,
): AssetVersion | null {
  if (!selectedVersion || !("agent_id" in selectedVersion)) {
    return null;
  }
  const previous =
    versions.find(
      (version) =>
        "agent_id" in version &&
        version.agent_id === selectedVersion.agent_id &&
        version.version_number === selectedVersion.version_number - 1,
    ) ?? null;
  return previous && "agent_id" in previous ? previous : null;
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

export function skillPublishStaleBaseMessage(
  {
    liveVersionNumber,
    baseVersionNumber,
  }: {
    liveVersionNumber: number | null;
    baseVersionNumber: number | null;
  },
  copy: Translations["skills"]["builder"]["publish"],
): string {
  if (liveVersionNumber && baseVersionNumber) {
    return copy.staleNamed(liveVersionNumber, baseVersionNumber);
  }
  return copy.staleGeneric;
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
type VersionStatus = ReturnType<typeof projectAssetVersionDisplayStatus>;

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
  credentialBindingsDirty: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onCredentialBindingsDirtyChange: (dirty: boolean) => void;
  onPublishValidityChange: (valid: boolean) => void;
  onVersionCreated: (
    versionId: string,
    options?: { focusCredentials?: boolean },
  ) => void;
  focusSkillCredentials: boolean;
  onSkillCredentialsFocused: () => void;
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

export function projectAssetVersionDisplayStatus(
  version: AssetVersion,
):
  | "draft"
  | "pending_approval"
  | "published"
  | "rejected"
  | "active"
  | "retired"
  | "revoked" {
  if (
    "governance_status" in version &&
    version.governance_status === "revoked"
  ) {
    return "revoked";
  }
  return "workflow_status" in version
    ? version.workflow_status
    : version.status;
}

function AgentVersionCapabilitySummary({
  version,
}: {
  version: Extract<AssetVersion, { agent_id: string }>;
}) {
  return (
    <dl className="grid gap-3 sm:grid-cols-3">
      <div className="bg-muted/35 rounded-xl p-4">
        <dt className="text-muted-foreground text-xs">内置工具组</dt>
        <dd className="mt-2 text-sm font-medium">
          {version.tool_groups.length} 个
        </dd>
      </div>
      <div className="bg-muted/35 rounded-xl p-4">
        <dt className="text-muted-foreground text-xs">Skill</dt>
        <dd className="mt-2 text-sm font-medium">
          {version.skill_version_ids.length} 个
        </dd>
      </div>
      <div className="bg-muted/35 rounded-xl p-4">
        <dt className="text-muted-foreground text-xs">MCP</dt>
        <dd className="mt-2 text-sm font-medium">
          {version.mcp_version_ids.length} 个
        </dd>
      </div>
    </dl>
  );
}

export function versionPublishDisabled(
  actionPending: boolean,
  versionDirty: boolean,
  versionSelectionPending = false,
  versionInvalid = false,
): boolean {
  return (
    versionActionDisabled(actionPending, versionSelectionPending) ||
    versionDirty ||
    versionInvalid
  );
}

export function projectAssetDetailDirty(
  versionDirty: boolean,
  credentialBindingsDirty: boolean,
): boolean {
  return versionDirty || credentialBindingsDirty;
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
  additionalActions,
  canAuthor,
  canDelete,
  editing,
  hasSelectedVersion,
  versionDirty,
  versionSelectionPending,
  onCreateVersion,
  onDelete,
  publishAction,
}: {
  actionPending: boolean;
  additionalActions?: ReactNode;
  canAuthor: boolean;
  canDelete: boolean;
  editing: boolean;
  hasSelectedVersion: boolean;
  versionDirty: boolean;
  versionSelectionPending: boolean;
  onCreateVersion: () => void;
  onDelete: () => void;
  publishAction?: ReactNode;
}) {
  return (
    <>
      {canAuthor && hasSelectedVersion && !editing ? (
        <Button
          type="button"
          variant="outline"
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
      {publishAction}
      {additionalActions}
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

export function ProjectAgentDetailActions({
  actionPending,
  canDelete,
  lifecycleActions,
  onDelete,
  publishAction,
}: {
  actionPending: boolean;
  canDelete: boolean;
  lifecycleActions?: ReactNode;
  onDelete: () => void;
  publishAction?: ReactNode;
}) {
  return (
    <>
      {publishAction}
      {lifecycleActions}
      {canDelete ? (
        <Button
          type="button"
          variant="destructive"
          disabled={actionPending}
          onClick={onDelete}
        >
          删除
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
  onDirtyChange,
  onVersionCreated,
  onRequestedVersionHandled,
  renderDetailActions,
  renderAssetEditor,
  renderVersion,
  focusSkillCredentials = false,
  onSkillCredentialsFocused,
  onSkillCredentialSetupRequired,
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
  onDirtyChange?: (dirty: boolean) => void;
  onVersionCreated: (assetId: string, versionId: string) => void;
  onRequestedVersionHandled: (
    assetId: string,
    versionId: string,
    available: boolean,
  ) => void;
  renderDetailActions?: (context: {
    item: ProjectAssetItem;
    editing: boolean;
  }) => ReactNode;
  renderAssetEditor?: (
    effectiveVersion: AssetVersion | null,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  renderVersion?: (
    version: AssetVersion,
    context: ProjectAssetVersionRenderContext,
  ) => ReactNode;
  focusSkillCredentials?: boolean;
  onSkillCredentialsFocused?: () => void;
  onSkillCredentialSetupRequired?: (versionId: string | null) => void;
}) {
  const { t } = useI18n();
  const { models } = useModels({ enabled: open && kind === "agents" });
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
  const showsVersionContent = true;
  const versionTerms = projectAssetDetailVersionTerms(kind, item.scope);
  const revisionCopy = projectAssetDetailRevisionCopy(kind);
  const discardCopy = projectAssetDiscardCopy(kind);
  const publish = usePublishProjectAssetVersion(accountId, projectId, kind);
  const changeStatus = useChangeProjectAssetStatus(accountId, projectId, kind);
  const deleteSkill = useDeleteProjectSkill(accountId, projectId);
  const deleteAgent = useDeleteProjectAgent(accountId, projectId);
  const deleteMcp = useDeleteProjectMcp(accountId, projectId);
  const restoreAgentVersion = useRestoreProjectAgentVersion(
    accountId,
    projectId,
  );
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [versionDirty, setVersionDirty] = useState(false);
  const [credentialBindingsDirty, setCredentialBindingsDirty] = useState(false);
  const [versionEditing, setVersionEditing] = useState(false);
  const [skillDeleteSnapshot, setSkillDeleteSnapshot] =
    useState<ProjectSkillDeleteSnapshot | null>(null);
  const [agentDeleteSnapshot, setAgentDeleteSnapshot] =
    useState<ProjectAgentDeleteSnapshot | null>(null);
  const [mcpDeleteSnapshot, setMcpDeleteSnapshot] =
    useState<ProjectMcpDeleteSnapshot | null>(null);
  const [agentRestoreTarget, setAgentRestoreTarget] = useState<Extract<
    AssetVersion,
    { agent_id: string }
  > | null>(null);
  const [publishStaleVersion, setPublishStaleVersion] =
    useState<AssetVersion | null>(null);
  const [skillPublishVersion, setSkillPublishVersion] = useState<Extract<
    AssetVersion,
    { skill_id: string }
  > | null>(null);
  const [skillPublishValidity, setSkillPublishValidity] = useState<{
    versionId: string;
    valid: boolean;
  } | null>(null);
  const [discardAction, setDiscardAction] = useState<
    | { type: "close" }
    | {
        type: "version";
        versionId: string;
        focusSkillCredentials?: boolean;
      }
    | null
  >(null);
  const updateVersionDirty = useCallback((dirty: boolean) => {
    setVersionDirty(dirty);
  }, []);
  const updateCredentialBindingsDirty = useCallback(
    (dirty: boolean) => setCredentialBindingsDirty(dirty),
    [],
  );
  const detailDirty = projectAssetDetailDirty(
    versionDirty,
    credentialBindingsDirty,
  );

  useEffect(() => {
    onDirtyChange?.(detailDirty);
  }, [detailDirty, onDirtyChange]);

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
  const selectedVersionIdentity = selectedVersion?.id ?? null;
  const selectedSkillPublishValid =
    kind !== "skills" ||
    (skillPublishValidity?.versionId === selectedVersionIdentity &&
      skillPublishValidity?.valid === true);
  const handleSkillPublishValidityChange = useCallback(
    (valid: boolean) => {
      if (!selectedVersionIdentity) return;
      setSkillPublishValidity((current) =>
        current?.versionId === selectedVersionIdentity &&
        current.valid === valid
          ? current
          : { versionId: selectedVersionIdentity, valid },
      );
    },
    [selectedVersionIdentity],
  );
  const selectedAgentVersion =
    selectedVersion && "agent_id" in selectedVersion ? selectedVersion : null;
  const previousAgentVersion = projectAgentPreviousVersion(
    versions,
    selectedVersion,
  );
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
  const agentAuthoringVersion = agentAuthoringBaseVersion(
    versions.filter(
      (version): version is Extract<AssetVersion, { agent_id: string }> =>
        "agent_id" in version,
    ),
    item.current_published_version_id,
  );
  const agentWorkbenchSelection = projectAgentVersionWorkbenchSelection(
    selectedAgentVersion,
    agentAuthoringVersion,
  );
  const editorVersion =
    kind === "agents" ? agentWorkbenchSelection.version : effectiveVersion;

  useEffect(() => {
    if (!open) return;
    const requestedVersionResolution = projectAssetRequestedVersionResolution(
      versions,
      requestedVersionId,
      history.isSuccess,
    );
    if (requestedVersionResolution === "pending") return;
    if (requestedVersionResolution === "missing" && requestedVersionId) {
      onRequestedVersionHandled(item.id, requestedVersionId, false);
    }
    if (versions.length === 0) return;
    if (kind === "mcp-servers") {
      const preferredId = projectAssetDetailPreferredVersionId(
        kind,
        item.scope,
        versions,
        item.current_published_version_id,
      );
      setSelectedVersionId(preferredId);
      if (requestedVersionResolution === "available" && requestedVersionId) {
        onRequestedVersionHandled(item.id, requestedVersionId, true);
      }
      return;
    }
    if (requestedVersionResolution === "available" && requestedVersionId) {
      setSelectedVersionId(requestedVersionId);
      onRequestedVersionHandled(item.id, requestedVersionId, true);
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
    history.isSuccess,
    kind,
    onRequestedVersionHandled,
    open,
    requestedVersionId,
    versions,
  ]);

  useEffect(() => {
    if (open) return;
    setSelectedVersionId("");
    updateVersionDirty(false);
    updateCredentialBindingsDirty(false);
    setVersionEditing(false);
    setSkillDeleteSnapshot(null);
    setAgentDeleteSnapshot(null);
    setMcpDeleteSnapshot(null);
    setAgentRestoreTarget(null);
    setSkillPublishVersion(null);
    setSkillPublishValidity(null);
    setDiscardAction(null);
  }, [open, updateCredentialBindingsDirty, updateVersionDirty]);

  useEffect(
    () => () => {
      onDirtyChange?.(false);
    },
    [onDirtyChange],
  );

  useEffect(() => {
    if (kind !== "agents") setVersionEditing(false);
  }, [kind, selectedVersionId]);

  const canAuthor =
    item.scope === "project" && projectAssetCanAuthor(item, kind);
  const canPublishSelectedAgentVersion =
    kind === "agents" &&
    projectAgentVersionCanPublish(
      item,
      projectCapabilities,
      selectedAgentVersion,
    );
  const canPublishSelectedSkillVersion =
    kind === "skills" &&
    projectSkillVersionCanPublish(
      item,
      projectCapabilities,
      selectedVersion && "workflow_status" in selectedVersion
        ? selectedVersion
        : null,
    );
  const lifecycleActions =
    item.scope === "project"
      ? projectAssetDetailLifecycleActions(kind, item, projectCapabilities)
      : [];
  const canDeleteAsset = projectAssetCanDelete(kind, item);
  const canCreateVersion = projectAssetCanCreateVersion(kind, canAuthor);
  const showSkillDetailActions =
    kind === "skills" &&
    (canAuthor || canDeleteAsset || canPublishSelectedSkillVersion);
  const canExportSkill =
    kind === "skills" && projectCapabilities.includes("shared_assets.edit");
  const showAgentDetailActions =
    kind === "agents" && (canPublishSelectedAgentVersion || canDeleteAsset);
  const showDetailActions =
    canCreateVersion ||
    lifecycleActions.length > 0 ||
    showSkillDetailActions ||
    showAgentDetailActions ||
    canExportSkill;
  const showVersionPicker = showsVersionHistory && versions.length > 1;
  const versionActions = useMemo(() => {
    if (
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
    deleteMcp.isPending ||
    restoreAgentVersion.isPending;
  const versionSelectionPending = requestedVersionId !== null;
  const actionError =
    publish.error ?? changeStatus.error ?? restoreAgentVersion.error;
  const optimisticSkillStatus =
    kind === "skills" && changeStatus.isPending
      ? changeStatus.variables?.action === "activate"
      : undefined;

  const handleWorkbenchVersionCreated = useCallback(
    (versionId: string, options?: { focusCredentials?: boolean }) => {
      updateVersionDirty(false);
      setVersionEditing(false);
      onVersionCreated(item.id, versionId);
      if (options?.focusCredentials) {
        onSkillCredentialSetupRequired?.(versionId);
      }
      void history.refetch();
    },
    [
      history,
      item.id,
      onSkillCredentialSetupRequired,
      onVersionCreated,
      updateVersionDirty,
    ],
  );

  function publishSelectedVersion(version: AssetVersion) {
    if ("skill_id" in version) {
      setSkillPublishVersion(version);
      return;
    }
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
        onError: (error) => {
          if (
            error instanceof SharedAssetApiError &&
            error.code === "SKILL_PUBLISH_BASE_STALE"
          ) {
            setPublishStaleVersion(version);
          }
        },
      },
    );
  }

  function confirmStalePublish() {
    const version = publishStaleVersion;
    if (!version || publish.isPending) return;
    publish.mutate(
      {
        assetId: item.id,
        versionId: version.id,
        input: {
          expected_asset_version: item.version,
          acknowledge_stale_base: true,
        },
      },
      {
        onSuccess: (result) => {
          setPublishStaleVersion(null);
          publish.reset();
          setSelectedVersionId(result.data.id);
          handleWorkbenchVersionCreated(result.data.id);
        },
      },
    );
  }

  function requestOpenChange(next: boolean) {
    if (!next && detailDirty) {
      setDiscardAction({ type: "close" });
      return;
    }
    if (!next) setVersionEditing(false);
    onOpenChange(next);
  }

  function requestVersionChange(
    versionId: string,
    options: { focusSkillCredentials?: boolean } = {},
  ) {
    if (versionId === selectedVersionId) {
      if (options.focusSkillCredentials) {
        onSkillCredentialSetupRequired?.(versionId);
      }
      return;
    }
    if (detailDirty) {
      setDiscardAction({
        type: "version",
        versionId,
        focusSkillCredentials: options.focusSkillCredentials,
      });
      return;
    }
    setVersionEditing(false);
    setSelectedVersionId(versionId);
    if (options.focusSkillCredentials) {
      onSkillCredentialSetupRequired?.(versionId);
    }
  }

  function confirmDiscardNavigation() {
    const action = discardAction;
    setDiscardAction(null);
    updateVersionDirty(false);
    updateCredentialBindingsDirty(false);
    setVersionEditing(false);
    if (action?.type === "close") {
      onOpenChange(false);
    } else if (action?.type === "version") {
      setSelectedVersionId(action.versionId);
      if (action.focusSkillCredentials) {
        onSkillCredentialSetupRequired?.(action.versionId);
      }
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

  async function confirmAgentVersionRestore() {
    const target = agentRestoreTarget;
    if (!target) return;
    try {
      const result = await restoreAgentVersion.mutateAsync({
        assetId: item.id,
        versionId: target.id,
        input: { expected_asset_version: item.version },
      });
      setAgentRestoreTarget(null);
      setSelectedVersionId(result.data.id);
      handleWorkbenchVersionCreated(result.data.id);
    } catch {
      // The mapped public error remains visible in the confirmation dialog.
    }
  }

  function toggleProjectSkillStatus(checked: boolean) {
    if (kind !== "skills") return;
    const toggleState = projectSkillStatusToggleState(item);
    if (toggleState.disabled || toggleState.checked === checked) return;
    changeStatus.mutate(
      {
        assetId: item.id,
        action: checked ? "activate" : "suspend",
        input: { expected_asset_version: item.version },
      },
      {
        onError: (error) => {
          const repairVersionId = projectSkillCredentialRepairVersionId(
            error,
            item.current_published_version_id,
          );
          if (!projectSkillCredentialSetupRequired(error)) return;
          if (repairVersionId) {
            requestVersionChange(repairVersionId, {
              focusSkillCredentials: true,
            });
          } else {
            onSkillCredentialSetupRequired?.(null);
          }
        },
      },
    );
  }

  const versionPickerPlacement = projectAssetDetailVersionPickerPlacement(kind);
  const versionPicker = showVersionPicker ? (
    <div className="flex items-center justify-between gap-3">
      <h2 className="text-sm font-semibold">{versionTerms.history}</h2>
      <label className="text-muted-foreground flex items-center gap-2 text-xs">
        查看
        <select
          aria-label={revisionCopy.viewAria}
          value={selectedVersionId}
          disabled={versionSelectionPending}
          onChange={(event) => requestVersionChange(event.target.value)}
          className="border-input bg-background h-8 rounded-md border px-2 text-xs"
        >
          {versions.map((version) => (
            <option key={version.id} value={version.id}>
              {revisionCopy.label(version.version_number)} ·{" "}
              {VERSION_STATUS_LABEL[projectAssetVersionDisplayStatus(version)]}
            </option>
          ))}
        </select>
      </label>
    </div>
  ) : null;

  const editorRenderContext = (
    versionCanAuthor: boolean,
  ): ProjectAssetVersionRenderContext => ({
    accountId,
    projectId,
    item,
    canAuthor: versionCanAuthor,
    editing: versionEditing,
    credentialBindingsDirty,
    onEditingChange: setVersionEditing,
    onDirtyChange: updateVersionDirty,
    onCredentialBindingsDirtyChange: updateCredentialBindingsDirty,
    onPublishValidityChange: handleSkillPublishValidityChange,
    onVersionCreated: handleWorkbenchVersionCreated,
    focusSkillCredentials,
    onSkillCredentialsFocused: onSkillCredentialsFocused ?? (() => undefined),
  });

  return (
    <>
      <Sheet open={open} onOpenChange={requestOpenChange}>
        <SheetContent
          closeLabel="关闭详情"
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

              {showDetailActions ? (
                <div className="flex flex-wrap items-start gap-2">
                  {canCreateVersion && (
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
                  {kind === "agents" ? (
                    <ProjectAgentDetailActions
                      actionPending={actionPending}
                      canDelete={canDeleteAsset}
                      lifecycleActions={lifecycleActions.map((action) => (
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
                      publishAction={
                        selectedVersion !== null &&
                        versionActions.includes("publish") &&
                        canPublishSelectedAgentVersion ? (
                          <Button
                            type="button"
                            disabled={versionPublishDisabled(
                              actionPending ||
                                Boolean(selectedRuntimeBlockReason),
                              detailDirty,
                              versionSelectionPending,
                            )}
                            title={
                              selectedRuntimeBlockReason ??
                              (detailDirty
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
                        ) : undefined
                      }
                      onDelete={() => {
                        deleteAgent.reset();
                        setAgentDeleteSnapshot(
                          createProjectAgentDeleteSnapshot(item, Date.now()),
                        );
                      }}
                    />
                  ) : (
                    lifecycleActions.map((action) => (
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
                    ))
                  )}
                  {kind === "skills" ? (
                    <>
                      {canExportSkill ? (
                        <SkillExportButton
                          versionNumber={
                            selectedVersion?.version_number ?? null
                          }
                          blockReason={skillExportBlockReason({
                            hasVersion: selectedVersion !== null,
                            unsaved: detailDirty,
                            loading: versionSelectionPending,
                            revoked:
                              selectedVersion !== null &&
                              "governance_status" in selectedVersion &&
                              selectedVersion.governance_status === "revoked",
                            unpublished:
                              item.scope === "system" &&
                              selectedVersion !== null &&
                              "workflow_status" in selectedVersion &&
                              selectedVersion.workflow_status !== "published",
                          })}
                          download={() => {
                            if (!selectedVersion) {
                              return Promise.reject(
                                new Error("No persisted Skill version"),
                              );
                            }
                            return exportProjectSkillVersion(
                              projectId,
                              item.id,
                              selectedVersion.id,
                            );
                          }}
                        />
                      ) : null}
                      <ProjectSkillDetailActions
                        actionPending={actionPending}
                        additionalActions={renderDetailActions?.({
                          item,
                          editing: versionEditing,
                        })}
                        canAuthor={canAuthor}
                        canDelete={canDeleteAsset}
                        editing={versionEditing}
                        hasSelectedVersion={selectedVersion !== null}
                        versionDirty={detailDirty}
                        versionSelectionPending={versionSelectionPending}
                        onCreateVersion={() => setVersionEditing(true)}
                        publishAction={
                          selectedVersion !== null &&
                          versionActions.includes("publish") &&
                          canPublishSelectedSkillVersion ? (
                            <Button
                              type="button"
                              disabled={versionPublishDisabled(
                                actionPending ||
                                  Boolean(selectedRuntimeBlockReason),
                                detailDirty,
                                versionSelectionPending,
                                !selectedSkillPublishValid,
                              )}
                              title={
                                selectedRuntimeBlockReason ??
                                (detailDirty
                                  ? "请先保存或放弃当前未保存修改"
                                  : !selectedSkillPublishValid
                                    ? t.skills.secrets.publishBlocked
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
                          ) : undefined
                        }
                        onDelete={() => {
                          deleteSkill.reset();
                          setSkillDeleteSnapshot(
                            createProjectSkillDeleteSnapshot(item, Date.now()),
                          );
                        }}
                      />
                    </>
                  ) : null}
                </div>
              ) : null}

              {versionPickerPlacement === "before-editor"
                ? versionPicker
                : null}

              {kind === "agents" ? (
                <ProjectAgentVersionRecoveryControls
                  kind={kind}
                  scope={item.scope}
                  canAuthor={canAuthor}
                  version={selectedAgentVersion}
                  currentPublishedVersionId={item.current_published_version_id}
                  actionPending={actionPending}
                  dirty={detailDirty}
                  versionSelectionPending={versionSelectionPending}
                  onCreateDraft={() => {
                    if (!selectedAgentVersion) return;
                    restoreAgentVersion.reset();
                    setAgentRestoreTarget(selectedAgentVersion);
                  }}
                />
              ) : null}

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
                ) : kind === "agents" ? (
                  <ProjectAgentVersionWorkbenchSlot
                    selectedVersion={selectedAgentVersion}
                    authoringBaseVersion={agentAuthoringVersion}
                    canAuthor={canAuthor && !versionSelectionPending}
                    render={(version, versionCanAuthor) =>
                      renderAssetEditor(
                        version,
                        editorRenderContext(versionCanAuthor),
                      )
                    }
                  />
                ) : (
                  <div className="border-border/70 border-t pt-5">
                    {renderAssetEditor(
                      editorVersion,
                      editorRenderContext(
                        canAuthor && !versionSelectionPending,
                      ),
                    )}
                  </div>
                )
              ) : null}

              {showsVersionContent ? (
                <section className="space-y-3">
                  {versionPickerPlacement === "version-section"
                    ? versionPicker
                    : null}

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
                      {kind === "mcp-servers" &&
                        item.scope === "project" &&
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
                                    detailDirty,
                                    versionSelectionPending,
                                  )}
                                  title={
                                    selectedRuntimeBlockReason ??
                                    (detailDirty
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
                        {"agent_id" in selectedVersion ? (
                          <AgentVersionCapabilitySummary
                            version={selectedVersion}
                          />
                        ) : null}
                        {renderVersion?.(
                          detailContentVersion ?? selectedVersion,
                          {
                            accountId,
                            projectId,
                            item,
                            canAuthor: canAuthor && !versionSelectionPending,
                            editing: versionEditing,
                            credentialBindingsDirty,
                            onEditingChange: setVersionEditing,
                            onDirtyChange: updateVersionDirty,
                            onCredentialBindingsDirtyChange:
                              updateCredentialBindingsDirty,
                            onPublishValidityChange:
                              handleSkillPublishValidityChange,
                            onVersionCreated: handleWorkbenchVersionCreated,
                            focusSkillCredentials,
                            onSkillCredentialsFocused:
                              onSkillCredentialsFocused ?? (() => undefined),
                          },
                        )}
                      </div>

                      {selectedAgentVersion && previousAgentVersion ? (
                        <details className="border-border/70 rounded-xl border px-4 py-3">
                          <summary className="cursor-pointer text-sm font-medium">
                            与前一版本比较
                          </summary>
                          <div className="mt-3 max-h-[32rem] overflow-auto">
                            <AssetVersionDiff
                              previous={previousAgentVersion}
                              current={selectedAgentVersion}
                              includeAgentDocuments
                              models={models}
                            />
                          </div>
                        </details>
                      ) : null}

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
            deleteSkill.error
              ? projectSkillDeleteErrorMessage(deleteSkill.error)
              : null
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
            deleteAgent.error
              ? projectAgentDeleteErrorMessage(deleteAgent.error)
              : null
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

      {skillPublishVersion !== null ? (
        <SkillPublishDialog
          accountId={accountId}
          projectId={projectId}
          item={item}
          version={skillPublishVersion}
          open
          onOpenChange={(next) => {
            if (!next) setSkillPublishVersion(null);
          }}
          onPublished={(versionId) => {
            setSkillPublishVersion(null);
            setSelectedVersionId(versionId);
            handleWorkbenchVersionCreated(versionId);
          }}
          onConfigureCredentials={() => {
            requestVersionChange(skillPublishVersion.id, {
              focusSkillCredentials: true,
            });
          }}
        />
      ) : null}

      <Dialog
        open={agentRestoreTarget !== null}
        onOpenChange={(next) => {
          if (next || restoreAgentVersion.isPending) return;
          setAgentRestoreTarget(null);
          restoreAgentVersion.reset();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>基于这个 Agent 版本创建新草稿？</DialogTitle>
            <DialogDescription>
              将复制版本 {agentRestoreTarget?.version_number ?? "-"}
              的全部设定与工具绑定，并创建一个新草稿。管理员发布前不会影响当前运行版本；现有版本记录不会被覆盖或删除。
            </DialogDescription>
          </DialogHeader>
          {restoreAgentVersion.error ? (
            <ErrorNotice error={restoreAgentVersion.error} />
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={restoreAgentVersion.isPending}
              onClick={() => {
                setAgentRestoreTarget(null);
                restoreAgentVersion.reset();
              }}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={restoreAgentVersion.isPending}
              onClick={() => void confirmAgentVersionRestore()}
            >
              {restoreAgentVersion.isPending ? "恢复中…" : "确认创建新草稿"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={publishStaleVersion !== null}
        onOpenChange={(next) => {
          if (next || publish.isPending) return;
          setPublishStaleVersion(null);
          publish.reset();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.skills.builder.publish.staleTitle}</DialogTitle>
            <DialogDescription>
              {skillPublishStaleBaseMessage(
                {
                  liveVersionNumber: currentPublished?.version_number ?? null,
                  baseVersionNumber:
                    publishStaleVersion &&
                    "supersedes_version_id" in publishStaleVersion
                      ? (versions.find(
                          (version) =>
                            version.id ===
                            publishStaleVersion.supersedes_version_id,
                        )?.version_number ?? null)
                      : null,
                },
                t.skills.builder.publish,
              )}
            </DialogDescription>
          </DialogHeader>
          {publish.error &&
          !(
            publish.error instanceof SharedAssetApiError &&
            publish.error.code === "SKILL_PUBLISH_BASE_STALE"
          ) ? (
            <ErrorNotice error={publish.error} />
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={publish.isPending}
              onClick={() => {
                setPublishStaleVersion(null);
                publish.reset();
              }}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={publish.isPending}
              onClick={confirmStalePublish}
            >
              {publish.isPending ? "发布中…" : "确认覆盖并发布"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
