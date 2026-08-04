import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";

import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { listProjectAssetVersions } from "./api";
import { useProjectAssets, useProjectAssetVersions } from "./hooks";
import { projectAssetVersionsKey } from "./query-keys";
import type {
  ProjectAssetItem,
  ProjectAssetList,
  VersionHistoryResponse,
} from "./types";

export type ProjectSlashSkill = {
  name: string;
  description: string;
  enabled: boolean;
};

export type ProjectSkillRuntime =
  | { kind: "main" }
  | {
      kind: "explicit";
      skillVersionIds: readonly string[];
      publishedProjectVersionIdsByAssetId?: ReadonlyMap<
        string,
        ReadonlySet<string>
      >;
    };

type ThreadAgentMetadata = {
  agent_asset_id?: unknown;
  agent_scope?: unknown;
};

const MAIN_PROJECT_AGENT_SLUG = "project-assistant";

function isExecutable(item: ProjectAssetItem): boolean {
  return (
    item.status === "active" &&
    item.capabilities.includes("shared_assets.execute")
  );
}

function effectiveSkillVersionId(item: ProjectAssetItem): string | null {
  if (item.scope === "project") return item.current_published_version_id;
  return item.binding?.enabled === true ? item.binding.version_id : null;
}

function slashSkill(item: ProjectAssetItem): ProjectSlashSkill {
  return {
    name: item.slug,
    description: item.display_name,
    enabled: isExecutable(item),
  };
}

export function projectSlashSkills(
  catalog: ProjectAssetList,
): ProjectSlashSkill[] {
  const published = (item: ProjectAssetList["project_items"][number]) =>
    item.current_published_version_id !== null;

  return [
    ...catalog.project_items.filter(published).map(slashSkill),
    ...catalog.system_items.filter(published).map((item) => ({
      ...slashSkill(item),
      enabled: isExecutable(item) && item.binding?.enabled === true,
    })),
  ];
}

export function projectRuntimeSlashSkills(
  catalog: ProjectAssetList,
  runtime: ProjectSkillRuntime,
): ProjectSlashSkill[] {
  if (runtime.kind === "main") {
    return projectSlashSkills(catalog).filter((skill) => skill.enabled);
  }
  const requiredVersionIds = new Set(runtime.skillVersionIds);
  return [...catalog.project_items, ...catalog.system_items]
    .filter((item) => {
      if (!isExecutable(item) || item.current_published_version_id === null) {
        return false;
      }
      if (item.scope === "system") {
        return requiredVersionIds.has(effectiveSkillVersionId(item) ?? "");
      }
      const publishedVersionIds =
        runtime.publishedProjectVersionIdsByAssetId?.get(item.id);
      if (publishedVersionIds) {
        return runtime.skillVersionIds.some((versionId) =>
          publishedVersionIds.has(versionId),
        );
      }
      return requiredVersionIds.has(item.current_published_version_id);
    })
    .map(slashSkill);
}

function threadAgent(
  catalog: ProjectAssetList | undefined,
  metadata: ThreadAgentMetadata | null | undefined,
): ProjectAssetItem | null {
  const id = metadata?.agent_asset_id;
  const scope = metadata?.agent_scope;
  if (
    !catalog ||
    typeof id !== "string" ||
    (scope !== "project" && scope !== "system")
  ) {
    return null;
  }
  const items =
    scope === "project" ? catalog.project_items : catalog.system_items;
  return items.find((item) => item.id === id) ?? null;
}

function selectedAgentVersionId(agent: ProjectAssetItem | null): string {
  if (!agent) return "";
  if (agent.scope === "project") {
    return agent.current_published_version_id ?? "";
  }
  return agent.binding?.enabled === true ? agent.binding.version_id : "";
}

function referencedSkillVersionIds(
  history: VersionHistoryResponse | undefined,
  versionId: string,
): readonly string[] | null {
  const version = history?.data.find(
    (candidate) =>
      "agent_id" in candidate &&
      candidate.id === versionId &&
      candidate.workflow_status === "published",
  );
  return version && "agent_id" in version ? version.skill_version_ids : null;
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

export function useProjectRuntimeSlashSkills(
  metadata: ThreadAgentMetadata | null | undefined,
) {
  const { scope } = useProjectPrivateWorkScope();
  const skillQuery = useProjectAssets(
    scope.accountId,
    scope.projectId,
    "skills",
  );
  const agentQuery = useProjectAssets(
    scope.accountId,
    scope.projectId,
    "agents",
  );
  const agentCatalog = agentQuery.data as ProjectAssetList | undefined;
  const agent = useMemo(
    () => threadAgent(agentCatalog, metadata),
    [agentCatalog, metadata],
  );
  const main =
    agent?.scope === "system" && agent.slug === MAIN_PROJECT_AGENT_SLUG;
  const versionId = main ? "" : selectedAgentVersionId(agent);
  const versionQuery = useProjectAssetVersions(
    scope.accountId,
    scope.projectId,
    "agents",
    agent?.id ?? "",
    Boolean(agent && !main && versionId),
  );
  const explicitSkillVersionIds = referencedSkillVersionIds(
    versionQuery.data,
    versionId,
  );
  const projectSkillItems = useMemo(() => {
    const catalog = skillQuery.data as ProjectAssetList | undefined;
    return catalog?.project_items ?? [];
  }, [skillQuery.data]);
  const shouldResolveProjectSkillVersions = Boolean(
    agent &&
    !main &&
    explicitSkillVersionIds &&
    explicitSkillVersionIds.length > 0,
  );
  const projectSkillHistories = useQueries({
    queries: projectSkillItems.map((item) => ({
      queryKey: projectAssetVersionsKey(
        scope.accountId,
        scope.projectId,
        "skills",
        item.id,
      ),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listProjectAssetVersions(scope.projectId, "skills", item.id, signal),
      enabled: shouldResolveProjectSkillVersions,
    })),
  });
  const publishedProjectVersionIdsByAssetId = useMemo(
    () =>
      new Map(
        projectSkillItems.map((item, index) => [
          item.id,
          new Set(
            (projectSkillHistories[index]?.data?.data ?? []).flatMap(
              (version) =>
                "skill_id" in version && version.workflow_status === "published"
                  ? [version.id]
                  : [],
            ),
          ),
        ]),
      ),
    [projectSkillHistories, projectSkillItems],
  );
  const error =
    skillQuery.error ??
    agentQuery.error ??
    versionQuery.error ??
    projectSkillHistories.find((history) => history.error)?.error;
  const skills = useMemo(() => {
    const catalog = skillQuery.data as ProjectAssetList | undefined;
    if (error || !catalog || !agent) return [];
    if (main) return projectRuntimeSlashSkills(catalog, { kind: "main" });
    return explicitSkillVersionIds
      ? projectRuntimeSlashSkills(catalog, {
          kind: "explicit",
          skillVersionIds: explicitSkillVersionIds,
          publishedProjectVersionIdsByAssetId,
        })
      : [];
  }, [
    agent,
    error,
    explicitSkillVersionIds,
    main,
    publishedProjectVersionIdsByAssetId,
    skillQuery.data,
  ]);
  const isLoading =
    skillQuery.isLoading ||
    agentQuery.isLoading ||
    Boolean(agent && !main && versionId && versionQuery.isLoading) ||
    (shouldResolveProjectSkillVersions &&
      projectSkillHistories.some((history) => history.isLoading));
  return {
    skills,
    isLoading,
    error,
  };
}
