import { assetLifecycleActions } from "@/components/admin/assets/admin-asset-view-model";
import type { ProjectAssetItem } from "@/core/shared-assets";

type ProjectLifecycleItem = Pick<ProjectAssetItem, "capabilities" | "status">;

export function projectAssetCanAuthor(item: ProjectLifecycleItem): boolean {
  return (
    item.status === "active" && item.capabilities.includes("shared_assets.edit")
  );
}

export function projectAssetLifecycleActions(item: ProjectLifecycleItem) {
  if (!item.capabilities.includes("shared_assets.edit")) return [];
  return assetLifecycleActions(item.status);
}
