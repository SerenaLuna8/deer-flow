import { useMemo } from "react";

import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { useProjectAssets } from "./hooks";
import type { ProjectAssetList } from "./types";

export type ProjectSlashSkill = {
  name: string;
  description: string;
  enabled: boolean;
};

export function useProjectSlashSkills() {
  const { scope } = useProjectPrivateWorkScope();
  const query = useProjectAssets(scope.accountId, scope.projectId, "skills");
  const skills = useMemo(() => {
    const catalog = query.data as ProjectAssetList | undefined;
    if (!catalog) return [];
    return [
      ...catalog.project_items.map((item) => ({
        name: item.slug,
        description: item.display_name,
        enabled: item.status === "active",
      })),
      ...catalog.system_items.map((item) => ({
        name: item.slug,
        description: item.display_name,
        enabled: item.status === "active" && item.binding?.enabled === true,
      })),
    ] satisfies ProjectSlashSkill[];
  }, [query.data]);
  return { skills, isLoading: query.isLoading, error: query.error };
}
