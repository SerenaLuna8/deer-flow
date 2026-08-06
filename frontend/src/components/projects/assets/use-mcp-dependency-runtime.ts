"use client";

import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";

import {
  listProjectAssetVersions,
  projectAssetVersionsKey,
  useProjectAssets,
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
} from "@/core/shared-assets";
import {
  mcpDependencyRuntimeBlockReason,
  type ScopedMcpVersion,
} from "@/core/shared-assets/mcp-runtime";

export type AgentMcpDependencyAssessment = {
  status: "loading" | "ready" | "blocked";
  reason: string | null;
};

export const MAIN_PROJECT_AGENT_SLUG = "project-assistant";

export function isMainProjectAgent(
  agent: Pick<ProjectAssetItem, "scope" | "slug">,
): boolean {
  return agent.scope === "system" && agent.slug === MAIN_PROJECT_AGENT_SLUG;
}

function selectedAgentVersion(
  agent: ProjectAssetItem,
  history: VersionHistoryResponse,
) {
  const versionId =
    agent.scope === "system" && agent.binding?.enabled
      ? agent.binding.version_id
      : agent.current_published_version_id;
  return history.data.find(
    (version) =>
      "agent_id" in version &&
      version.id === versionId &&
      version.workflow_status === "published",
  );
}

export function agentMcpDependencyAssessment(
  agent: ProjectAssetItem,
  history: VersionHistoryResponse | undefined,
  mcpVersions: readonly ScopedMcpVersion[] | undefined,
): AgentMcpDependencyAssessment {
  if (isMainProjectAgent(agent)) {
    return { status: "ready", reason: null };
  }
  if (!history || !mcpVersions) {
    return { status: "loading", reason: null };
  }
  const currentVersion = selectedAgentVersion(agent, history);
  if (!currentVersion || !("agent_id" in currentVersion)) {
    return {
      status: "blocked",
      reason: "Agent 当前发布版本无法确认，请刷新后重试。",
    };
  }
  const reason = mcpDependencyRuntimeBlockReason(
    currentVersion.mcp_version_ids,
    mcpVersions,
  );
  return reason
    ? { status: "blocked", reason }
    : { status: "ready", reason: null };
}

export function useMcpDependencyRuntime({
  accountId,
  projectId,
  requiredVersionIds,
  enabled = true,
}: {
  accountId: string;
  projectId: string;
  requiredVersionIds: readonly string[];
  enabled?: boolean;
}): {
  versions: ScopedMcpVersion[];
  isLoading: boolean;
  error: unknown;
  blockReason: string | null;
} {
  const shouldLoad = enabled && requiredVersionIds.length > 0;
  const catalog = useProjectAssets(
    accountId,
    projectId,
    "mcp-servers",
    shouldLoad,
  );
  const items = useMemo(() => {
    if (!shouldLoad) return [];
    const data = catalog.data as ProjectAssetList | undefined;
    return data ? [...data.system_items, ...data.project_items] : [];
  }, [catalog.data, shouldLoad]);
  const histories = useQueries({
    queries: items.map((item) => ({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "mcp-servers",
        item.id,
      ),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listProjectAssetVersions(projectId, "mcp-servers", item.id, signal),
      enabled: shouldLoad,
    })),
  });
  const versions = useMemo(
    () =>
      histories.flatMap((history, index) => {
        const item = items[index];
        if (!item) return [];
        return (history.data?.data ?? []).flatMap((version) =>
          "mcp_server_id" in version
            ? [{ scope: item.scope, version } satisfies ScopedMcpVersion]
            : [],
        );
      }),
    [histories, items],
  );
  const isLoading =
    shouldLoad &&
    (catalog.isLoading ||
      (Boolean(catalog.data) &&
        histories.some((history) => history.isLoading)));
  const error =
    catalog.error ?? histories.find((history) => history.error)?.error ?? null;
  const blockReason =
    !shouldLoad || isLoading
      ? null
      : error
        ? "无法验证 Agent 的 MCP 依赖，请稍后重试。"
        : mcpDependencyRuntimeBlockReason(requiredVersionIds, versions);

  return { versions, isLoading, error, blockReason };
}

export function useAgentMcpDependencyRuntime({
  accountId,
  projectId,
  agents,
  enabled = true,
}: {
  accountId: string;
  projectId: string;
  agents: readonly ProjectAssetItem[];
  enabled?: boolean;
}): {
  assessments: AgentMcpDependencyAssessment[];
  isLoading: boolean;
  error: unknown;
} {
  const shouldLoad = enabled && agents.length > 0;
  const histories = useQueries({
    queries: agents.map((agent) => ({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "agents",
        agent.id,
      ),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listProjectAssetVersions(projectId, "agents", agent.id, signal),
      enabled: shouldLoad && !isMainProjectAgent(agent),
    })),
  });
  const requiredVersionIds = useMemo(() => {
    const ids = new Set<string>();
    for (const [index, agent] of agents.entries()) {
      if (isMainProjectAgent(agent)) continue;
      const history = histories[index]?.data;
      if (!history) continue;
      const version = selectedAgentVersion(agent, history);
      if (!version || !("agent_id" in version)) continue;
      for (const id of version.mcp_version_ids) ids.add(id);
    }
    return [...ids];
  }, [agents, histories]);
  const runtime = useMcpDependencyRuntime({
    accountId,
    projectId,
    requiredVersionIds,
    enabled: shouldLoad,
  });
  const historyError =
    histories.find((history) => history.error)?.error ?? null;
  const error = historyError ?? runtime.error;
  const isLoading =
    shouldLoad &&
    (histories.some((history) => history.isLoading) || runtime.isLoading);
  const assessments = agents.map((agent, index) => {
    if (isMainProjectAgent(agent)) {
      return {
        status: "ready",
        reason: null,
      } satisfies AgentMcpDependencyAssessment;
    }
    if (!shouldLoad || isLoading) {
      return {
        status: "loading",
        reason: "正在验证 Agent 的 MCP 依赖，请稍候。",
      } satisfies AgentMcpDependencyAssessment;
    }
    if (error) {
      return {
        status: "blocked",
        reason: "无法验证 Agent 的 MCP 依赖，请稍后重试。",
      } satisfies AgentMcpDependencyAssessment;
    }
    return agentMcpDependencyAssessment(
      agent,
      histories[index]?.data,
      runtime.versions,
    );
  });

  return { assessments, isLoading, error };
}
