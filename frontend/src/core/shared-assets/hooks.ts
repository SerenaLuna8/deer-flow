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
  approveAdminProjectMcpVersion,
  approveProjectMcpVersion,
  changeAdminProjectAssetStatus,
  changeProjectAssetStatus,
  configureAdminMcpCredentialGrants,
  createAdminProjectAsset,
  createAdminProjectAssetVersion,
  createProjectAsset,
  createProjectAssetVersion,
  deleteProjectSkill,
  disableAdminProjectSystemBinding,
  disableProjectSystemBinding,
  enableAdminProjectSystemBinding,
  enableProjectSystemBinding,
  forkProjectSkillVersion,
  getProjectSkillVersionFile,
  getAdminCredentialRotationStatus,
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
  revokeAdminCredential,
  revokeProjectCredential,
  rollbackProjectSystemBinding,
  rollbackAdminProjectSystemBinding,
  submitAdminProjectMcpVersion,
  submitProjectMcpVersion,
  upgradeAdminProjectSystemBinding,
  upgradeProjectSystemBinding,
  type ProjectAssetStatusAction,
} from "./api";
import {
  adminAssetKey,
  adminAssetVersionsKey,
  adminCredentialRotationStatusKey,
  adminProjectAssetKey,
  adminProjectAssetVersionsKey,
  projectAssetKey,
  projectAssetMutationKey,
  projectAssetVersionsKey,
  projectSkillVersionFileKey,
  systemCatalogKey,
} from "./query-keys";
import type {
  AdminAssetList,
  AdminCredentialList,
  AgentVersionInput,
  ApproveMcpInput,
  AssetKind,
  AssetListKind,
  CreateAssetInput,
  CredentialRotationStatus,
  ConfigureSystemMcpCredentialGrantsInput,
  DisableSystemBindingInput,
  EnableSystemBindingInput,
  ExpectedAssetVersionInput,
  MoveSystemBindingInput,
  McpVersionInput,
  ProjectAssetList,
  ProjectCredentialList,
  RevokeCredentialInput,
  SkillVersionInput,
  SkillFileForkInput,
  SkillVersionFileContentResponse,
  VersionHistoryResponse,
} from "./types";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
type VersionAuthoringInput =
  | AgentVersionInput
  | SkillVersionInput
  | McpVersionInput;

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
    invalidateProjectAssetQueries(queryClient, accountId, projectId, kind);
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

export function useAdminCredentialRotationStatus(accountId: string) {
  return useQuery<CredentialRotationStatus>({
    queryKey: adminCredentialRotationStatusKey(accountId),
    queryFn: ({ signal }) => getAdminCredentialRotationStatus(signal),
  });
}

export function useProjectAssetVersions(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
  assetId: string,
) {
  return useQuery<VersionHistoryResponse>({
    queryKey: projectAssetVersionsKey(accountId, projectId, kind, assetId),
    queryFn: ({ signal }) =>
      listProjectAssetVersions(projectId, kind, assetId, signal),
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

export function useCreateProjectAsset(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(accountId, projectId, kind, "create"),
    mutationFn: (input: CreateAssetInput) =>
      runMutation((signal) =>
        createProjectAsset(projectId, kind, input, signal),
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

export function useCreateAdminProjectAsset(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useAdminProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: (input: CreateAssetInput) =>
      createAdminProjectAsset(projectId, kind, input),
    onSuccess: invalidate,
  });
}

function isAgentVersionInput(
  input: VersionAuthoringInput,
): input is AgentVersionInput {
  return "soul" in input && "model_ref" in input;
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
  kind: MutableAssetKind,
  assetId: string,
  input: VersionAuthoringInput,
  signal?: AbortSignal,
) {
  if (kind === "agents" && isAgentVersionInput(input)) {
    return createProjectAssetVersion(projectId, kind, assetId, input, signal);
  }
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
  kind: MutableAssetKind,
  assetId: string,
  input: VersionAuthoringInput,
) {
  if (kind === "agents" && isAgentVersionInput(input)) {
    return createAdminProjectAssetVersion(projectId, kind, assetId, input);
  }
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
  kind: MutableAssetKind,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      kind,
      "create-version",
    ),
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: VersionAuthoringInput;
    }) =>
      runMutation((signal) =>
        createProjectVersionForKind(projectId, kind, assetId, input, signal),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function useCreateAdminProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useAdminProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: VersionAuthoringInput;
    }) => createAdminProjectVersionForKind(projectId, kind, assetId, input),
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
    onSuccess: whenActive(invalidate),
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
      action: ProjectAssetStatusAction<Kind>;
      input: ExpectedAssetVersionInput;
    }) =>
      changeAdminProjectAssetStatus(projectId, kind, assetId, action, input),
    onSuccess: invalidate,
  });
}

export function usePublishProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
  const { runMutation, whenActive } = useProjectMutationRunner(
    accountId,
    projectId,
  );
  return useMutation({
    mutationKey: projectAssetMutationKey(
      accountId,
      projectId,
      kind,
      "publish-version",
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
        publishProjectAssetVersion(
          projectId,
          kind,
          assetId,
          versionId,
          input,
          signal,
        ),
      ),
    onSuccess: whenActive(invalidate),
  });
}

export function usePublishAdminProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useAdminProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) =>
      publishAdminProjectAssetVersion(
        projectId,
        kind,
        assetId,
        versionId,
        input,
      ),
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
