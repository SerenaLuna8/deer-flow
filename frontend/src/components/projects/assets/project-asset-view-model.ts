import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import type { Capability } from "@/core/projects/types";
import {
  SharedAssetApiError,
  type AssetListKind,
  type ProjectAssetItem,
} from "@/core/shared-assets";

type ProjectLifecycleItem = Pick<ProjectAssetItem, "capabilities" | "status">;
type ProjectSkillDeleteItem = Pick<ProjectAssetItem, "capabilities" | "scope">;
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

export type ProjectAssetDetailLifecycleAction<Kind extends MutableAssetKind> =
  Kind extends "skills" ? never : "archive" | "suspend";

export function projectAssetCanAuthor(
  item: ProjectLifecycleItem,
  kind?: MutableAssetKind,
): boolean {
  return (
    item.capabilities.includes("shared_assets.edit") &&
    (item.status === "active" ||
      (kind === "skills" && item.status === "suspended"))
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
  const canArchive = item.capabilities.includes("shared_assets.edit");
  const canSuspend = projectCapabilities.includes(
    "shared_assets.manage_bindings",
  );

  if (item.status === "active") {
    return [
      ...(canArchive ? ["archive" as const] : []),
      ...(canSuspend ? ["suspend" as const] : []),
    ] as ProjectAssetDetailLifecycleAction<Kind>[];
  }
  if (item.status === "archived") {
    return (
      canSuspend ? ["suspend" as const] : []
    ) as ProjectAssetDetailLifecycleAction<Kind>[];
  }
  return (
    canArchive ? ["archive" as const] : []
  ) as ProjectAssetDetailLifecycleAction<Kind>[];
}

export type ProjectSkillStatusToggleState = {
  checked: boolean;
  disabled: boolean;
  disabledReason: string | null;
};

export function projectSkillStatusToggleState(
  item: ProjectSkillStatusItem,
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
        ? "请先发布版本"
        : null,
  };
}

export function projectSkillCanDelete(
  kind: MutableAssetKind,
  item: ProjectSkillDeleteItem,
): boolean {
  return (
    kind === "skills" &&
    item.scope === "project" &&
    item.capabilities.includes("shared_assets.edit")
  );
}
