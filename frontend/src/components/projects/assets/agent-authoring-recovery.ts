import type { QueryClient } from "@tanstack/react-query";

import {
  listProjectAssets,
  listProjectAssetVersions,
  projectAssetKey,
  projectAssetVersionsKey,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
} from "@/core/shared-assets";

export type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;

export function agentAuthoringBaseVersion<
  T extends Pick<
    AgentAssetVersion,
    "id" | "agent_id" | "version_number" | "relation" | "supersedes_version_id"
  >,
>(versions: readonly T[], currentVersionId: string | null): T | null {
  const latest = versions.reduce<T | null>(
    (current, candidate) =>
      !current || candidate.version_number > current.version_number
        ? candidate
        : current,
    null,
  );
  if (
    latest &&
    (latest.id === currentVersionId || latest.relation === "candidate")
  ) {
    return latest;
  }
  return (
    versions.find((candidate) => candidate.id === currentVersionId) ?? null
  );
}

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
  version: AgentAssetVersion;
};

export type ProjectAgentAuthoringReload = ProjectAgentAuthoringState & {
  agentCatalog: ProjectAssetList;
  history: VersionHistoryResponse;
  skillCatalog: ProjectAssetList | null;
  mcpCatalog: ProjectAssetList | null;
};

type ProjectAgentAuthoringCacheEpoch = {
  dataUpdateCount: number;
  data: unknown;
};

export type ProjectAgentAuthoringCacheEpochs = {
  agentCatalog: ProjectAgentAuthoringCacheEpoch;
  history: ProjectAgentAuthoringCacheEpoch;
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
    history: queryCacheEpoch(
      queryClient,
      projectAssetVersionsKey(accountId, projectId, "agents", assetId),
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
  history,
  afterCatalog,
  assetId,
  attemptedRevision,
  minimumRevision,
}: {
  beforeCatalog: ProjectAssetList;
  history: VersionHistoryResponse;
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
    beforeItem.current_version_id !== item.current_version_id ||
    (attemptedRevision !== undefined && item.revision <= attemptedRevision) ||
    (minimumRevision !== undefined && item.revision < minimumRevision)
  ) {
    throw new Error(
      "Agent changed while its latest authoring state was loading",
    );
  }

  const versions = history.data.filter(
    (candidate): candidate is AgentAssetVersion => "agent_id" in candidate,
  );
  const version = agentAuthoringBaseVersion(versions, item.current_version_id);
  if (version?.agent_id !== assetId) {
    throw new Error("Agent authoring base is unavailable");
  }
  return { item, version };
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
    const [history, skills, mcps] = await Promise.all([
      listProjectAssetVersions(projectId, "agents", assetId, batchSignal),
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
      history,
      afterCatalog,
      assetId,
      attemptedRevision,
      minimumRevision,
    });

    return {
      ...state,
      agentCatalog: afterCatalog,
      history,
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
  const historyKey = projectAssetVersionsKey(
    accountId,
    projectId,
    "agents",
    assetId,
  );
  const skillKey = projectAssetKey(accountId, projectId, "skills");
  const mcpKey = projectAssetKey(accountId, projectId, "mcp-servers");
  const keysToReplace = [agentKey, historyKey];
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
    !unchanged(currentEpochs.history, startedAt.history) ||
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
  queryClient.setQueryData(historyKey, reload.history);
  if (reload.skillCatalog) {
    queryClient.setQueryData(skillKey, reload.skillCatalog);
  }
  if (reload.mcpCatalog) {
    queryClient.setQueryData(mcpKey, reload.mcpCatalog);
  }
}
