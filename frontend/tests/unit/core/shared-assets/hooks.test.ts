import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { useQueryClient } from "@tanstack/react-query";

import {
  approveProjectMcpVersion,
  createAdminAssetVersion,
  createAdminCredential,
  createProjectAssetVersion,
  createProjectCredential,
  disableProjectSystemBinding,
  enableProjectSystemBinding,
  listAdminAssetVersions,
  listProjectAssetVersions,
  publishProjectAssetVersion,
  replaceProjectCredential,
  revokeProjectCredential,
  rollbackProjectSystemBinding,
  submitProjectMcpVersion,
  upgradeProjectSystemBinding,
} from "@/core/shared-assets/api";
import {
  useAdminAssetVersions,
  useApproveAdminMcpVersion,
  useApproveProjectMcpVersion,
  useCreateAdminAssetVersion,
  useCreateAdminCredential,
  useCreateProjectAssetVersion,
  useCreateProjectCredential,
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useProjectAssetVersions,
  usePublishAdminAssetVersion,
  usePublishProjectAssetVersion,
  useReplaceAdminCredential,
  useReplaceProjectCredential,
  useRevokeAdminCredential,
  useRevokeProjectCredential,
  useRollbackProjectSystemBinding,
  useSubmitAdminMcpVersion,
  useSubmitProjectMcpVersion,
  useUpgradeProjectSystemBinding,
} from "@/core/shared-assets/hooks";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn((options) => options),
  useQuery: rs.fn((options) => options),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/shared-assets/api", () => ({
  approveAdminMcpVersion: rs.fn(),
  approveProjectMcpVersion: rs.fn(),
  changeAdminAssetStatus: rs.fn(),
  changeProjectAssetStatus: rs.fn(),
  createAdminAsset: rs.fn(),
  createAdminAssetVersion: rs.fn(),
  createAdminCredential: rs.fn(),
  createProjectAsset: rs.fn(),
  createProjectAssetVersion: rs.fn(),
  createProjectCredential: rs.fn(),
  disableProjectSystemBinding: rs.fn(),
  enableProjectSystemBinding: rs.fn(),
  listAdminAssetVersions: rs.fn(),
  listAdminAssets: rs.fn(),
  listProjectAssetVersions: rs.fn(),
  listProjectAssets: rs.fn(),
  publishAdminAssetVersion: rs.fn(),
  publishProjectAssetVersion: rs.fn(),
  replaceAdminCredential: rs.fn(),
  replaceProjectCredential: rs.fn(),
  revokeAdminCredential: rs.fn(),
  revokeProjectCredential: rs.fn(),
  rollbackProjectSystemBinding: rs.fn(),
  submitAdminMcpVersion: rs.fn(),
  submitProjectMcpVersion: rs.fn(),
  upgradeProjectSystemBinding: rs.fn(),
}));

type QueryConfig = {
  queryKey: readonly unknown[];
  queryFn: (context: { signal: AbortSignal }) => Promise<unknown>;
};
type MutationConfig = {
  mutationFn: (input: never) => Promise<unknown>;
  onSuccess: () => Promise<unknown>;
};

const accountId = "account-1";
const projectId = "33333333-3333-4333-8333-333333333333";
const assetId = "11111111-1111-4111-8111-111111111111";
const versionId = "22222222-2222-4222-8222-222222222222";
const client = { invalidateQueries: rs.fn(() => Promise.resolve()) };

function mutation(value: unknown): MutationConfig {
  return value as MutationConfig;
}

beforeEach(() => {
  rs.clearAllMocks();
  rs.mocked(useQueryClient).mockReturnValue(client as never);
  for (const api of [
    approveProjectMcpVersion,
    createAdminAssetVersion,
    createAdminCredential,
    createProjectAssetVersion,
    createProjectCredential,
    disableProjectSystemBinding,
    enableProjectSystemBinding,
    listAdminAssetVersions,
    listProjectAssetVersions,
    publishProjectAssetVersion,
    replaceProjectCredential,
    revokeProjectCredential,
    rollbackProjectSystemBinding,
    submitProjectMcpVersion,
    upgradeProjectSystemBinding,
  ]) {
    rs.mocked(api).mockResolvedValue({} as never);
  }
});

describe("shared asset hooks", () => {
  test("loads project and admin history with isolated account scope keys", async () => {
    const signal = new AbortController().signal;
    const project = useProjectAssetVersions(
      accountId,
      projectId,
      "agents",
      assetId,
    ) as unknown as QueryConfig;
    const admin = useAdminAssetVersions(
      accountId,
      "skills",
      assetId,
    ) as unknown as QueryConfig;

    await project.queryFn({ signal });
    await admin.queryFn({ signal });

    expect(project.queryKey).toEqual([
      "account",
      accountId,
      "shared-assets",
      "project",
      projectId,
      "agents",
      "asset",
      assetId,
      "versions",
    ]);
    expect(listProjectAssetVersions).toHaveBeenCalledWith(
      projectId,
      "agents",
      assetId,
      signal,
    );
    expect(listAdminAssetVersions).toHaveBeenCalledWith(
      "skills",
      assetId,
      signal,
    );
  });

  test("wires typed version and write-only credential authoring hooks", async () => {
    const agentInput = {
      description: "Writer",
      soul: "Be precise",
      model_ref: "default",
      tool_groups: [],
      skill_version_ids: [],
      mcp_version_ids: [],
      expected_asset_version: 1,
    };
    const credentialInput = {
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    };
    const projectVersion = mutation(
      useCreateProjectAssetVersion(accountId, projectId, "agents"),
    );
    const adminVersion = mutation(
      useCreateAdminAssetVersion(accountId, "mcp-servers"),
    );
    const projectCredential = mutation(
      useCreateProjectCredential(accountId, projectId),
    );
    const adminCredential = mutation(useCreateAdminCredential(accountId));

    await projectVersion.mutationFn({ assetId, input: agentInput } as never);
    await adminVersion.mutationFn({
      assetId,
      input: { expected_asset_version: 1 },
    } as never);
    await projectCredential.mutationFn(credentialInput as never);
    await adminCredential.mutationFn(credentialInput as never);
    await projectVersion.onSuccess();

    expect(createProjectAssetVersion).toHaveBeenCalledWith(
      projectId,
      "agents",
      assetId,
      agentInput,
    );
    expect(createAdminAssetVersion).toHaveBeenCalledWith(
      "mcp-servers",
      assetId,
      { expected_asset_version: 1 },
    );
    expect(createProjectCredential).toHaveBeenCalledWith(
      projectId,
      credentialInput,
    );
    expect(createAdminCredential).toHaveBeenCalledWith(credentialInput);
    expect(client.invalidateQueries).toHaveBeenLastCalledWith({
      queryKey: [
        "account",
        accountId,
        "shared-assets",
        "project",
        projectId,
        "agents",
      ],
    });
  });

  test("wires project lifecycle and binding mutations to scope-local invalidation", async () => {
    const publish = mutation(
      usePublishProjectAssetVersion(accountId, projectId, "agents"),
    );
    const replace = mutation(useReplaceProjectCredential(accountId, projectId));
    const revoke = mutation(useRevokeProjectCredential(accountId, projectId));
    const submit = mutation(useSubmitProjectMcpVersion(accountId, projectId));
    const approve = mutation(useApproveProjectMcpVersion(accountId, projectId));
    const enable = mutation(
      useEnableProjectSystemBinding(accountId, projectId, "agent"),
    );
    const upgrade = mutation(
      useUpgradeProjectSystemBinding(accountId, projectId, "agent"),
    );
    const rollback = mutation(
      useRollbackProjectSystemBinding(accountId, projectId, "agent"),
    );
    const disable = mutation(
      useDisableProjectSystemBinding(accountId, projectId, "agent"),
    );

    await publish.mutationFn({
      assetId,
      versionId,
      input: { expected_asset_version: 1 },
    } as never);
    await replace.mutationFn({
      credentialId: assetId,
      input: {
        payload: { env: { TOKEN: "write-only" } },
        expected_credential_version: 1,
      },
    } as never);
    await revoke.mutationFn({
      credentialId: assetId,
      input: { expected_credential_version: 1 },
    } as never);
    await submit.mutationFn({
      assetId,
      versionId,
      input: { expected_asset_version: 1 },
    } as never);
    await approve.mutationFn({
      assetId,
      versionId,
      input: {
        credential_versions: { github: versionId },
        expected_asset_version: 1,
      },
    } as never);
    await enable.mutationFn({
      asset_id: assetId,
      version_id: versionId,
    } as never);
    for (const item of [upgrade, rollback]) {
      await item.mutationFn({
        assetId,
        input: { version_id: versionId, expected_binding_version: 1 },
      } as never);
    }
    await disable.mutationFn({
      assetId,
      input: { expected_binding_version: 1 },
    } as never);
    await disable.onSuccess();

    expect(publishProjectAssetVersion).toHaveBeenCalled();
    expect(replaceProjectCredential).toHaveBeenCalled();
    expect(revokeProjectCredential).toHaveBeenCalled();
    expect(submitProjectMcpVersion).toHaveBeenCalled();
    expect(approveProjectMcpVersion).toHaveBeenCalled();
    expect(enableProjectSystemBinding).toHaveBeenCalled();
    expect(upgradeProjectSystemBinding).toHaveBeenCalled();
    expect(rollbackProjectSystemBinding).toHaveBeenCalled();
    expect(disableProjectSystemBinding).toHaveBeenCalled();
    expect(client.invalidateQueries).toHaveBeenLastCalledWith({
      queryKey: [
        "account",
        accountId,
        "shared-assets",
        "project",
        projectId,
        "agents",
      ],
    });
  });

  test("exports the admin lifecycle hooks", () => {
    for (const hook of [
      usePublishAdminAssetVersion,
      useReplaceAdminCredential,
      useRevokeAdminCredential,
      useSubmitAdminMcpVersion,
      useApproveAdminMcpVersion,
    ]) {
      expect(typeof hook).toBe("function");
    }
  });
});
