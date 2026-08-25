import { describe, expect, rs, test } from "@rstest/core";
import { useQuery, type QueryClient } from "@tanstack/react-query";

rs.mock("@tanstack/react-query", () => ({
  useQuery: rs.fn((options: unknown) => options),
}));

import { projectKeys } from "@/core/projects/query-keys";
import { SharedAssetApiError } from "@/core/shared-assets/api";
import {
  applyProjectAgentMutationToCatalog,
  applyProjectSkillDeletionToCache,
  invalidateConfiguredProjectMcpQueries,
  invalidateProjectAgentConflictQueries,
  invalidateProjectMcpSecretQueries,
  isProjectAgentCasConflict,
  useProjectAgentDefinition,
} from "@/core/shared-assets/hooks";
import {
  projectAgentRuntimeAssessmentsRoot,
  projectAgentDefinitionKey,
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
import { skillBuilderRootKey } from "@/core/skill-builder/query-keys";

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

  test("removes a deleted Skill and refreshes every auto-unbound Agent and Builder cache", async () => {
    const accountId = "11111111-1111-4111-8111-111111111111";
    const projectId = "22222222-2222-4222-8222-222222222222";
    const skillId = "33333333-3333-4333-8333-333333333333";
    const agentId = "44444444-4444-4444-8444-444444444444";
    const skillCatalog = {
      system_items: [],
      project_items: [{ id: skillId }, { id: "another-skill" }],
      request_id: "skills",
    } as unknown as ProjectAssetList;
    let nextCatalog: ProjectAssetList | undefined;
    const cancelQueries = rs.fn(async () => undefined);
    const removeQueries = rs.fn(() => undefined);
    const invalidateQueries = rs.fn(async () => undefined);
    const setQueryData = rs.fn(
      (
        _key: readonly unknown[],
        update: (
          current: ProjectAssetList | undefined,
        ) => ProjectAssetList | undefined,
      ) => {
        nextCatalog = update(skillCatalog);
      },
    );
    const queryClient = {
      cancelQueries,
      removeQueries,
      invalidateQueries,
      setQueryData,
    } as unknown as QueryClient;

    await applyProjectSkillDeletionToCache(
      queryClient,
      accountId,
      projectId,
      skillId,
    );

    expect(nextCatalog?.project_items.map((item) => item.id)).toEqual([
      "another-skill",
    ]);
    expect(cancelQueries).toHaveBeenCalledWith({
      queryKey: projectAssetKey(accountId, projectId, "skills"),
    });
    expect(removeQueries).toHaveBeenCalledWith({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "skills",
        skillId,
      ),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectAssetKey(accountId, projectId, "agents"),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: skillBuilderRootKey(accountId, projectId),
    });

    // The Agent root prefix owns every exact Definition and runtime assessment.
    expect(
      projectAgentDefinitionKey(accountId, projectId, agentId).slice(0, 6),
    ).toEqual(projectAssetKey(accountId, projectId, "agents"));
    expect(
      projectAgentRuntimeAssessmentsRoot(accountId, projectId).slice(0, 6),
    ).toEqual(projectAssetKey(accountId, projectId, "agents"));
  });

  test("keeps an unselected Agent Definition query disabled on a valid inert key", () => {
    useProjectAgentDefinition(
      "11111111-1111-4111-8111-111111111111",
      "22222222-2222-4222-8222-222222222222",
      "",
      false,
    );

    expect(rs.mocked(useQuery)).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        queryKey: expect.arrayContaining([
          "asset",
          "00000000-0000-4000-8000-000000000000",
          "definition",
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
      queryKey: projectAgentDefinitionKey(accountId, projectId, assetId),
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
});
