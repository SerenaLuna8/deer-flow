import type { QueryClient } from "@tanstack/react-query";

import {
  getProjectAgentDefinition,
  listProjectAssets,
  projectAgentDefinitionKey,
  projectAssetKey,
  type AgentDefinition,
  type AgentDefinitionResponse,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

function catalogAgent(
  catalog: ProjectAssetList,
  assetId: string,
): ProjectAssetItem | null {
  return (
    catalog.project_items.find((candidate) => candidate.id === assetId) ??
    catalog.system_items.find((candidate) => candidate.id === assetId) ??
    null
  );
}

export type ProjectAgentAuthoringState = {
  item: ProjectAssetItem;
  definition: AgentDefinition;
};

export type ProjectAgentAuthoringReload = ProjectAgentAuthoringState & {
  agentCatalog: ProjectAssetList;
  aggregate: AgentDefinitionResponse;
  skillCatalog: ProjectAssetList | null;
  mcpCatalog: ProjectAssetList | null;
};

type ProjectAgentAuthoringCacheEpoch = {
  dataUpdateCount: number;
  data: unknown;
};

export type ProjectAgentAuthoringCacheEpochs = {
  agentCatalog: ProjectAgentAuthoringCacheEpoch;
  definition: ProjectAgentAuthoringCacheEpoch;
  skillCatalog: ProjectAgentAuthoringCacheEpoch;
  mcpCatalog: ProjectAgentAuthoringCacheEpoch;
};

function queryCacheEpoch(
  queryClient: QueryClient,
  queryKey: readonly unknown[],
): ProjectAgentAuthoringCacheEpoch {
  const state = queryClient.getQueryState(queryKey);
  return {
    dataUpdateCount: state?.dataUpdateCount ?? 0,
    data: state?.data,
  };
}

export function projectAgentAuthoringCacheEpochs({
  queryClient,
  accountId,
  projectId,
  assetId,
}: {
  queryClient: QueryClient;
  accountId: string;
  projectId: string;
  assetId: string;
}): ProjectAgentAuthoringCacheEpochs {
  return {
    agentCatalog: queryCacheEpoch(
      queryClient,
      projectAssetKey(accountId, projectId, "agents"),
    ),
    definition: queryCacheEpoch(
      queryClient,
      projectAgentDefinitionKey(accountId, projectId, assetId),
    ),
    skillCatalog: queryCacheEpoch(
      queryClient,
      projectAssetKey(accountId, projectId, "skills"),
    ),
    mcpCatalog: queryCacheEpoch(
      queryClient,
      projectAssetKey(accountId, projectId, "mcp-servers"),
    ),
  };
}

export function resolveProjectAgentAuthoringState({
  beforeCatalog,
  aggregate,
  afterCatalog,
  assetId,
  attemptedRevision,
  minimumRevision,
}: {
  beforeCatalog: ProjectAssetList;
  aggregate: AgentDefinitionResponse;
  afterCatalog: ProjectAssetList;
  assetId: string;
  attemptedRevision?: number;
  minimumRevision?: number;
}): ProjectAgentAuthoringState {
  if ((attemptedRevision === undefined) === (minimumRevision === undefined)) {
    throw new Error("Agent authoring revision guard is invalid");
  }
  const beforeItem = catalogAgent(beforeCatalog, assetId);
  const item = catalogAgent(afterCatalog, assetId);
  if (beforeItem === null || item === null) {
    throw new Error("Agent authoring state is unavailable");
  }
  if (
    beforeItem.revision !== item.revision ||
    beforeItem.definition_id !== item.definition_id ||
    aggregate.item.revision !== item.revision ||
    aggregate.item.definition_id !== item.definition_id ||
    aggregate.definition.agent_id !== assetId ||
    aggregate.definition.definition_id !== item.definition_id ||
    (attemptedRevision !== undefined && item.revision <= attemptedRevision) ||
    (minimumRevision !== undefined && item.revision < minimumRevision)
  ) {
    throw new Error(
      "Agent changed while its latest authoring state was loading",
    );
  }
  return { item, definition: aggregate.definition };
}

export async function reloadProjectAgentAuthoringState({
  projectId,
  assetId,
  attemptedRevision,
  minimumRevision,
  includeDependencyCatalogs = false,
  signal,
}: {
  projectId: string;
  assetId: string;
  attemptedRevision?: number;
  minimumRevision?: number;
  includeDependencyCatalogs?: boolean;
  signal?: AbortSignal;
}): Promise<ProjectAgentAuthoringReload> {
  const batchController = new AbortController();
  const batchSignal = signal
    ? AbortSignal.any([signal, batchController.signal])
    : batchController.signal;
  try {
    const beforeCatalog = await listProjectAssets(
      projectId,
      "agents",
      batchSignal,
    );
    const [aggregate, skills, mcps] = await Promise.all([
      getProjectAgentDefinition(projectId, assetId, batchSignal),
      includeDependencyCatalogs
        ? listProjectAssets(projectId, "skills", batchSignal)
        : Promise.resolve(null),
      includeDependencyCatalogs
        ? listProjectAssets(projectId, "mcp-servers", batchSignal)
        : Promise.resolve(null),
    ]);
    const afterCatalog = await listProjectAssets(
      projectId,
      "agents",
      batchSignal,
    );
    const state = resolveProjectAgentAuthoringState({
      beforeCatalog,
      aggregate,
      afterCatalog,
      assetId,
      attemptedRevision,
      minimumRevision,
    });

    return {
      ...state,
      agentCatalog: afterCatalog,
      aggregate,
      skillCatalog: skills,
      mcpCatalog: mcps,
    };
  } catch (error) {
    batchController.abort();
    throw error;
  }
}

export async function cacheProjectAgentAuthoringReload({
  queryClient,
  accountId,
  projectId,
  assetId,
  reload,
  startedAt,
  isCurrent,
}: {
  queryClient: QueryClient;
  accountId: string;
  projectId: string;
  assetId: string;
  reload: ProjectAgentAuthoringReload;
  startedAt: ProjectAgentAuthoringCacheEpochs;
  isCurrent: () => boolean;
}): Promise<void> {
  const agentKey = projectAssetKey(accountId, projectId, "agents");
  const definitionKey = projectAgentDefinitionKey(
    accountId,
    projectId,
    assetId,
  );
  const skillKey = projectAssetKey(accountId, projectId, "skills");
  const mcpKey = projectAssetKey(accountId, projectId, "mcp-servers");
  const keysToReplace = [agentKey, definitionKey];
  if (reload.skillCatalog !== null) keysToReplace.push(skillKey);
  if (reload.mcpCatalog !== null) keysToReplace.push(mcpKey);
  await Promise.all(
    keysToReplace.map((queryKey) =>
      queryClient.cancelQueries({ queryKey, exact: true }),
    ),
  );
  if (!isCurrent()) {
    const error = new Error("Agent authoring reload was cancelled");
    error.name = "AbortError";
    throw error;
  }
  const currentEpochs = projectAgentAuthoringCacheEpochs({
    queryClient,
    accountId,
    projectId,
    assetId,
  });
  const unchanged = (
    current: ProjectAgentAuthoringCacheEpoch,
    initial: ProjectAgentAuthoringCacheEpoch,
  ) =>
    current.dataUpdateCount === initial.dataUpdateCount &&
    current.data === initial.data;
  if (
    !unchanged(currentEpochs.agentCatalog, startedAt.agentCatalog) ||
    !unchanged(currentEpochs.definition, startedAt.definition) ||
    (reload.skillCatalog !== null &&
      !unchanged(currentEpochs.skillCatalog, startedAt.skillCatalog)) ||
    (reload.mcpCatalog !== null &&
      !unchanged(currentEpochs.mcpCatalog, startedAt.mcpCatalog))
  ) {
    throw new Error("Agent authoring reload is older than the cached state");
  }
  if (!isCurrent()) {
    const error = new Error("Agent authoring reload was cancelled");
    error.name = "AbortError";
    throw error;
  }

  queryClient.setQueryData(agentKey, reload.agentCatalog);
  queryClient.setQueryData(definitionKey, reload.aggregate);
  if (reload.skillCatalog)
    queryClient.setQueryData(skillKey, reload.skillCatalog);
  if (reload.mcpCatalog) queryClient.setQueryData(mcpKey, reload.mcpCatalog);
}
