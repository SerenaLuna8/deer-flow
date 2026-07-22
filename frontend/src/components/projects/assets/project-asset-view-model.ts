import type { Capability } from "@/core/projects/types";
import type { ProjectAssetItem } from "@/core/shared-assets";

type ProjectLifecycleItem = Pick<ProjectAssetItem, "capabilities" | "status">;

export function projectAssetCanAuthor(item: ProjectLifecycleItem): boolean {
  return (
    item.status === "active" && item.capabilities.includes("shared_assets.edit")
  );
}

export function projectAssetLifecycleActions(
  item: ProjectLifecycleItem,
  projectCapabilities: readonly Capability[] = item.capabilities,
) {
  const canArchive = item.capabilities.includes("shared_assets.edit");
  const canSuspend = projectCapabilities.includes(
    "shared_assets.manage_bindings",
  );

  if (item.status === "active") {
    return [
      ...(canArchive ? (["archive"] as const) : []),
      ...(canSuspend ? (["suspend"] as const) : []),
    ];
  }
  if (item.status === "archived") {
    return canSuspend ? (["suspend"] as const) : [];
  }
  return canArchive ? (["archive"] as const) : [];
}
