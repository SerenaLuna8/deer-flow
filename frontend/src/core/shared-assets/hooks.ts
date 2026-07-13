import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import {
  approveAdminMcpVersion,
  approveProjectMcpVersion,
  changeAdminAssetStatus,
  changeProjectAssetStatus,
  createAdminAsset,
  createAdminAssetVersion,
  createAdminCredential,
  createProjectAsset,
  createProjectAssetVersion,
  createProjectCredential,
  disableProjectSystemBinding,
  enableProjectSystemBinding,
  listAdminAssetVersions,
  listAdminAssets,
  listProjectAssetVersions,
  listProjectAssets,
  publishAdminAssetVersion,
  publishProjectAssetVersion,
  replaceAdminCredential,
  replaceProjectCredential,
  revokeAdminCredential,
  revokeProjectCredential,
  rollbackProjectSystemBinding,
  submitAdminMcpVersion,
  submitProjectMcpVersion,
  upgradeProjectSystemBinding,
} from "./api";
import {
  adminAssetKey,
  adminAssetVersionsKey,
  projectAssetKey,
  projectAssetVersionsKey,
} from "./query-keys";
import type {
  AdminAssetList,
  AdminCredentialList,
  AgentVersionInput,
  ApproveMcpInput,
  AssetKind,
  AssetListKind,
  CreateAssetInput,
  CreateCredentialInput,
  DisableSystemBindingInput,
  EnableSystemBindingInput,
  ExpectedAssetVersionInput,
  MoveSystemBindingInput,
  McpVersionInput,
  ProjectAssetList,
  ProjectCredentialList,
  ReplaceCredentialInput,
  RevokeCredentialInput,
  SkillVersionInput,
  VersionHistoryResponse,
} from "./types";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;
type VersionInputByKind = {
  agents: AgentVersionInput;
  skills: SkillVersionInput;
  "mcp-servers": McpVersionInput;
};

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

function useProjectInvalidation(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  const queryClient = useQueryClient();
  return () =>
    invalidateProjectAssetQueries(queryClient, accountId, projectId, kind);
}

function useAdminInvalidation(accountId: string, kind: AssetListKind) {
  const queryClient = useQueryClient();
  return () => invalidateAdminAssetQueries(queryClient, accountId, kind);
}

export function useProjectAssets(
  accountId: string,
  projectId: string,
  kind: AssetListKind,
) {
  return useQuery<ProjectAssetList | ProjectCredentialList>({
    queryKey: projectAssetKey(accountId, projectId, kind),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listProjectAssets(projectId, kind, signal)
        : listProjectAssets(projectId, kind, signal),
  });
}

export function useAdminAssets(accountId: string, kind: AssetListKind) {
  return useQuery<AdminAssetList | AdminCredentialList>({
    queryKey: adminAssetKey(accountId, kind),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listAdminAssets(kind, signal)
        : listAdminAssets(kind, signal),
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

export function useCreateProjectAsset(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: (input: CreateAssetInput) =>
      createProjectAsset(projectId, kind, input),
    onSuccess: invalidate,
  });
}

export function useCreateAdminAsset(accountId: string, kind: MutableAssetKind) {
  const invalidate = useAdminInvalidation(accountId, kind);
  return useMutation({
    mutationFn: (input: CreateAssetInput) => createAdminAsset(kind, input),
    onSuccess: invalidate,
  });
}

function createProjectVersionForKind<K extends MutableAssetKind>(
  projectId: string,
  kind: K,
  assetId: string,
  input: VersionInputByKind[K],
) {
  if (kind === "agents") {
    return createProjectAssetVersion(
      projectId,
      kind,
      assetId,
      input as AgentVersionInput,
    );
  }
  if (kind === "skills") {
    return createProjectAssetVersion(
      projectId,
      kind,
      assetId,
      input as SkillVersionInput,
    );
  }
  return createProjectAssetVersion(
    projectId,
    "mcp-servers",
    assetId,
    input as McpVersionInput,
  );
}

function createAdminVersionForKind<K extends MutableAssetKind>(
  kind: K,
  assetId: string,
  input: VersionInputByKind[K],
) {
  if (kind === "agents") {
    return createAdminAssetVersion(kind, assetId, input as AgentVersionInput);
  }
  if (kind === "skills") {
    return createAdminAssetVersion(kind, assetId, input as SkillVersionInput);
  }
  return createAdminAssetVersion(
    "mcp-servers",
    assetId,
    input as McpVersionInput,
  );
}

export function useCreateProjectAssetVersion<K extends MutableAssetKind>(
  accountId: string,
  projectId: string,
  kind: K,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: VersionInputByKind[K];
    }) => createProjectVersionForKind(projectId, kind, assetId, input),
    onSuccess: invalidate,
  });
}

export function useCreateAdminAssetVersion<K extends MutableAssetKind>(
  accountId: string,
  kind: K,
) {
  const invalidate = useAdminInvalidation(accountId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      input,
    }: {
      assetId: string;
      input: VersionInputByKind[K];
    }) => createAdminVersionForKind(kind, assetId, input),
    onSuccess: invalidate,
  });
}

export function useCreateProjectCredential(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "credentials",
  );
  return useMutation({
    mutationFn: (input: CreateCredentialInput) =>
      createProjectCredential(projectId, input),
    onSuccess: invalidate,
  });
}

export function useCreateAdminCredential(accountId: string) {
  const invalidate = useAdminInvalidation(accountId, "credentials");
  return useMutation({
    mutationFn: (input: CreateCredentialInput) => createAdminCredential(input),
    onSuccess: invalidate,
  });
}

export function useChangeProjectAssetStatus(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      action,
      input,
    }: {
      assetId: string;
      action: "archive" | "suspend";
      input: ExpectedAssetVersionInput;
    }) => changeProjectAssetStatus(projectId, kind, assetId, action, input),
    onSuccess: invalidate,
  });
}

export function useChangeAdminAssetStatus(
  accountId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useAdminInvalidation(accountId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      action,
      input,
    }: {
      assetId: string;
      action: "archive" | "suspend";
      input: ExpectedAssetVersionInput;
    }) => changeAdminAssetStatus(kind, assetId, action, input),
    onSuccess: invalidate,
  });
}

export function usePublishProjectAssetVersion(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useProjectInvalidation(accountId, projectId, kind);
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
      publishProjectAssetVersion(projectId, kind, assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function usePublishAdminAssetVersion(
  accountId: string,
  kind: MutableAssetKind,
) {
  const invalidate = useAdminInvalidation(accountId, kind);
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) => publishAdminAssetVersion(kind, assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useReplaceProjectCredential(
  accountId: string,
  projectId: string,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    "credentials",
  );
  return useMutation({
    mutationFn: ({
      credentialId,
      input,
    }: {
      credentialId: string;
      input: ReplaceCredentialInput;
    }) => replaceProjectCredential(projectId, credentialId, input),
    onSuccess: invalidate,
  });
}

export function useReplaceAdminCredential(accountId: string) {
  const invalidate = useAdminInvalidation(accountId, "credentials");
  return useMutation({
    mutationFn: ({
      credentialId,
      input,
    }: {
      credentialId: string;
      input: ReplaceCredentialInput;
    }) => replaceAdminCredential(credentialId, input),
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
  return useMutation({
    mutationFn: ({
      credentialId,
      input,
    }: {
      credentialId: string;
      input: RevokeCredentialInput;
    }) => revokeProjectCredential(projectId, credentialId, input),
    onSuccess: invalidate,
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
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) => submitProjectMcpVersion(projectId, assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useSubmitAdminMcpVersion(accountId: string) {
  const invalidate = useAdminInvalidation(accountId, "mcp-servers");
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ExpectedAssetVersionInput;
    }) => submitAdminMcpVersion(assetId, versionId, input),
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
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ApproveMcpInput;
    }) => approveProjectMcpVersion(projectId, assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useApproveAdminMcpVersion(accountId: string) {
  const invalidate = useAdminInvalidation(accountId, "mcp-servers");
  return useMutation({
    mutationFn: ({
      assetId,
      versionId,
      input,
    }: {
      assetId: string;
      versionId: string;
      input: ApproveMcpInput;
    }) => approveAdminMcpVersion(assetId, versionId, input),
    onSuccess: invalidate,
  });
}

export function useEnableProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
) {
  const invalidate = useProjectInvalidation(
    accountId,
    projectId,
    BINDING_LIST_KIND[kind],
  );
  return useMutation({
    mutationFn: (input: EnableSystemBindingInput) =>
      enableProjectSystemBinding(projectId, kind, input),
    onSuccess: invalidate,
  });
}

function useMoveProjectSystemBinding(
  accountId: string,
  projectId: string,
  kind: AssetKind,
  action: "upgrade" | "rollback",
) {
  const invalidate = useProjectInvalidation(
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
        ? upgradeProjectSystemBinding(projectId, kind, assetId, input)
        : rollbackProjectSystemBinding(projectId, kind, assetId, input),
    onSuccess: invalidate,
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
  const invalidate = useProjectInvalidation(
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
    }) => disableProjectSystemBinding(projectId, kind, assetId, input),
    onSuccess: invalidate,
  });
}
