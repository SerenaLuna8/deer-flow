import {
  adminAssetErrorMessage,
  projectMcpVersionErrorMessage,
} from "@/components/admin/assets/admin-asset-view-model";
import type { Capability } from "@/core/projects/types";
import {
  SharedAssetApiError,
  type AdminProjectAssetStatusAction,
  type AssetListKind,
  type ProjectAssetItem,
} from "@/core/shared-assets";

type ProjectLifecycleItem = Pick<
  ProjectAssetItem,
  "capabilities" | "current_published_version_id" | "status"
>;
type ProjectAssetDeleteItem = Pick<
  ProjectAssetItem,
  "capabilities" | "current_published_version_id" | "scope"
>;
type ProjectAgentPublishItem = Pick<
  ProjectAssetItem,
  "capabilities" | "current_published_version_id" | "scope"
>;
type ProjectSkillStatusItem = Pick<
  ProjectAssetItem,
  "capabilities" | "current_published_version_id" | "scope" | "status"
>;
type MutableAssetKind = Exclude<AssetListKind, "credentials">;

export function projectAssetCreateErrorMessage(
  kind: MutableAssetKind,
  error: unknown,
): string {
  if (
    kind === "skills" &&
    error instanceof SharedAssetApiError &&
    error.status === 409
  ) {
    return "当前项目已存在同名 Skill，请更换名称或标识。";
  }
  return adminAssetErrorMessage(error);
}

export function projectConfiguredMcpErrorMessage(error: unknown): string {
  if (error instanceof SharedAssetApiError && error.status === 409) {
    return "当前项目已存在相同标识的 MCP。请修改名称或标识后重试。";
  }
  return projectMcpVersionErrorMessage(error);
}

export function projectMcpDeleteErrorMessage(error: unknown): string {
  if (error instanceof SharedAssetApiError && error.status === 409) {
    return "该 MCP 状态已变化，或仍被 Agent、历史运行或 Credential 授权快照引用；刷新并解除引用后重试。";
  }
  return adminAssetErrorMessage(error);
}

export function projectSkillDeleteErrorMessage(error: unknown): string {
  if (error instanceof SharedAssetApiError && error.code === "ASSET_IN_USE") {
    return "无法删除此 Skill：仍有 Agent 或历史运行引用它的版本。可先停用以阻止后续使用；物理删除需解除 Agent 引用，并等待历史运行按保留策略清理后重试。";
  }
  if (error instanceof SharedAssetApiError && error.code === "ASSET_CONFLICT") {
    return "Skill 状态已发生变化，请刷新后重试。";
  }
  return adminAssetErrorMessage(error);
}

export function projectAgentDeleteErrorMessage(error: unknown): string {
  if (error instanceof SharedAssetApiError && error.code === "ASSET_CONFLICT") {
    return "Agent 状态已发生变化，请刷新后重试。";
  }
  return adminAssetErrorMessage(error);
}

export type ProjectAssetDetailLifecycleAction<Kind extends MutableAssetKind> =
  Kind extends "skills" ? never : "activate" | "suspend";

export function projectAssetCanCreateVersion(
  kind: MutableAssetKind,
  canAuthor: boolean,
): boolean {
  return kind === "mcp-servers" && canAuthor;
}

export function projectAssetCanAuthor(
  item: ProjectLifecycleItem,
  kind?: MutableAssetKind,
): boolean {
  return (
    item.capabilities.includes("shared_assets.edit") &&
    (item.status === "active" ||
      ((kind === "skills" || kind === "agents") && item.status === "suspended"))
  );
}

export function projectAgentVersionCanPublish(
  item: ProjectAgentPublishItem,
  projectCapabilities: readonly Capability[],
  version: {
    workflow_status: string;
    supersedes_version_id: string | null;
  } | null,
): boolean {
  return (
    item.scope === "project" &&
    version?.workflow_status === "draft" &&
    version.supersedes_version_id === item.current_published_version_id &&
    projectCapabilities.includes("shared_assets.manage_bindings") &&
    item.capabilities.includes("shared_assets.manage_bindings")
  );
}

export function projectSkillVersionCanPublish(
  item: ProjectAgentPublishItem,
  projectCapabilities: readonly Capability[],
  version: { workflow_status: string } | null,
): boolean {
  return (
    item.scope === "project" &&
    version?.workflow_status === "draft" &&
    projectCapabilities.includes("shared_assets.manage_bindings") &&
    item.capabilities.includes("shared_assets.manage_bindings")
  );
}

export function projectAssetDetailLifecycleActions<
  Kind extends MutableAssetKind,
>(
  kind: Kind,
  item: ProjectLifecycleItem,
  projectCapabilities: readonly Capability[] = item.capabilities,
): ProjectAssetDetailLifecycleAction<Kind>[] {
  if (kind === "skills") {
    return [] as ProjectAssetDetailLifecycleAction<Kind>[];
  }
  const canSuspend = projectCapabilities.includes(
    "shared_assets.manage_bindings",
  );
  if (kind === "agents") {
    const canManageLifecycle =
      canSuspend && item.capabilities.includes("shared_assets.manage_bindings");
    if (!canManageLifecycle) {
      return [] as ProjectAssetDetailLifecycleAction<Kind>[];
    }
    return (
      item.status === "active"
        ? ["suspend" as const]
        : item.status === "suspended" &&
            item.current_published_version_id !== null
          ? ["activate" as const]
          : []
    ) as ProjectAssetDetailLifecycleAction<Kind>[];
  }
  if (kind === "mcp-servers") {
    const canManageLifecycle =
      canSuspend && item.capabilities.includes("shared_assets.manage_bindings");
    if (
      !canManageLifecycle ||
      item.status === "archived" ||
      item.current_published_version_id === null
    ) {
      return [] as ProjectAssetDetailLifecycleAction<Kind>[];
    }
    return (
      item.status === "active"
        ? ["suspend" as const]
        : item.status === "suspended"
          ? ["activate" as const]
          : []
    ) as ProjectAssetDetailLifecycleAction<Kind>[];
  }
  return [] as ProjectAssetDetailLifecycleAction<Kind>[];
}

export type AdminProjectAssetDetailLifecycleAction<
  Kind extends MutableAssetKind,
> = AdminProjectAssetStatusAction<Kind>;

export function adminProjectAssetDetailLifecycleActions<
  Kind extends MutableAssetKind,
>(
  kind: Kind,
  item: ProjectLifecycleItem,
): AdminProjectAssetDetailLifecycleAction<Kind>[] {
  if (kind === "skills") {
    return [] as AdminProjectAssetDetailLifecycleAction<Kind>[];
  }
  if (kind === "agents") {
    return projectAssetDetailLifecycleActions(
      kind,
      item,
      item.capabilities,
    ) as AdminProjectAssetDetailLifecycleAction<Kind>[];
  }
  const canArchive = item.capabilities.includes("shared_assets.edit");
  const canSuspend = item.capabilities.includes(
    "shared_assets.manage_bindings",
  );
  if (item.status === "active") {
    return [
      ...(canArchive ? ["archive" as const] : []),
      ...(canSuspend ? ["suspend" as const] : []),
    ] as AdminProjectAssetDetailLifecycleAction<Kind>[];
  }
  if (item.status === "archived") {
    return (
      canSuspend ? ["suspend" as const] : []
    ) as AdminProjectAssetDetailLifecycleAction<Kind>[];
  }
  return (
    canArchive ? ["archive" as const] : []
  ) as AdminProjectAssetDetailLifecycleAction<Kind>[];
}

export type ProjectSkillStatusToggleState = {
  checked: boolean;
  disabled: boolean;
  disabledReason: string | null;
};

export function projectSkillStatusToggleState(
  item: ProjectSkillStatusItem,
): ProjectSkillStatusToggleState {
  return projectStatusToggleState(item, "请先发布版本");
}

export function projectMcpStatusToggleState(
  item: ProjectSkillStatusItem,
): ProjectSkillStatusToggleState {
  return projectStatusToggleState(item, "请先发布配置");
}

function projectStatusToggleState(
  item: ProjectSkillStatusItem,
  unpublishedReason: string,
): ProjectSkillStatusToggleState {
  const checked = item.scope === "project" && item.status === "active";
  const canManage =
    item.scope === "project" &&
    item.capabilities.includes("shared_assets.manage_bindings");
  const canActivate =
    item.status === "suspended" && item.current_published_version_id !== null;
  const supportedStatus =
    item.status === "active" || item.status === "suspended";
  return {
    checked,
    disabled: !canManage || !supportedStatus || (!checked && !canActivate),
    disabledReason:
      canManage &&
      item.status === "suspended" &&
      item.current_published_version_id === null
        ? unpublishedReason
        : null,
  };
}

export function projectAssetCanDelete(
  kind: MutableAssetKind,
  item: ProjectAssetDeleteItem,
): boolean {
  const canEdit =
    (kind === "skills" || kind === "agents" || kind === "mcp-servers") &&
    item.scope === "project" &&
    item.capabilities.includes("shared_assets.edit");
  if (!canEdit) return false;
  const publishedPackageNeedsPublisher = kind === "agents" || kind === "skills";
  return (
    !publishedPackageNeedsPublisher ||
    item.current_published_version_id === null ||
    item.capabilities.includes("shared_assets.manage_bindings")
  );
}
