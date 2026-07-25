import { useMemo } from "react";

import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { useProjectAssets } from "./hooks";
import type { ProjectAssetList } from "./types";

export type ProjectSlashSkill = {
  name: string;
  description: string;
  enabled: boolean;
};

export function projectSlashSkills(
  catalog: ProjectAssetList,
): ProjectSlashSkill[] {
  const published = (item: ProjectAssetList["project_items"][number]) =>
    item.current_published_version_id !== null;
  const executable = (item: ProjectAssetList["project_items"][number]) =>
    item.status === "active" &&
    item.capabilities.includes("shared_assets.execute");

  return [
    ...catalog.project_items.filter(published).map((item) => ({
      name: item.slug,
      description: item.display_name,
      enabled: executable(item),
    })),
    ...catalog.system_items.filter(published).map((item) => ({
      name: item.slug,
      description: item.display_name,
      enabled: executable(item) && item.binding?.enabled === true,
    })),
  ];
}

export function useProjectSlashSkills() {
  const { scope } = useProjectPrivateWorkScope();
  const query = useProjectAssets(scope.accountId, scope.projectId, "skills");
  const skills = useMemo(() => {
    const catalog = query.data as ProjectAssetList | undefined;
    if (!catalog) return [];
    return projectSlashSkills(catalog);
  }, [query.data]);
  return { skills, isLoading: query.isLoading, error: query.error };
}
