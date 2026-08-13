"use client";

import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";

import {
  assessProjectAgentRuntime,
  listProjectAssetVersions,
  projectAgentRuntimeAssessmentsKey,
  projectAssetVersionsKey,
  useProjectAssets,
  MAX_AGENT_RUNTIME_ASSESSMENTS,
  type AgentRuntimeAssessmentReasonCode,
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

const AGENT_RUNTIME_BLOCK_REASONS: Record<
  AgentRuntimeAssessmentReasonCode,
  string
> = {
  agent_unavailable: "Agent 当前发布版本或项目绑定不可用，请刷新后重试。",
  runtime_dependency_unavailable:
    "Agent 的 Skill、MCP 或凭据依赖当前不可用，请完成配置后重试。",
  model_unavailable: "Agent 配置的模型当前不可用，请联系管理员。",
};

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
  const agentIds = useMemo(
    () => [...new Set(agents.map((agent) => agent.id))].sort(),
    [agents],
  );
  const batches = useMemo(() => {
    const items: string[][] = [];
    for (
      let start = 0;
      start < agentIds.length;
      start += MAX_AGENT_RUNTIME_ASSESSMENTS
    ) {
      items.push(agentIds.slice(start, start + MAX_AGENT_RUNTIME_ASSESSMENTS));
    }
    return items;
  }, [agentIds]);
  const shouldLoad = enabled && batches.length > 0;
  const runtime = useQueries({
    queries: batches.map((batch) => ({
      queryKey: projectAgentRuntimeAssessmentsKey(accountId, projectId, batch),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        assessProjectAgentRuntime(projectId, batch, signal),
      enabled: shouldLoad,
      staleTime: 0,
    })),
  });
  const error = runtime.find((query) => query.error)?.error ?? null;
  const isLoading = shouldLoad && runtime.some((query) => query.isLoading);
  const byAgentId = useMemo(
    () =>
      new Map(
        runtime.flatMap((query) =>
          (query.data?.items ?? []).map(
            (assessment) => [assessment.agent_asset_id, assessment] as const,
          ),
        ),
      ),
    [runtime],
  );
  const assessments = agents.map((agent) => {
    if (!shouldLoad || isLoading) {
      return {
        status: "loading",
        reason: "正在验证 Agent 的运行依赖，请稍候。",
      } satisfies AgentMcpDependencyAssessment;
    }
    if (error) {
      return {
        status: "blocked",
        reason: "无法验证 Agent 的运行依赖，请稍后重试。",
      } satisfies AgentMcpDependencyAssessment;
    }
    const assessment = byAgentId.get(agent.id);
    if (!assessment) {
      return {
        status: "blocked",
        reason: "无法确认 Agent 的运行依赖，请刷新后重试。",
      } satisfies AgentMcpDependencyAssessment;
    }
    return assessment.status === "ready"
      ? ({
          status: "ready",
          reason: null,
        } satisfies AgentMcpDependencyAssessment)
      : ({
          status: "blocked",
          reason: AGENT_RUNTIME_BLOCK_REASONS[assessment.reason_code],
        } satisfies AgentMcpDependencyAssessment);
  });

  return { assessments, isLoading, error };
}
