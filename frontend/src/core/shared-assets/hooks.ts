import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import {
  changeAdminAssetStatus,
  changeProjectAssetStatus,
  createAdminAsset,
  createProjectAsset,
  listAdminAssets,
  listProjectAssets,
} from "./api";
import { adminAssetKey, projectAssetKey } from "./query-keys";
import type {
  AdminAssetList,
  AdminCredentialList,
  AssetListKind,
  CreateAssetInput,
  ExpectedAssetVersionInput,
  ProjectAssetList,
  ProjectCredentialList,
} from "./types";

type MutableAssetKind = Exclude<AssetListKind, "credentials">;

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

export function useProjectAssets(
  accountId: string | null | undefined,
  projectId: string | null | undefined,
  kind: AssetListKind,
) {
  return useQuery<ProjectAssetList | ProjectCredentialList>({
    queryKey: projectAssetKey(
      accountId ?? "missing-account",
      projectId ?? "missing-project",
      kind,
    ),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listProjectAssets(projectId ?? "", kind, signal)
        : listProjectAssets(projectId ?? "", kind, signal),
    enabled: Boolean(accountId && projectId),
  });
}

export function useAdminAssets(
  accountId: string | null | undefined,
  kind: AssetListKind,
) {
  return useQuery<AdminAssetList | AdminCredentialList>({
    queryKey: adminAssetKey(accountId ?? "missing-account", kind),
    queryFn: ({ signal }) =>
      kind === "credentials"
        ? listAdminAssets(kind, signal)
        : listAdminAssets(kind, signal),
    enabled: Boolean(accountId),
  });
}

export function useCreateProjectAsset(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateAssetInput) =>
      createProjectAsset(projectId, kind, input),
    onSuccess: () =>
      invalidateProjectAssetQueries(queryClient, accountId, projectId, kind),
  });
}

export function useCreateAdminAsset(accountId: string, kind: MutableAssetKind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateAssetInput) => createAdminAsset(kind, input),
    onSuccess: () => invalidateAdminAssetQueries(queryClient, accountId, kind),
  });
}

export function useChangeProjectAssetStatus(
  accountId: string,
  projectId: string,
  kind: MutableAssetKind,
) {
  const queryClient = useQueryClient();
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
    onSuccess: () =>
      invalidateProjectAssetQueries(queryClient, accountId, projectId, kind),
  });
}

export function useChangeAdminAssetStatus(
  accountId: string,
  kind: MutableAssetKind,
) {
  const queryClient = useQueryClient();
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
    onSuccess: () => invalidateAdminAssetQueries(queryClient, accountId, kind),
  });
}
