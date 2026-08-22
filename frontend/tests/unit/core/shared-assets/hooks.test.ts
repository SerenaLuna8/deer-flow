import { describe, expect, rs, test } from "@rstest/core";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn(),
  useQuery: rs.fn((options: unknown) => options),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn(),
}));

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import { projectKeys } from "@/core/projects/query-keys";
import { SharedAssetApiError } from "@/core/shared-assets/api";
import {
  applyProjectAgentMutationToCatalog,
  invalidateConfiguredProjectMcpQueries,
  invalidateProjectAgentConflictQueries,
  invalidateProjectMcpSecretQueries,
  isProjectAgentCasConflict,
  useProjectAssetVersions,
  useActivateProjectAssetVersion,
} from "@/core/shared-assets/hooks";
import {
  projectAgentRuntimeAssessmentsRoot,
  projectAssetKey,
  projectAssetVersionsKey,
  projectDefaultAgentKey,
  projectMcpEditableConfigurationKey,
  projectMcpSecretsKey,
  projectMcpToolInventoryKey,
} from "@/core/shared-assets/query-keys";
import type {
  AssetMutationResponse,
  ProjectAssetList,
} from "@/core/shared-assets/types";

describe("shared asset hooks", () => {
  test("applies a suspended Agent response to the shared catalog immediately", () => {
    const assetId = "33333333-3333-4333-8333-333333333333";
    const current = {
      project_items: [{ id: assetId, scope: "project", status: "active" }],
      system_items: [],
    } as unknown as ProjectAssetList;
    const response = {
      item: {
        id: assetId,
        scope: "project",
        status: "suspended",
        revision: 4,
      },
    } as unknown as AssetMutationResponse;

    expect(
      applyProjectAgentMutationToCatalog(current, response.item)
        ?.project_items[0],
    ).toMatchObject({ status: "suspended", revision: 4 });
  });

  test("keeps an unselected agent version query disabled without building an empty key", () => {
    useProjectAssetVersions(
      "11111111-1111-4111-8111-111111111111",
      "22222222-2222-4222-8222-222222222222",
      "agents",
      "",
      false,
    );

    expect(rs.mocked(useQuery)).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        queryKey: expect.arrayContaining([
          "asset",
          "__unselected__",
          "versions",
        ]),
      }),
    );
  });

  test("refreshes every authoritative Agent cache after a CAS conflict", async () => {
    const invalidateQueries = rs.fn(async () => undefined);
    const queryClient = { invalidateQueries } as unknown as QueryClient;
    const accountId = "11111111-1111-4111-8111-111111111111";
    const projectId = "22222222-2222-4222-8222-222222222222";
    const assetId = "33333333-3333-4333-8333-333333333333";

    await invalidateProjectAgentConflictQueries(
      queryClient,
      accountId,
      projectId,
      assetId,
    );

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetKey(accountId, projectId, "agents"),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "agents",
        assetId,
      ),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectDefaultAgentKey(accountId, projectId),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAgentRuntimeAssessmentsRoot(accountId, projectId),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectKeys.workspace(accountId),
    });
  });

  test("refreshes configured MCP state without remounting the active project scope", async () => {
    const invalidateQueries = rs.fn(async () => undefined);
    const queryClient = { invalidateQueries } as unknown as QueryClient;
    const accountId = "11111111-1111-4111-8111-111111111111";
    const projectId = "22222222-2222-4222-8222-222222222222";
    const assetId = "33333333-3333-4333-8333-333333333333";

    await invalidateConfiguredProjectMcpQueries(
      queryClient,
      accountId,
      projectId,
      assetId,
    );

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetKey(accountId, projectId, "mcp-servers"),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAgentRuntimeAssessmentsRoot(accountId, projectId),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "mcp-servers",
        assetId,
      ),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectMcpEditableConfigurationKey(
        accountId,
        projectId,
        assetId,
      ),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectKeys.workspace(accountId),
      refetchType: "none",
    });
    expect(invalidateQueries).toHaveBeenCalledTimes(5);
  });

  test("refreshes exact MCP secret, discovery, and Agent readiness after a secret mutation", async () => {
    const invalidateQueries = rs.fn(async () => undefined);
    const queryClient = { invalidateQueries } as unknown as QueryClient;
    const accountId = "11111111-1111-4111-8111-111111111111";
    const projectId = "22222222-2222-4222-8222-222222222222";
    const assetId = "33333333-3333-4333-8333-333333333333";
    const versionId = "44444444-4444-4444-8444-444444444444";

    await invalidateProjectMcpSecretQueries(
      queryClient,
      accountId,
      projectId,
      assetId,
      versionId,
    );

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectMcpSecretsKey(accountId, projectId, assetId, versionId),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectMcpToolInventoryKey(
        accountId,
        projectId,
        assetId,
        versionId,
      ),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAgentRuntimeAssessmentsRoot(accountId, projectId),
    });
    expect(invalidateQueries).toHaveBeenCalledTimes(3);
  });

  test("recognizes only mapped Agent CAS conflicts", () => {
    expect(
      isProjectAgentCasConflict(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "changed"),
      ),
    ).toBe(true);
    expect(
      isProjectAgentCasConflict(
        new SharedAssetApiError(422, "ASSET_VALIDATION_FAILED", "invalid"),
      ),
    ).toBe(false);
  });

  test("refreshes the Agent catalog and exact history after an activation conflict", async () => {
    const accountId = "11111111-1111-4111-8111-111111111111";
    const projectId = "22222222-2222-4222-8222-222222222222";
    const assetId = "33333333-3333-4333-8333-333333333333";
    const invalidateQueries = rs.fn(async () => undefined);
    const queryClient = { invalidateQueries } as unknown as QueryClient;
    rs.mocked(useQueryClient).mockReturnValue(queryClient);
    rs.mocked(usePrivateWorkAccess).mockReturnValue({
      scope: { accountId, projectId },
      client: {} as PrivateWorkAccess["client"],
      apiBaseURL: "/api",
      queryKeyPrefix: [],
      reconnectOnMount: false,
    });
    rs.mocked(useMutation).mockClear();

    useActivateProjectAssetVersion(accountId, projectId, "agents");
    const mutationOptions = rs.mocked(useMutation).mock
      .calls[0]?.[0] as unknown as {
      onError: (
        error: unknown,
        variables: {
          assetId: string;
          versionId: string;
          input: { expected_asset_version: number };
        },
      ) => Promise<void>;
    };
    await mutationOptions.onError(
      new SharedAssetApiError(409, "ASSET_CONFLICT", "changed"),
      {
        assetId,
        versionId: "44444444-4444-4444-8444-444444444444",
        input: { expected_asset_version: 5 },
      },
    );

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetKey(accountId, projectId, "agents"),
      exact: true,
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "agents",
        assetId,
      ),
      exact: true,
    });
  });
});
