import { useCallback, useMemo } from "react";

import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { useProjectAssets, useProjectAssetVersions } from "./hooks";
import type {
  AgentVersion,
  ProjectAssetItem,
  ProjectAssetList,
  VersionHistoryResponse,
} from "./types";

export type ProjectSlashSkill = {
  name: string;
  description: string;
  enabled: boolean;
};

type SkillRef = AgentVersion["skill_refs"][number];

export type ProjectSkillRuntime =
  | { kind: "main" }
  | { kind: "explicit"; skillRefs: readonly SkillRef[] };

type ThreadAgentMetadata = {
  [key: string]: unknown;
  agent_asset_id?: unknown;
  agent_scope?: unknown;
};

const MAIN_PROJECT_AGENT_SLUG = "project-assistant";

function isExecutable(item: ProjectAssetItem): boolean {
  return (
    item.status === "active" &&
    item.current_version_id !== null &&
    item.capabilities.includes("shared_assets.execute")
  );
}

function slashSkill(item: ProjectAssetItem): ProjectSlashSkill {
  return {
    name: item.slug,
    description: item.display_name,
    enabled:
      isExecutable(item) &&
      (item.scope === "project" || item.binding?.enabled === true),
  };
}

export function projectSlashSkills(
  catalog: ProjectAssetList,
): ProjectSlashSkill[] {
  return [...catalog.project_items, ...catalog.system_items]
    .filter((item) => item.current_version_id !== null)
    .map(slashSkill);
}

export function projectRuntimeSlashSkills(
  catalog: ProjectAssetList,
  runtime: ProjectSkillRuntime,
): ProjectSlashSkill[] {
  const available = [...catalog.project_items, ...catalog.system_items];
  if (runtime.kind === "main") {
    return available.map(slashSkill).filter((skill) => skill.enabled);
  }
  const required = new Set(
    runtime.skillRefs.map((ref) => `${ref.scope}:${ref.asset_id}`),
  );
  return available
    .filter((item) => required.has(`${item.scope}:${item.id}`))
    .map(slashSkill)
    .filter((skill) => skill.enabled);
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

export function isThreadProjectAgentArchived(
  catalog: ProjectAssetList | undefined,
  metadata: ThreadAgentMetadata | null | undefined,
  catalogSettled: boolean,
): boolean {
  const id = metadata?.agent_asset_id;
  if (
    !catalogSettled ||
    !catalog ||
    metadata?.agent_scope !== "project" ||
    typeof id !== "string"
  ) {
    return false;
  }
  return !catalog.project_items.some((item) => item.id === id);
}

function selectedAgentVersionId(agent: ProjectAssetItem | null): string {
  if (agent?.status !== "active") return "";
  if (agent.scope === "system" && agent.binding?.enabled !== true) return "";
  return agent.current_version_id ?? "";
}

function isSuspendedProjectAgent(agent: ProjectAssetItem | null): boolean {
  return agent?.scope === "project" && agent?.status === "suspended";
}

function currentAgentVersion(
  history: VersionHistoryResponse | undefined,
  agent: ProjectAssetItem | null,
): AgentVersion | null {
  if (!agent) return null;
  const versionId = selectedAgentVersionId(agent);
  const version = history?.data.find(
    (candidate) =>
      "agent_id" in candidate &&
      candidate.agent_id === agent.id &&
      candidate.id === versionId &&
      candidate.relation === "current",
  );
  return version && "agent_id" in version ? version : null;
}

export function resolveThreadAgentModelRef(
  catalog: ProjectAssetList | undefined,
  metadata: ThreadAgentMetadata | null | undefined,
  history: VersionHistoryResponse | undefined,
): string | null {
  const agent = threadAgent(catalog, metadata);
  if (!agent || isSuspendedProjectAgent(agent)) return null;
  if (agent.scope === "system" && agent.slug === MAIN_PROJECT_AGENT_SLUG) {
    return "default";
  }
  const version = currentAgentVersion(history, agent);
  return version?.model_ref.trim() ? version.model_ref : null;
}

export function useThreadAgentModelRef(
  metadata: ThreadAgentMetadata | null | undefined,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const { scope } = useProjectPrivateWorkScope();
  const agentQuery = useProjectAssets(
    scope.accountId,
    scope.projectId,
    "agents",
    enabled,
  );
  const catalog = agentQuery.data as ProjectAssetList | undefined;
  const catalogSettled =
    catalog !== undefined &&
    !agentQuery.isLoading &&
    !agentQuery.isFetching &&
    agentQuery.error == null;
  const agent = useMemo(
    () => threadAgent(catalog, metadata),
    [catalog, metadata],
  );
  const isMain =
    agent?.scope === "system" && agent.slug === MAIN_PROJECT_AGENT_SLUG;
  const agentSuspended = isSuspendedProjectAgent(agent);
  const agentArchived = isThreadProjectAgentArchived(
    catalog,
    metadata,
    catalogSettled,
  );
  const versionId = isMain ? "" : selectedAgentVersionId(agent);
  const versionQuery = useProjectAssetVersions(
    scope.accountId,
    scope.projectId,
    "agents",
    agent?.id ?? "",
    enabled && Boolean(agent && !isMain && !agentSuspended && versionId),
    agent?.scope ?? "project",
  );
  const modelRef = resolveThreadAgentModelRef(
    catalog,
    metadata,
    versionQuery.data,
  );
  const isLoading =
    agentQuery.isLoading ||
    agentQuery.isFetching ||
    Boolean(
      agent &&
      !isMain &&
      versionId &&
      (versionQuery.isLoading || versionQuery.isFetching),
    );
  const refetch = useCallback(async () => {
    await agentQuery.refetch();
    if (agent && !isMain && !agentSuspended && versionId) {
      await versionQuery.refetch();
    }
  }, [agent, agentQuery, agentSuspended, isMain, versionId, versionQuery]);
  return {
    modelRef,
    agentArchived,
    agentSuspended,
    isLoading,
    error: agentQuery.error ?? versionQuery.error,
    refetch,
  };
}

export function useProjectSlashSkills() {
  const { scope } = useProjectPrivateWorkScope();
  const query = useProjectAssets(scope.accountId, scope.projectId, "skills");
  const skills = useMemo(() => {
    const catalog = query.data as ProjectAssetList | undefined;
    return catalog ? projectSlashSkills(catalog) : [];
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
    agent?.scope ?? "project",
  );
  const version = currentAgentVersion(versionQuery.data, agent);
  const error = skillQuery.error ?? agentQuery.error ?? versionQuery.error;
  const skills = useMemo(() => {
    const catalog = skillQuery.data as ProjectAssetList | undefined;
    if (error || !catalog || !agent) return [];
    if (main) return projectRuntimeSlashSkills(catalog, { kind: "main" });
    return version
      ? projectRuntimeSlashSkills(catalog, {
          kind: "explicit",
          skillRefs: version.skill_refs,
        })
      : [];
  }, [agent, error, main, skillQuery.data, version]);
  return {
    skills,
    isLoading:
      skillQuery.isLoading ||
      agentQuery.isLoading ||
      Boolean(agent && !main && versionId && versionQuery.isLoading),
    error,
  };
}
