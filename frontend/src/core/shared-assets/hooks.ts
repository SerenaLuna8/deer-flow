import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import { projectKeys } from "@/core/projects/query-keys";

import {
  SharedAssetApiError,
  approveAdminProjectMcpVersion,
  approveProjectMcpVersion,
  changeAdminProjectAssetStatus,
  changeProjectAssetStatus,
  configureAdminMcpCredentialGrants,
  createAdminProjectAssetVersion,
  createConfiguredProjectMcp,
  createProjectAssetVersion,
  deleteProjectAgent,
  deleteProjectMcp,
  deleteProjectSkill,
  disableAdminProjectSystemBinding,
  disableProjectSystemBinding,
  enableAdminProjectSystemBinding,
  enableProjectSystemBinding,
  forkProjectSkillVersion,
  getProjectMcpEditableConfiguration,
  getProjectMcpToolInventory,
  getProjectDefaultAgent,
  getProjectSkillCredentialBindings,
  getProjectSkillPublishPlan,
  getProjectSkillVersionFile,
  importProjectSkillArchive,
  listAdminAssetVersions,
  listAdminAssets,
  listAdminProjectAssetVersions,
  listAdminProjectAssets,
  listProjectAssetVersions,
  listProjectAssets,
  listSystemAssetCatalog,
  publishProjectAssetVersion,
  publishAdminProjectAssetVersion,
  requestProjectMcpToolDiscovery,
  restoreProjectAgentVersion,
  revokeAdminCredential,
  revokeProjectCredential,
  rollbackProjectSystemBinding,
  rollbackAdminProjectSystemBinding,
  setProjectDefaultAgent,
  syncCurrentProjectSystemMcpBinding,
  submitAdminProjectMcpVersion,
  submitProjectMcpVersion,
  updateConfiguredProjectMcp,
  updateProjectAgentCapabilityBindings,
  updateProjectAgentInstructions,
  updateProjectSkillCredentialBindings,
  upgradeAdminProjectSystemBinding,
  upgradeProjectSystemBinding,
} from "./api";
import {
  adminAssetKey,
  adminAssetVersionsKey,
  adminProjectAssetKey,
  adminProjectAssetVersionsKey,
  projectAssetKey,
  projectAssetMutationKey,
  projectAssetVersionsKey,
  projectAgentRuntimeAssessmentsRoot,
  projectDefaultAgentKey,
  projectMcpEditableConfigurationKey,
  projectMcpToolInventoryKey,
  projectSkillCredentialBindingsKey,
  projectSkillCredentialBindingsMutationKey,
  projectSkillPublishPlanKey,
  projectSkillVersionFileKey,
  systemCatalogKey,
} from "./query-keys";
import type { SkillPublishPlanResponse } from "./skill-secret-declarations";
import type {
  AdminAssetList,
  AssetMutationResponse,
  AssetSummary,
  AgentCapabilityBindingsInput,
  AdminProjectAssetStatusAction,
  AdminCredentialList,
  AgentInstructionsInput,
  ApproveMcpInput,
  AssetKind,
  AssetListKind,
  CreateConfiguredMcpInput,
  ConfigureSystemMcpCredentialGrantsInput,
  DisableSystemBindingInput,
  EnableSystemBindingInput,
  ExpectedAssetVersionInput,
  MoveSystemBindingInput,
  McpVersionInput,
  PublishAssetVersionInput,
  McpToolInventoryResponse,
  ProjectAssetList,
  ProjectAssetStatusAction,
  ProjectCredentialList,
  ProjectDefaultAgent,
  ProjectDefaultAgentInput,
  ProjectMcpEditableConfigurationResponse,
  RevokeCredentialInput,
  SkillVersionInput,
  SkillFileForkInput,
  SkillCredentialBindingsInput,
  SkillCredentialBindingsResponse,
  SkillPublishAssetVersionInput,
  SkillVersionFileContentResponse,
  SyncCurrentSystemMcpBindingInput,
  UpdateConfiguredMcpInput,
  VersionHistoryResponse,
} from "./types";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
type PublishableVersionKind = MutableAssetKind;
type AuthorableVersionKind = Exclude<PublishableVersionKind, "agents">;
type VersionAuthoringInput = SkillVersionInput | McpVersionInput;

export const MCP_TOOL_INVENTORY_POLL_INTERVAL_MS = 2_000;

const BINDING_LIST_KIND: Record<AssetKind, MutableAssetKind> = {
  agent: "agents",
  skill: "skills",
  mcp: "mcp-servers",
};

export function invalidateProjectAssetQueries(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  return queryClient.invalidateQueries({
    queryKey: projectAssetKey(accountId, projectId, kind),
  });
}

export function invalidateProjectAgentRuntimeAssessments(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
) {
  return queryClient.invalidateQueries({
    queryKey: projectAgentRuntimeAssessmentsRoot(accountId, projectId),
  });
}

export function invalidateProjectSkillConflictQueries(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  assetId: string,
) {
  return Promise.all([
    queryClient.invalidateQueries({
      queryKey: projectAssetKey(accountId, projectId, "skills"),
      exact: true,
    }),
    queryClient.invalidateQueries({
      queryKey: projectAssetVersionsKey(
        accountId,
        projectId,
        "skills",
        assetId,
      ),
      exact: true,
    }),
  ]);
}

export function projectAgentMutationQueryKeys(
  accountId: string,
  projectId: string,
  assetId: string,
) {
  return [
    projectAssetKey(accountId, projectId, "agents"),
    projectAssetVersionsKey(accountId, projectId, "agents", assetId),
  ] as const;
}

export function isProjectAgentCasConflict(error: unknown): boolean {
  return (
    error instanceof SharedAssetApiError && error.code === "ASSET_CONFLICT"
  );
}

export function applyProjectAgentMutationToCatalog(
  current: ProjectAssetList | undefined,
  item: AssetSummary,
): ProjectAssetList | undefined {
  if (!current || item.scope !== "project") return current;
  let changed = false;
  const projectItems = current.project_items.map((existing) => {
    if (existing.id !== item.id) return existing;
    changed = true;
    return { ...existing, ...item };
  });
  return changed ? { ...current, project_items: projectItems } : current;
}

export function invalidateProjectAgentConflictQueries(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  assetId?: string | null,
) {
  return Promise.all([
    queryClient.invalidateQueries({
      queryKey: projectAssetKey(accountId, projectId, "agents"),
      exact: true,
    }),
    ...(assetId
      ? [
          queryClient.invalidateQueries({
            queryKey: projectAssetVersionsKey(
              accountId,
              projectId,
              "agents",
              assetId,
            ),
            exact: true,
          }),
        ]
      : []),
    queryClient.invalidateQueries({
      queryKey: projectDefaultAgentKey(accountId, projectId),
      exact: true,
    }),
    invalidateProjectAgentRuntimeAssessments(queryClient, accountId, projectId),
    queryClient.invalidateQueries({
      queryKey: projectKeys.workspace(accountId),
    }),
  ]);
}

function invalidateProjectAgentMutationQueries(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  assetId: string,
) {
  return Promise.all([
    ...projectAgentMutationQueryKeys(accountId, projectId, assetId).map(
      (queryKey) => queryClient.invalidateQueries({ queryKey, exact: true }),
    ),
    invalidateProjectAgentRuntimeAssessments(queryClient, accountId, projectId),
  ]);
}

export function invalidateAdminAssetQueries(
  queryClient: QueryClient,
  accountId: string,
  kind: AssetListKind,
) {
  return queryClient.invalidateQueries({
    queryKey: adminAssetKey(accountId, kind),
  });
}

export function invalidateAdminProjectAssetQueries(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  return queryClient.invalidateQueries({
    queryKey: adminProjectAssetKey(accountId, projectId, kind),
  });
}

function useProjectInvalidation(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  const queryClient = useQueryClient();
  return () =>
    Promise.all([
      invalidateProjectAssetQueries(queryClient, accountId, projectId, kind),
      invalidateProjectAgentRuntimeAssessments(
        queryClient,
        accountId,
        projectId,
      ),
      queryClient.invalidateQueries({
        queryKey: projectKeys.workspace(accountId),
      }),
    ]);
}

function useProjectAssetListInvalidation(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  const queryClient = useQueryClient();
  return () =>
    Promise.all([
      invalidateProjectAssetQueries(queryClient, accountId, projectId, kind),
      invalidateProjectAgentRuntimeAssessments(
        queryClient,
        accountId,
        projectId,
      ),
    ]);
}

function useProjectMutationRunner(accountId: string, projectId: string) {
  const access = usePrivateWorkAccess();
  if (
    access.scope.accountId !== accountId ||
    access.scope.projectId !== projectId
  ) {
    throw new Error("Shared asset mutation scope does not match the project");
  }

  function inactiveScopeError() {
    const error = new Error("Shared asset mutation scope is inactive");
    error.name = "AbortError";
    return error;
  }

  async function runMutation<T>(
    operation: (signal?: AbortSignal) => Promise<T>,
  ) {
    try {
      const result = await runPrivateWorkAbortable(access, operation);
      if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
      return result;
    } catch (error) {
      if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
      throw error;
    }
  }

  function whenActive<Arguments extends unknown[], T>(
    operation: (...args: Arguments) => T | Promise<T>,
  ) {
    return async (...args: Arguments): Promise<T> => {
      if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
      const result = await operation(...args);
      if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
      return result;
    };
  }

  return { runMutation, whenActive };
}

function useAdminInvalidation(accountId: string, kind: AssetListKind) {
  const queryClient = useQueryClient();
  return () => invalidateAdminAssetQueries(queryClient, accountId, kind);
}

function useAdminProjectInvalidation(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  const queryClient = useQueryClient();
  return () =>
    invalidateAdminProjectAssetQueries(queryClient, accountId, projectId, kind);
}

export function useProjectAssets(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  enabled = true,
) {
  return useQuery<ProjectAssetList | ProjectCredentialList>({
    queryKey: projectAssetKey(accountId, projectId, kind),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listProjectAssets(projectId, kind, signal)
        : listProjectAssets(projectId, kind, signal),
    enabled,
  });
}

export function useProjectDefaultAgent(
  accountId: string,
  projectId: string,
  enabled = true,
) {
  return useQuery<ProjectDefaultAgent>({
    queryKey: projectDefaultAgentKey(accountId, projectId),
    queryFn: ({ signal }) => getProjectDefaultAgent(projectId, signal),
    enabled,
  });
}

export function useSetProjectDefaultAgent(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const key = projectDefaultAgentKey(accountId, projectId);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: [...key, "mutation"],
    mutationFn: (input: ProjectDefaultAgentInput) =>
      runMutation((signal) => setProjectDefaultAgent(projectId, input, signal)),
    onSuccess: whenActive((data: ProjectDefaultAgent) => {
      queryClient.setQueryData(key, data);
    }),
    onError: whenActive(
      async (error: unknown, input: ProjectDefaultAgentInput) => {
        if (!isProjectAgentCasConflict(error)) return;
        await invalidateProjectAgentConflictQueries(
          queryClient,
          accountId,
          projectId,
          input.agent_asset_id,
        );
      },
    ),
  });
}

export function useAdminAssets(
  accountId: string,
  kind: AssetListKind,
  enabled = true,
) {
  return useQuery<AdminAssetList | AdminCredentialList>({
    queryKey: adminAssetKey(accountId, kind),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listAdminAssets(kind, signal)
        : listAdminAssets(kind, signal),
    enabled,
  });
}

export function useAdminProjectAssets(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  enabled = true,
) {
  return useQuery<ProjectAssetList | ProjectCredentialList>({
    queryKey: adminProjectAssetKey(accountId, projectId, kind),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listAdminProjectAssets(projectId, kind, signal)
        : listAdminProjectAssets(projectId, kind, signal),
    enabled,
  });
}

export function useSystemAssetCatalog(
  accountId: string,
  kind: Exclude<AssetListKind, "credentials">,
) {
  return useQuery<AdminAssetList>({
    queryKey: systemCatalogKey(accountId, kind),
    queryFn: ({ signal }) => listSystemAssetCatalog(kind, signal),
  });
}

export function useProjectAssetVersions(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  assetId: string,
  enabled = true,
) {
  const hasAssetId = assetId.trim() !== "";
  return useQuery<VersionHistoryResponse>({
    queryKey: projectAssetVersionsKey(
      accountId,
      projectId,
      kind,
      hasAssetId ? assetId : "__unselected__",
    ),
    queryFn: ({ signal }) =>
      listProjectAssetVersions(projectId, kind, assetId, signal),
    enabled: enabled && hasAssetId,
  });
}

export function useProjectMcpToolInventory(
  accountId: string,
  projectId: string,
  assetId: string,
  versionId: string,
  enabled = true,
) {
  return useQuery<McpToolInventoryResponse>({
    queryKey: projectMcpToolInventoryKey(
      accountId,
      projectId,
      assetId,
      versionId,
    ),
    queryFn: ({ signal }) =>
      getProjectMcpToolInventory(projectId, assetId, versionId, signal),
    enabled: enabled && versionId !== "",
    staleTime: 0,
    refetchInterval: (query) =>
      query.state.data?.data.status === "testing"
        ? MCP_TOOL_INVENTORY_POLL_INTERVAL_MS
        : false,
    refetchIntervalInBackground: false,
  });
}

export function useProjectMcpEditableConfiguration(
  accountId: string,
  projectId: string,
  assetId: string,
  enabled = true,
) {
  return useQuery<ProjectMcpEditableConfigurationResponse>({
    queryKey: projectMcpEditableConfigurationKey(accountId, projectId, assetId),
    queryFn: ({ signal }) =>
      getProjectMcpEditableConfiguration(projectId, assetId, signal),
    enabled,
    staleTime: 0,
  });
}

export function useRequestProjectMcpToolDiscovery(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "tool-discovery",
    ),
    mutationFn: ({
      assetId,
      versionId,
    }: {
      assetId: string;
      versionId: string;
    }) =>
      runMutation((signal) =>
        requestProjectMcpToolDiscovery(projectId, assetId, versionId, signal),
      ),
    onSuccess: whenActive((_data, { assetId, versionId }) =>
      queryClient.invalidateQueries({
        queryKey: projectMcpToolInventoryKey(
          accountId,
          projectId,
          assetId,
          versionId,
        ),
      }),
    ),
  });
}

export function useProjectSkillVersionFile(
  accountId: string,
  projectId: string,
  assetId: string,
  versionId: string,
  path: string,
  enabled = true,
) {
  return useQuery<SkillVersionFileContentResponse>({
    queryKey: projectSkillVersionFileKey(
      accountId,
      projectId,
      assetId,
      versionId,
      path,
    ),
    queryFn: ({ signal }) =>
      getProjectSkillVersionFile(projectId, assetId, versionId, path, signal),
    enabled: enabled && path !== "",
    staleTime: 0,
    gcTime: 0,
  });
}

export function useProjectSkillCredentialBindings(
  accountId: string,
  projectId: string,
  skillId: string,
  enabled = true,
) {
  return useQuery<SkillCredentialBindingsResponse>({
    queryKey: projectSkillCredentialBindingsKey(accountId, projectId, skillId),
    queryFn: ({ signal }) =>
      getProjectSkillCredentialBindings(projectId, skillId, signal),
    enabled,
  });
}

export function useProjectSkillPublishPlan(
  accountId: string,
  projectId: string,
  skillId: string,
  versionId: string,
  enabled = true,
) {
  return useQuery<SkillPublishPlanResponse>({
    queryKey: projectSkillPublishPlanKey(
      accountId,
      projectId,
      skillId,
      versionId,
    ),
    queryFn: ({ signal }) =>
      getProjectSkillPublishPlan(projectId, skillId, versionId, signal),
    enabled: enabled && skillId !== "" && versionId !== "",
    staleTime: 0,
  });
}

export function useUpdateProjectSkillCredentialBindings(
  accountId: string,
  projectId: string,
  skillId: string,
) {
  const queryClient = useQueryClient();
  const key = projectSkillCredentialBindingsKey(accountId, projectId, skillId);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectSkillCredentialBindingsMutationKey(
      accountId,
      projectId,
      skillId,
    ),
    mutationFn: (input: SkillCredentialBindingsInput) =>
      runMutation((signal) =>
        updateProjectSkillCredentialBindings(projectId, skillId, input, signal),
      ),
    onSuccess: whenActive((response: SkillCredentialBindingsResponse) => {
      queryClient.setQueryData(key, response);
      return invalidateProjectAgentRuntimeAssessments(
        queryClient,
        accountId,
        projectId,
      );
    }),
  });
}

export function useAdminAssetVersions(
  accountId: string,
  kind: AssetListKind,
  assetId: string,
) {
  return useQuery<VersionHistoryResponse>({
    queryKey: adminAssetVersionsKey(accountId, kind, assetId),
    queryFn: ({ signal }) => listAdminAssetVersions(kind, assetId, signal),
  });
}

export function useAdminProjectAssetVersions(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  assetId: string,
) {
  return useQuery<VersionHistoryResponse>({
    queryKey: adminProjectAssetVersionsKey(accountId, projectId, kind, assetId),
    queryFn: ({ signal }) =>
      listAdminProjectAssetVersions(projectId, kind, assetId, signal),
  });
}

export function useCreateConfiguredProjectMcp(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "add-configured",
    ),
    mutationFn: (input: CreateConfiguredMcpInput) =>
      runMutation((signal) =>
        createConfiguredProjectMcp(projectId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useUpdateConfiguredProjectMcp(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "update-configured",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: UpdateConfiguredMcpInput;
    }) =>
      runMutation((signal) =>
        updateConfiguredProjectMcp(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useImportProjectSkillArchive(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, "skills");
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "skills",
      "import",
    ),
    mutationFn: (archive: File) =>
      runMutation((signal) =>
        importProjectSkillArchive(projectId, archive, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

function isSkillVersionInput(
  input: VersionAuthoringInput,
): input is SkillVersionInput {
  return "files" in input;
}

function isMcpVersionInput(
  input: VersionAuthoringInput,
): input is McpVersionInput {
  return "transport" in input;
}

function createProjectVersionForKind(
  projectId: string,
  kind: AuthorableVersionKind,
  assetId: string,
  input: VersionAuthoringInput,
  signal?: AbortSignal,
) {
  if (kind === "skills" && isSkillVersionInput(input)) {
    return createProjectAssetVersion(projectId, kind, assetId, input, signal);
  }
  if (kind === "mcp-servers" && isMcpVersionInput(input)) {
    return createProjectAssetVersion(projectId, kind, assetId, input, signal);
  }
  throw new TypeError(`Version input does not match ${kind}`);
}

function createAdminProjectVersionForKind(
  projectId: string,
  kind: AuthorableVersionKind,
  assetId: string,
  input: VersionAuthoringInput,
) {
  if (kind === "skills" && isSkillVersionInput(input)) {
    return createAdminProjectAssetVersion(projectId, kind, assetId, input);
  }
  if (kind === "mcp-servers" && isMcpVersionInput(input)) {
    return createAdminProjectAssetVersion(projectId, kind, assetId, input);
  }
  throw new TypeError(`Version input does not match ${kind}`);
}

export function useCreateProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: AuthorableVersionKind | null,
) {
  const queryKind = kind ?? "agents";
  const invalidate = useProjectInvalidation(accountId, projectId, queryKind);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      queryKind,
      "create-version",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: VersionAuthoringInput;
    }) => {
      if (kind === null) {
        return Promise.reject(
          new TypeError("Agent revisions are managed internally"),
        );
      }
      return runMutation((signal) =>
        createProjectVersionForKind(projectId, kind, assetId, input, signal),
      );
    },
    onSuccess: whenActive(invalidate),
  });
}

export function useUpdateProjectAgentInstructions(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "agents",
      "update-instructions",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: AgentInstructionsInput;
    }) =>
      runMutation((signal) =>
        updateProjectAgentInstructions(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(
      (_data, variables: { assetId: string; input: AgentInstructionsInput }) =>
        invalidateProjectAgentMutationQueries(
          queryClient,
          accountId,
          projectId,
          variables.assetId,
        ),
    ),
  });
}

export function useUpdateProjectAgentCapabilityBindings(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "agents",
      "update-capability-bindings",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: AgentCapabilityBindingsInput;
    }) =>
      runMutation((signal) =>
        updateProjectAgentCapabilityBindings(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(
      (
        _data,
        variables: { assetId: string; input: AgentCapabilityBindingsInput },
      ) =>
        invalidateProjectAgentMutationQueries(
          queryClient,
          accountId,
          projectId,
          variables.assetId,
        ),
    ),
  });
}

export function useRestoreProjectAgentVersion(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "agents",
      "restore-version",
    ),
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) =>
      runMutation((signal) =>
        restoreProjectAgentVersion(
          projectId,
          assetId,
          versionId,
          input,
          signal,
        ),
      ),
    onSuccess: whenActive(
      (
        _data,
        variables: {
          assetId: string;
          versionId: string;
          input: ExpectedAssetVersionInput;
        },
      ) =>
        invalidateProjectAgentMutationQueries(
          queryClient,
          accountId,
          projectId,
          variables.assetId,
        ),
    ),
  });
}

export function useCreateAdminProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: AuthorableVersionKind | null,
) {
  const queryKind = kind ?? "agents";
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    queryKind,
  );
  return useMutation({
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: VersionAuthoringInput;
    }) => {
      if (kind === null) {
        return Promise.reject(
          new TypeError("Agent revisions are managed internally"),
        );
      }
      return createAdminProjectVersionForKind(projectId, kind, assetId, input);
    },
    onSuccess: invalidate,
  });
}

export function useForkProjectSkillVersion(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, "skills");
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "skills",
      "fork-version",
    ),
    mutationFn: ({
      assetId,
      sourceVersionId,
      input,
    }: {
      assetId: string;
      sourceVersionId: string;
      input: SkillFileForkInput;
    }) =>
      runMutation((signal) =>
        forkProjectSkillVersion(
          projectId,
          assetId,
          sourceVersionId,
          input,
          signal,
        ),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useChangeProjectAssetStatus<Kind extends MutableAssetKind>(
  accountId: string,
  projectId: string,
  kind: Kind,
) {
  const queryClient = useQueryClient();
  const invalidate = useProjectAssetListInvalidation(
    accountId,
    projectId,
    kind,
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      kind,
      "change-status",
    ),
    mutationFn: ({
      assetId,
      action,
      input,
    }: {
      assetId: string;
      action: ProjectAssetStatusAction<Kind>;
      input: ExpectedAssetVersionInput;
    }) =>
      runMutation((signal) =>
        changeProjectAssetStatus(
          projectId,
          kind,
          assetId,
          action,
          input,
          signal,
        ),
      ),
    onSuccess: whenActive(async (response: AssetMutationResponse) => {
      if (kind === "agents") {
        queryClient.setQueryData<ProjectAssetList>(
          projectAssetKey(accountId, projectId, "agents"),
          (current) =>
            applyProjectAgentMutationToCatalog(current, response.item),
        );
      }
      await invalidate();
    }),
    onError: whenActive(
      async (
        error: unknown,
        variables: {
          assetId: string;
          action: ProjectAssetStatusAction<Kind>;
          input: ExpectedAssetVersionInput;
        },
      ) => {
        if (kind !== "agents" || !isProjectAgentCasConflict(error)) return;
        await invalidateProjectAgentConflictQueries(
          queryClient,
          accountId,
          projectId,
          variables.assetId,
        );
      },
    ),
  });
}

export function useDeleteProjectSkill(accountId: string, projectId: string) {
  const queryClient = useQueryClient();
  const invalidate = useProjectInvalidation(accountId, projectId, "skills");
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "skills",
      "delete",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: ExpectedAssetVersionInput;
    }) =>
      runMutation((signal) =>
        deleteProjectSkill(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(
      (
        _data: void,
        variables: {
          assetId: string;
          input: ExpectedAssetVersionInput;
        },
      ) => {
        queryClient.setQueryData<ProjectAssetList>(
          projectAssetKey(accountId, projectId, "skills"),
          (current) =>
            current
              ? {
                  ...current,
                  project_items: current.project_items.filter(
                    (item) => item.id !== variables.assetId,
                  ),
                }
              : current,
        );
        queryClient.removeQueries({
          queryKey: projectAssetVersionsKey(
            accountId,
            projectId,
            "skills",
            variables.assetId,
          ),
        });
        void invalidate();
      },
    ),
  });
}

export function useDeleteProjectAgent(accountId: string, projectId: string) {
  const queryClient = useQueryClient();
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "agents",
      "delete",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: ExpectedAssetVersionInput;
    }) =>
      runMutation((signal) =>
        deleteProjectAgent(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(
      (
        _data: void,
        variables: {
          assetId: string;
          input: ExpectedAssetVersionInput;
        },
      ) => {
        queryClient.setQueryData<ProjectAssetList>(
          projectAssetKey(accountId, projectId, "agents"),
          (current) =>
            current
              ? {
                  ...current,
                  project_items: current.project_items.filter(
                    (item) => item.id !== variables.assetId,
                  ),
                }
              : current,
        );
        queryClient.removeQueries({
          queryKey: projectAssetVersionsKey(
            accountId,
            projectId,
            "agents",
            variables.assetId,
          ),
        });
        void invalidateProjectAgentConflictQueries(
          queryClient,
          accountId,
          projectId,
        );
      },
    ),
    onError: whenActive(
      async (
        error: unknown,
        variables: {
          assetId: string;
          input: ExpectedAssetVersionInput;
        },
      ) => {
        if (!isProjectAgentCasConflict(error)) return;
        await invalidateProjectAgentConflictQueries(
          queryClient,
          accountId,
          projectId,
          variables.assetId,
        );
      },
    ),
  });
}

export function useDeleteProjectMcp(accountId: string, projectId: string) {
  const queryClient = useQueryClient();
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "delete",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: ExpectedAssetVersionInput;
    }) =>
      runMutation((signal) =>
        deleteProjectMcp(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(
      (
        _data: void,
        variables: {
          assetId: string;
          input: ExpectedAssetVersionInput;
        },
      ) => {
        queryClient.setQueryData<ProjectAssetList>(
          projectAssetKey(accountId, projectId, "mcp-servers"),
          (current) =>
            current
              ? {
                  ...current,
                  project_items: current.project_items.filter(
                    (item) => item.id !== variables.assetId,
                  ),
                }
              : current,
        );
        queryClient.removeQueries({
          queryKey: projectAssetVersionsKey(
            accountId,
            projectId,
            "mcp-servers",
            variables.assetId,
          ),
        });
        void invalidate();
      },
    ),
  });
}

export function useChangeAdminProjectAssetStatus<Kind extends MutableAssetKind>(
  accountId: string,
  projectId: string,
  kind: Kind,
) {
  const invalidate = useAdminProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      action,
      input,
    }: {
      assetId: string;
      action: AdminProjectAssetStatusAction<Kind>;
      input: ExpectedAssetVersionInput;
    }) =>
      changeAdminProjectAssetStatus(projectId, kind, assetId, action, input),
    onSuccess: invalidate,
  });
}

export function usePublishProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: PublishableVersionKind,
) {
  const queryClient = useQueryClient();
  const queryKind = kind;
  const invalidate = useProjectInvalidation(accountId, projectId, queryKind);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      queryKind,
      "publish-version",
    ),
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: PublishAssetVersionInput | SkillPublishAssetVersionInput;
    }) => {
      return runMutation((signal) =>
        kind === "skills"
          ? publishProjectAssetVersion(
              projectId,
              kind,
              assetId,
              versionId,
              input as SkillPublishAssetVersionInput,
              signal,
            )
          : publishProjectAssetVersion(
              projectId,
              kind,
              assetId,
              versionId,
              input as PublishAssetVersionInput,
              signal,
            ),
      );
    },
    onSuccess: whenActive(
      (
        _data,
        variables: {
          assetId: string;
          versionId: string;
          input: PublishAssetVersionInput | SkillPublishAssetVersionInput;
        },
      ) =>
        kind === "agents"
          ? invalidateProjectAgentMutationQueries(
              queryClient,
              accountId,
              projectId,
              variables.assetId,
            )
          : invalidate(),
    ),
    onError: whenActive(
      async (
        error: unknown,
        variables: {
          assetId: string;
          versionId: string;
          input: PublishAssetVersionInput | SkillPublishAssetVersionInput;
        },
      ) => {
        if (kind !== "agents" || !isProjectAgentCasConflict(error)) return;
        await invalidateProjectAgentConflictQueries(
          queryClient,
          accountId,
          projectId,
          variables.assetId,
        );
      },
    ),
  });
}

export function usePublishAdminProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: PublishableVersionKind,
) {
  const queryKind = kind;
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    queryKind,
  );
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: PublishAssetVersionInput;
    }) => {
      return publishAdminProjectAssetVersion(
        projectId,
        kind,
        assetId,
        versionId,
        input,
      );
    },
    onSuccess: invalidate,
  });
}

export function useRevokeProjectCredential(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "credentials",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "credentials",
      "revoke",
    ),
    mutationFn: ({
      credentialId,
      input,
    }: {
      credentialId: string;
      input: RevokeCredentialInput;
    }) =>
      runMutation((signal) =>
        revokeProjectCredential(projectId, credentialId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useRevokeAdminCredential(accountId: string) {
  const invalidate = useAdminInvalidation(accountId, "credentials");
  return useMutation({
    mutationFn: ({
      credentialId,
      input,
    }: {
      credentialId: string;
      input: RevokeCredentialInput;
    }) => revokeAdminCredential(credentialId, input),
    onSuccess: invalidate,
  });
}

export function useSubmitProjectMcpVersion(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "submit-version",
    ),
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) =>
      runMutation((signal) =>
        submitProjectMcpVersion(projectId, assetId, versionId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useSubmitAdminProjectMcpVersion(
  accountId: string,
  projectId: string,
) {
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) => submitAdminProjectMcpVersion(projectId, assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useApproveProjectMcpVersion(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "approve-version",
    ),
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ApproveMcpInput;
    }) =>
      runMutation((signal) =>
        approveProjectMcpVersion(projectId, assetId, versionId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useApproveAdminProjectMcpVersion(
  accountId: string,
  projectId: string,
) {
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ApproveMcpInput;
    }) => approveAdminProjectMcpVersion(projectId, assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useConfigureAdminMcpCredentialGrants(accountId: string) {
  const invalidate = useAdminInvalidation(accountId, "mcp-servers");
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ConfigureSystemMcpCredentialGrantsInput;
    }) => configureAdminMcpCredentialGrants(assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useEnableProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  const invalidate = useProjectAssetListInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      BINDING_LIST_KIND[kind],
      "enable-binding",
    ),
    mutationFn: (input: EnableSystemBindingInput) =>
      runMutation((signal) =>
        enableProjectSystemBinding(projectId, kind, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useSyncCurrentProjectSystemMcpBinding(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectAssetListInvalidation(
    accountId,
    projectId,
    "mcp-servers",
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      "mcp-servers",
      "sync-current-binding",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: SyncCurrentSystemMcpBindingInput;
    }) =>
      runMutation((signal) =>
        syncCurrentProjectSystemMcpBinding(projectId, assetId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

function useMoveProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
  action: "upgrade" | "rollback",
) {
  const invalidate = useProjectAssetListInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      BINDING_LIST_KIND[kind],
      `${action}-binding`,
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: MoveSystemBindingInput;
    }) =>
      runMutation((signal) =>
        action === "upgrade"
          ? upgradeProjectSystemBinding(projectId, kind, assetId, input, signal)
          : rollbackProjectSystemBinding(
              projectId,
              kind,
              assetId,
              input,
              signal,
            ),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useUpgradeProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  return useMoveProjectSystemBinding(accountId, projectId, kind, "upgrade");
}

export function useRollbackProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  return useMoveProjectSystemBinding(accountId, projectId, kind, "rollback");
}

export function useDisableProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  const invalidate = useProjectAssetListInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      BINDING_LIST_KIND[kind],
      "disable-binding",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: DisableSystemBindingInput;
    }) =>
      runMutation((signal) =>
        disableProjectSystemBinding(projectId, kind, assetId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useEnableAdminProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  return useMutation({
    mutationFn: (input: EnableSystemBindingInput) =>
      enableAdminProjectSystemBinding(projectId, kind, input),
    onSuccess: invalidate,
  });
}

function useMoveAdminProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
  action: "upgrade" | "rollback",
) {
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  return useMutation({
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: MoveSystemBindingInput;
    }) =>
      action === "upgrade"
        ? upgradeAdminProjectSystemBinding(projectId, kind, assetId, input)
        : rollbackAdminProjectSystemBinding(projectId, kind, assetId, input),
    onSuccess: invalidate,
  });
}

export function useUpgradeAdminProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  return useMoveAdminProjectSystemBinding(
    accountId,
    projectId,
    kind,
    "upgrade",
  );
}

export function useRollbackAdminProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  return useMoveAdminProjectSystemBinding(
    accountId,
    projectId,
    kind,
    "rollback",
  );
}

export function useDisableAdminProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  const invalidate = useAdminProjectInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  return useMutation({
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: DisableSystemBindingInput;
    }) => disableAdminProjectSystemBinding(projectId, kind, assetId, input),
    onSuccess: invalidate,
  });
}
