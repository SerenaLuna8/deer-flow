import { useCallback, useMemo } from "react";

import { useProjectPrivateWorkScope } from "@/core/private-work/provider";

import { useProjectAgentDefinition, useProjectAssets } from "./hooks";
import type {
  AgentDefinition,
  AgentDefinitionResponse,
  ProjectAssetItem,
  ProjectAssetList,
} from "./types";

export type ProjectSlashSkill = {
  name: string;
  description: string;
  enabled: boolean;
};

type SkillRef = AgentDefinition["skill_refs"][number];

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

function selectedAgentDefinitionId(agent: ProjectAssetItem | null): string {
  if (agent?.status !== "active") return "";
  if (agent.scope === "system" && agent.binding?.enabled !== true) return "";
  return agent.definition_id ?? "";
}

function isSuspendedProjectAgent(agent: ProjectAssetItem | null): boolean {
  return agent?.scope === "project" && agent?.status === "suspended";
}

function currentAgentDefinition(
  aggregate: AgentDefinitionResponse | undefined,
  agent: ProjectAssetItem | null,
): AgentDefinition | null {
  if (!agent) return null;
  const definitionId = selectedAgentDefinitionId(agent);
  return aggregate?.item.id === agent.id &&
    aggregate.item.definition_id === definitionId &&
    aggregate.definition.agent_id === agent.id &&
    aggregate.definition.definition_id === definitionId
    ? aggregate.definition
    : null;
}

export function resolveThreadAgentModelRef(
  catalog: ProjectAssetList | undefined,
  metadata: ThreadAgentMetadata | null | undefined,
  aggregate: AgentDefinitionResponse | undefined,
): string | null {
  const agent = threadAgent(catalog, metadata);
  if (!agent || isSuspendedProjectAgent(agent)) return null;
  if (agent.scope === "system" && agent.slug === MAIN_PROJECT_AGENT_SLUG) {
    return "default";
  }
  const definition = currentAgentDefinition(aggregate, agent);
  return definition?.model_ref.trim() ? definition.model_ref : null;
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
  const catalog = agentQuery.data;
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
  const definitionId = isMain ? "" : selectedAgentDefinitionId(agent);
  const definitionQuery = useProjectAgentDefinition(
    scope.accountId,
    scope.projectId,
    agent?.id ?? "",
    enabled && Boolean(agent && !isMain && !agentSuspended && definitionId),
  );
  const modelRef = resolveThreadAgentModelRef(
    catalog,
    metadata,
    definitionQuery.data,
  );
  const isLoading =
    agentQuery.isLoading ||
    agentQuery.isFetching ||
    Boolean(
      agent &&
      !isMain &&
      definitionId &&
      (definitionQuery.isLoading || definitionQuery.isFetching),
    );
  const refetch = useCallback(async () => {
    await agentQuery.refetch();
    if (agent && !isMain && !agentSuspended && definitionId) {
      await definitionQuery.refetch();
    }
  }, [
    agent,
    agentQuery,
    agentSuspended,
    definitionId,
    definitionQuery,
    isMain,
  ]);
  return {
    modelRef,
    agentArchived,
    agentSuspended,
    isLoading,
    error: agentQuery.error ?? definitionQuery.error,
    refetch,
  };
}

export function useProjectSlashSkills() {
  const { scope } = useProjectPrivateWorkScope();
  const query = useProjectAssets(scope.accountId, scope.projectId, "skills");
  const skills = useMemo(() => {
    const catalog = query.data;
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
  const agentCatalog = agentQuery.data;
  const agent = useMemo(
    () => threadAgent(agentCatalog, metadata),
    [agentCatalog, metadata],
  );
  const main =
    agent?.scope === "system" && agent.slug === MAIN_PROJECT_AGENT_SLUG;
  const definitionId = main ? "" : selectedAgentDefinitionId(agent);
  const definitionQuery = useProjectAgentDefinition(
    scope.accountId,
    scope.projectId,
    agent?.id ?? "",
    Boolean(agent && !main && definitionId),
  );
  const definition = currentAgentDefinition(definitionQuery.data, agent);
  const error = skillQuery.error ?? agentQuery.error ?? definitionQuery.error;
  const skills = useMemo(() => {
    const catalog = skillQuery.data;
    if (error || !catalog || !agent) return [];
    if (main) return projectRuntimeSlashSkills(catalog, { kind: "main" });
    return definition
      ? projectRuntimeSlashSkills(catalog, {
          kind: "explicit",
          skillRefs: definition.skill_refs,
        })
      : [];
  }, [agent, definition, error, main, skillQuery.data]);
  return {
    skills,
    isLoading:
      skillQuery.isLoading ||
      agentQuery.isLoading ||
      Boolean(agent && !main && definitionId && definitionQuery.isLoading),
    error,
  };
}
