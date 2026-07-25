import type { AgentDependencyOption } from "@/components/admin/assets/admin-asset-dialogs";
import type { ProjectAssetList } from "@/core/shared-assets";

export function dependencyVersionOptions(
  data: ProjectAssetList | undefined,
): AgentDependencyOption[] {
  if (!data) return [];
  const options = new Map<string, AgentDependencyOption>();
  for (const item of [...data.system_items, ...data.project_items]) {
    if (!item.current_published_version_id) continue;
    options.set(item.current_published_version_id, {
      id: item.current_published_version_id,
      label: `${item.scope === "system" ? "系统" : "项目"} · ${item.display_name}（${item.slug}）`,
    });
  }
  return [...options.values()].sort((left, right) =>
    left.label.localeCompare(right.label, "zh-CN"),
  );
}
