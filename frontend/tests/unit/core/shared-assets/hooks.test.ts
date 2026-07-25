import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { useQueryClient } from "@tanstack/react-query";

import {
  approveProjectMcpVersion,
  configureAdminMcpCredentialGrants,
  createProjectAssetVersion,
  disableProjectSystemBinding,
  enableProjectSystemBinding,
  forkProjectSkillVersion,
  getProjectSkillVersionFile,
  listAdminAssetVersions,
  listAdminProjectAssets,
  listProjectAssetVersions,
  listSystemAssetCatalog,
  publishProjectAssetVersion,
  revokeProjectCredential,
  rollbackProjectSystemBinding,
  submitProjectMcpVersion,
  upgradeProjectSystemBinding,
} from "@/core/shared-assets/api";
import {
  useAdminAssetVersions,
  useAdminProjectAssets,
  useApproveProjectMcpVersion,
  useConfigureAdminMcpCredentialGrants,
  useCreateProjectAssetVersion,
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useForkProjectSkillVersion,
  useProjectAssetVersions,
  useProjectSkillVersionFile,
  useSystemAssetCatalog,
  usePublishProjectAssetVersion,
  useRevokeAdminCredential,
  useRevokeProjectCredential,
  useRollbackProjectSystemBinding,
  useSubmitProjectMcpVersion,
  useUpgradeProjectSystemBinding,
} from "@/core/shared-assets/hooks";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn((options) => options),
  useQuery: rs.fn((options) => options),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/shared-assets/api", () => ({
  approveProjectMcpVersion: rs.fn(),
  configureAdminMcpCredentialGrants: rs.fn(),
  changeProjectAssetStatus: rs.fn(),
  createProjectAsset: rs.fn(),
  createProjectAssetVersion: rs.fn(),
  disableProjectSystemBinding: rs.fn(),
  enableProjectSystemBinding: rs.fn(),
  forkProjectSkillVersion: rs.fn(),
  getProjectSkillVersionFile: rs.fn(),
  listAdminAssetVersions: rs.fn(),
  listAdminProjectAssets: rs.fn(),
  listAdminAssets: rs.fn(),
  listProjectAssetVersions: rs.fn(),
  listProjectAssets: rs.fn(),
  listSystemAssetCatalog: rs.fn(),
  publishProjectAssetVersion: rs.fn(),
  revokeAdminCredential: rs.fn(),
  revokeProjectCredential: rs.fn(),
  rollbackProjectSystemBinding: rs.fn(),
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
    configureAdminMcpCredentialGrants,
    createProjectAssetVersion,
    disableProjectSystemBinding,
    enableProjectSystemBinding,
    forkProjectSkillVersion,
    getProjectSkillVersionFile,
    listAdminAssetVersions,
    listAdminProjectAssets,
    listProjectAssetVersions,
    listSystemAssetCatalog,
    publishProjectAssetVersion,
    revokeProjectCredential,
    rollbackProjectSystemBinding,
    submitProjectMcpVersion,
    upgradeProjectSystemBinding,
  ]) {
    rs.mocked(api).mockResolvedValue({} as never);
  }
});

describe("shared asset hooks", () => {
  test("never exposes secret-bearing Credential create or replace TanStack hooks", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/core/shared-assets/hooks.ts"),
      "utf8",
    );
    for (const hook of [
      "useCreateProjectCredential",
      "useCreateAdminCredential",
      "useCreateAdminProjectCredential",
      "useReplaceProjectCredential",
      "useReplaceAdminCredential",
      "useReplaceAdminProjectCredential",
    ]) {
      expect(source).not.toContain(`export function ${hook}`);
    }
  });

  test("loads Skill source with an abortable fully isolated ephemeral key", async () => {
    const signal = new AbortController().signal;
    const source = useProjectSkillVersionFile(
      accountId,
      projectId,
      assetId,
      versionId,
      "SKILL.md",
    ) as unknown as QueryConfig & { gcTime: number; enabled: boolean };

    await source.queryFn({ signal });

    expect(source.queryKey).toEqual([
      "account",
      accountId,
      "shared-assets",
      "project",
      projectId,
      "skills",
      "asset",
      assetId,
      "versions",
      "version",
      versionId,
      "file",
      "SKILL.md",
    ]);
    expect(source.gcTime).toBe(0);
    expect(source.enabled).toBe(true);
    expect(getProjectSkillVersionFile).toHaveBeenCalledWith(
      projectId,
      assetId,
      versionId,
      "SKILL.md",
      signal,
    );
  });

  test("forks a project Skill as a new version and invalidates only its project catalog", async () => {
    const fork = mutation(useForkProjectSkillVersion(accountId, projectId));
    const input = {
      expected_asset_version: 3,
      expected_source_payload_checksum: "d".repeat(64),
      changes: [
        {
          op: "replace" as const,
          path: "SKILL.md",
          content: "# Updated",
          media_type: "text/markdown",
        },
      ],
    };

    await fork.mutationFn({
      assetId,
      sourceVersionId: versionId,
      input,
    } as never);
    await fork.onSuccess();

    expect(forkProjectSkillVersion).toHaveBeenCalledWith(
      projectId,
      assetId,
      versionId,
      input,
    );
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [
        "account",
        accountId,
        "shared-assets",
        "project",
        projectId,
        "skills",
      ],
    });
  });

  test("loads the authenticated system catalog with an account-scoped key", async () => {
    const signal = new AbortController().signal;
    const catalog = useSystemAssetCatalog(
      accountId,
      "mcp-servers",
    ) as unknown as QueryConfig;

    await catalog.queryFn({ signal });

    expect(catalog.queryKey).toEqual([
      "account",
      accountId,
      "shared-assets",
      "catalog",
      "mcp-servers",
    ]);
    expect(listSystemAssetCatalog).toHaveBeenCalledWith("mcp-servers", signal);
  });

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

  test("loads admin project override data under its own account and project key", async () => {
    const signal = new AbortController().signal;
    const override = useAdminProjectAssets(
      accountId,
      projectId,
      "agents",
    ) as unknown as QueryConfig;

    await override.queryFn({ signal });

    expect(override.queryKey).toEqual([
      "account",
      accountId,
      "shared-assets",
      "admin",
      "project",
      projectId,
      "agents",
    ]);
    expect(listAdminProjectAssets).toHaveBeenCalledWith(
      projectId,
      "agents",
      signal,
    );
  });

  test("wires typed project version authoring hooks", async () => {
    const agentInput = {
      description: "Writer",
      soul: "Be precise",
      model_ref: "default",
      tool_groups: [],
      skill_version_ids: [],
      mcp_version_ids: [],
      expected_asset_version: 1,
    };
    const projectVersion = mutation(
      useCreateProjectAssetVersion(accountId, projectId, "agents"),
    );
    await projectVersion.mutationFn({ assetId, input: agentInput } as never);
    await projectVersion.onSuccess();

    expect(createProjectAssetVersion).toHaveBeenCalledWith(
      projectId,
      "agents",
      assetId,
      agentInput,
    );
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [
        "account",
        accountId,
        "shared-assets",
        "project",
        projectId,
        "agents",
      ],
    });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["account", accountId, "projects"],
    });
  });

  test("wires the dedicated packaged System MCP Credential grant hook", async () => {
    const configure = mutation(useConfigureAdminMcpCredentialGrants(accountId));
    const input = {
      credential_versions: { primary: versionId },
      expected_active_grant_versions: { primary: 1 },
    };

    await configure.mutationFn({ assetId, versionId, input } as never);
    await configure.onSuccess();

    expect(configureAdminMcpCredentialGrants).toHaveBeenCalledWith(
      assetId,
      versionId,
      input,
    );
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["account", accountId, "shared-assets", "admin", "mcp-servers"],
    });
  });

  test("keeps binding invalidation on the asset catalog without remounting project context", async () => {
    const publish = mutation(
      usePublishProjectAssetVersion(accountId, projectId, "agents"),
    );
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
    expect(revokeProjectCredential).toHaveBeenCalled();
    expect(submitProjectMcpVersion).toHaveBeenCalled();
    expect(approveProjectMcpVersion).toHaveBeenCalled();
    expect(enableProjectSystemBinding).toHaveBeenCalled();
    expect(upgradeProjectSystemBinding).toHaveBeenCalled();
    expect(rollbackProjectSystemBinding).toHaveBeenCalled();
    expect(disableProjectSystemBinding).toHaveBeenCalled();
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [
        "account",
        accountId,
        "shared-assets",
        "project",
        projectId,
        "agents",
      ],
    });
    expect(client.invalidateQueries).not.toHaveBeenCalledWith({
      queryKey: ["account", accountId, "projects"],
    });
  });

  test("keeps only the system Credential admin lifecycle hook", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/core/shared-assets/hooks.ts"),
      "utf8",
    );

    expect(typeof useRevokeAdminCredential).toBe("function");
    for (const hook of [
      "useCreateAdminAsset",
      "useCreateAdminAssetVersion",
      "useChangeAdminAssetStatus",
      "usePublishAdminAssetVersion",
      "useSubmitAdminMcpVersion",
      "useApproveAdminMcpVersion",
    ]) {
      expect(source).not.toContain(`export function ${hook}`);
    }
  });
});
