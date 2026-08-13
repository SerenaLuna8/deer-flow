import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { useQueries } from "@tanstack/react-query";

import {
  MAIN_PROJECT_AGENT_SLUG,
  useAgentMcpDependencyRuntime,
} from "@/components/projects/assets/use-mcp-dependency-runtime";
import {
  assessProjectAgentRuntime,
  type AgentRuntimeAssessmentsResponse,
  type ProjectAssetItem,
} from "@/core/shared-assets";

rs.mock("react", () => ({
  useMemo: <T>(factory: () => T) => factory(),
}));
rs.mock("@tanstack/react-query", () => ({
  useQueries: rs.fn(() => []),
  useQuery: rs.fn(),
}));
rs.mock("@/core/shared-assets", () => ({
  MAX_AGENT_RUNTIME_ASSESSMENTS: 100,
  assessProjectAgentRuntime: rs.fn(),
  listProjectAssetVersions: rs.fn(),
  projectAgentRuntimeAssessmentsKey: (
    accountId: string,
    projectId: string,
    agentIds: readonly string[],
  ) => [accountId, projectId, "agent-runtime-assessments", ...agentIds],
  projectAssetVersionsKey: rs.fn(),
  useProjectAssets: rs.fn(),
}));
rs.mock("@/core/shared-assets/mcp-runtime", () => ({
  mcpDependencyRuntimeBlockReason: () => null,
}));

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";

type QueryOption = {
  enabled: boolean;
  queryFn: (context: { signal: AbortSignal }) => Promise<unknown>;
  queryKey: readonly unknown[];
};

const mockedUseQueries = rs.mocked(useQueries);
const mockedAssessRuntime = rs.mocked(assessProjectAgentRuntime);

function projectAgent(index: number): ProjectAssetItem {
  const suffix = String(index).padStart(12, "0");
  return {
    id: `30000000-0000-4000-8000-${suffix}`,
    scope: "project",
    slug: `agent-${index}`,
    current_published_version_id: `40000000-0000-4000-8000-${suffix}`,
  } as ProjectAssetItem;
}

function mainAgent(): ProjectAssetItem {
  return {
    id: "50000000-0000-4000-8000-000000000000",
    scope: "system",
    slug: MAIN_PROJECT_AGENT_SLUG,
    current_published_version_id: "50000000-0000-4000-8000-000000000001",
  } as ProjectAssetItem;
}

function responseForIds(
  agentIds: readonly string[],
  agents: readonly ProjectAssetItem[],
  blockedAgentId?: string,
): AgentRuntimeAssessmentsResponse {
  const byId = new Map(agents.map((agent) => [agent.id, agent]));
  return {
    items: agentIds.map((agentId) => {
      const agent = byId.get(agentId);
      if (!agent) throw new Error("unknown Agent test fixture");
      return agentId === blockedAgentId
        ? {
            agent_asset_id: agent.id,
            selected_version_id: null,
            status: "blocked" as const,
            reason_code: "agent_unavailable" as const,
          }
        : {
            agent_asset_id: agent.id,
            selected_version_id: agent.current_published_version_id!,
            status: "ready" as const,
            reason_code: null,
          };
    }),
    request_id: "measured-runtime-assessment",
  };
}

async function exerciseColdDependencyRequests({
  agents,
  enabled = true,
  blockedAgentId,
}: {
  agents: readonly ProjectAssetItem[];
  enabled?: boolean;
  blockedAgentId?: string;
}) {
  const options: QueryOption[] = [];
  const useQueriesImplementation = (input: { queries: QueryOption[] }) => {
    const queries = input.queries;
    options.push(...queries);
    return queries.map((query) => ({
      data: responseForIds(
        query.queryKey.slice(3) as string[],
        agents,
        blockedAgentId,
      ),
      isLoading: false,
      error: null,
    })) as ReturnType<typeof useQueries>;
  };
  mockedUseQueries.mockImplementation(useQueriesImplementation as never);
  mockedAssessRuntime.mockImplementation(async (_projectId, agentIds) =>
    responseForIds(agentIds, agents, blockedAgentId),
  );

  // The real composition hook exposes its exact enabled HTTP query plan.
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const result = useAgentMcpDependencyRuntime({
    accountId: ACCOUNT_ID,
    projectId: PROJECT_ID,
    agents,
    enabled,
  });

  const signal = new AbortController().signal;
  await Promise.all(
    options
      .filter((option) => option.enabled)
      .map((option) => option.queryFn({ signal })),
  );
  const dependencyRequests = mockedAssessRuntime.mock.calls.length;
  return {
    dependencyRequests,
    // ProjectAgentPage has already made one cold Agent-catalog request.
    agentListPageRequests: dependencyRequests + 1,
    assessments: result.assessments,
  };
}

async function measureColdDependencyRequests(
  input: Parameters<typeof exerciseColdDependencyRequests>[0],
) {
  const result = await exerciseColdDependencyRequests(input);
  return {
    dependencyRequests: result.dependencyRequests,
    agentListPageRequests: result.agentListPageRequests,
  };
}

beforeEach(() => {
  rs.clearAllMocks();
});

describe("cold Agent runtime assessment request fan-out", () => {
  test("uses one batch assessment request for 20 Agents", async () => {
    const agents = Array.from({ length: 20 }, (_, index) =>
      projectAgent(index + 1),
    );

    await expect(measureColdDependencyRequests({ agents })).resolves.toEqual({
      dependencyRequests: 1,
      agentListPageRequests: 2,
    });
    expect(mockedAssessRuntime).toHaveBeenCalledWith(
      PROJECT_ID,
      agents.map((agent) => agent.id).sort(),
      expect.any(AbortSignal),
    );
  });

  test("assesses Main on the server instead of fabricating local readiness", async () => {
    const agents = [mainAgent(), projectAgent(1)];

    await expect(measureColdDependencyRequests({ agents })).resolves.toEqual({
      dependencyRequests: 1,
      agentListPageRequests: 2,
    });
    expect(mockedAssessRuntime.mock.calls[0]?.[1]).toContain(mainAgent().id);
  });

  test("splits 101 Agents into two batches and maps results in list order", async () => {
    const agents = Array.from({ length: 101 }, (_, index) =>
      projectAgent(101 - index),
    );
    const blockedAgentId = agents[0]!.id;

    const result = await exerciseColdDependencyRequests({
      agents,
      blockedAgentId,
    });

    expect(result.dependencyRequests).toBe(2);
    expect(result.agentListPageRequests).toBe(3);
    expect(mockedAssessRuntime.mock.calls.map((call) => call[1])).toEqual([
      agents
        .map((agent) => agent.id)
        .sort()
        .slice(0, 100),
      agents
        .map((agent) => agent.id)
        .sort()
        .slice(100),
    ]);
    expect(result.assessments).toHaveLength(agents.length);
    expect(result.assessments[0]).toEqual({
      status: "blocked",
      reason: "Agent 当前发布版本或项目绑定不可用，请刷新后重试。",
    });
    expect(result.assessments.slice(1)).toEqual(
      Array.from({ length: 100 }, () => ({ status: "ready", reason: null })),
    );
  });

  test("issues no assessment request for an empty or disabled query", async () => {
    await expect(
      measureColdDependencyRequests({ agents: [] }),
    ).resolves.toEqual({
      dependencyRequests: 0,
      agentListPageRequests: 1,
    });
    await expect(
      measureColdDependencyRequests({
        agents: [projectAgent(1)],
        enabled: false,
      }),
    ).resolves.toEqual({
      dependencyRequests: 0,
      agentListPageRequests: 1,
    });
  });
});
