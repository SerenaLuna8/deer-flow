"use client";

import {
  skipToken,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";

import {
  createWorkflowDefinitionTransport,
  type WorkflowDefinitionListInput,
  type WorkflowDefinitionTransport,
  type WorkflowVersionListInput,
} from "./definition-api";
import {
  workflowDefinitionListQueryV1Schema,
  workflowVersionListQueryV1Schema,
  type WorkflowCredentialGrantMutationRequestV1,
  type WorkflowDefinitionArchiveRequestV1,
  type WorkflowDefinitionCreateRequestV1,
  type WorkflowDefinitionListQueryV1,
  type WorkflowDraftSaveRequestV1,
  type WorkflowDraftValidateRequestV1,
  type WorkflowPublishRequestV1,
  type WorkflowVersionListQueryV1,
} from "./definition-contracts";
import { projectWorkflowQueryKey } from "./query-keys";

function inactiveScopeError(): Error {
  const error = new Error("Project Workflow scope is inactive");
  error.name = "AbortError";
  return error;
}

function normalizedDefinitionListQuery(
  query: WorkflowDefinitionListInput = {},
): WorkflowDefinitionListQueryV1 {
  return workflowDefinitionListQueryV1Schema.parse(query);
}

function normalizedVersionListQuery(
  query: WorkflowVersionListInput = {},
): WorkflowVersionListQueryV1 {
  return workflowVersionListQueryV1Schema.parse(query);
}

async function activeResult<T>(
  access: PrivateWorkAccess,
  result: Promise<T>,
): Promise<T> {
  const value = await result;
  if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
  return value;
}

async function runScopedMutation<T>(
  access: PrivateWorkAccess,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  return runPrivateWorkAbortable(access, async (signal) => {
    if (!signal) throw inactiveScopeError();
    return activeResult(access, operation(signal));
  });
}

async function invalidateWhenActive(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  queryKey: readonly unknown[],
): Promise<void> {
  if (!isPrivateWorkAccessActive(access)) return;
  await queryClient.invalidateQueries({ queryKey });
}

export function workflowDefinitionsRootKey(scope: ProjectClientScope) {
  return projectWorkflowQueryKey(scope, "definitions");
}

export function workflowDefinitionRootKey(
  scope: ProjectClientScope,
  workflowId: string,
) {
  return [...workflowDefinitionsRootKey(scope), workflowId] as const;
}

export function workflowDefinitionsQueryKey(
  scope: ProjectClientScope,
  query: WorkflowDefinitionListInput = {},
) {
  return [
    ...workflowDefinitionsRootKey(scope),
    "list",
    normalizedDefinitionListQuery(query),
  ] as const;
}

export function workflowDefinitionQueryKey(
  scope: ProjectClientScope,
  workflowId: string,
) {
  return [...workflowDefinitionRootKey(scope, workflowId), "detail"] as const;
}

export function workflowDraftQueryKey(
  scope: ProjectClientScope,
  workflowId: string,
) {
  return [...workflowDefinitionRootKey(scope, workflowId), "draft"] as const;
}

export function workflowVersionsRootKey(
  scope: ProjectClientScope,
  workflowId: string,
) {
  return [...workflowDefinitionRootKey(scope, workflowId), "versions"] as const;
}

export function workflowVersionsQueryKey(
  scope: ProjectClientScope,
  workflowId: string,
  query: WorkflowVersionListInput = {},
) {
  return [
    ...workflowVersionsRootKey(scope, workflowId),
    "list",
    normalizedVersionListQuery(query),
  ] as const;
}

export function workflowVersionQueryKey(
  scope: ProjectClientScope,
  workflowId: string,
  versionId: string,
) {
  return [...workflowVersionsRootKey(scope, workflowId), versionId] as const;
}

export function workflowDefinitionMutationKey(
  scope: ProjectClientScope,
  workflowId: string | null,
  action: string,
) {
  return [
    ...(workflowId === null
      ? workflowDefinitionsRootKey(scope)
      : workflowDefinitionRootKey(scope, workflowId)),
    "mutation",
    action,
  ] as const;
}

export function workflowDefinitionsQueryOptions(
  access: PrivateWorkAccess,
  query: WorkflowDefinitionListInput = {},
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  const normalized = normalizedDefinitionListQuery(query);
  return {
    queryKey: workflowDefinitionsQueryKey(scope, normalized),
    queryFn: enabled
      ? async ({
          signal,
          pageParam,
        }: {
          signal: AbortSignal;
          pageParam: string | null;
        }) =>
          activeResult(
            access,
            transport.listDefinitions(
              scope,
              { ...normalized, cursor: pageParam },
              { signal },
            ),
          )
      : skipToken,
    initialPageParam: normalized.cursor,
    getNextPageParam: (lastPage: { next_cursor: string | null }) =>
      lastPage.next_cursor ?? undefined,
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export function workflowDefinitionQueryOptions(
  access: PrivateWorkAccess,
  workflowId: string | null | undefined,
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  const active = enabled && Boolean(workflowId);
  return {
    queryKey: workflowDefinitionQueryKey(scope, workflowId ?? ""),
    queryFn: active
      ? ({ signal }: { signal: AbortSignal }) =>
          activeResult(
            access,
            transport.readDefinition(scope, workflowId ?? "", { signal }),
          )
      : skipToken,
    enabled: active,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export function workflowDraftQueryOptions(
  access: PrivateWorkAccess,
  workflowId: string | null | undefined,
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  const active = enabled && Boolean(workflowId);
  return {
    queryKey: workflowDraftQueryKey(scope, workflowId ?? ""),
    queryFn: active
      ? ({ signal }: { signal: AbortSignal }) =>
          activeResult(
            access,
            transport.readDraft(scope, workflowId ?? "", { signal }),
          )
      : skipToken,
    enabled: active,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export function workflowVersionsQueryOptions(
  access: PrivateWorkAccess,
  workflowId: string | null | undefined,
  query: WorkflowVersionListInput = {},
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  const normalized = normalizedVersionListQuery(query);
  const active = enabled && Boolean(workflowId);
  return {
    queryKey: workflowVersionsQueryKey(scope, workflowId ?? "", normalized),
    queryFn: active
      ? async ({
          signal,
          pageParam,
        }: {
          signal: AbortSignal;
          pageParam: string | null;
        }) =>
          activeResult(
            access,
            transport.listVersions(
              scope,
              workflowId ?? "",
              { ...normalized, cursor: pageParam },
              { signal },
            ),
          )
      : skipToken,
    initialPageParam: normalized.cursor,
    getNextPageParam: (lastPage: { next_cursor: string | null }) =>
      lastPage.next_cursor ?? undefined,
    enabled: active,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export function workflowVersionQueryOptions(
  access: PrivateWorkAccess,
  workflowId: string | null | undefined,
  versionId: string | null | undefined,
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  const active = enabled && Boolean(workflowId) && Boolean(versionId);
  return {
    queryKey: workflowVersionQueryKey(scope, workflowId ?? "", versionId ?? ""),
    queryFn: active
      ? ({ signal }: { signal: AbortSignal }) =>
          activeResult(
            access,
            transport.readVersion(scope, workflowId ?? "", versionId ?? "", {
              signal,
            }),
          )
      : skipToken,
    enabled: active,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export type CreateWorkflowDefinitionVariables = {
  body: WorkflowDefinitionCreateRequestV1;
  idempotencyKey: string;
};

export type ArchiveWorkflowDefinitionVariables = {
  body: WorkflowDefinitionArchiveRequestV1;
  idempotencyKey: string;
};

export type SaveWorkflowDraftVariables = {
  body: WorkflowDraftSaveRequestV1;
  idempotencyKey: string;
};

export type ValidateWorkflowDraftVariables = {
  body: WorkflowDraftValidateRequestV1;
};

export type PublishWorkflowDraftVariables = {
  body: WorkflowPublishRequestV1;
  idempotencyKey: string;
};

export type PutWorkflowDraftGrantIntentVariables = {
  slotId: string;
  body: WorkflowCredentialGrantMutationRequestV1;
  idempotencyKey: string;
};

export type DeleteWorkflowDraftGrantIntentVariables = {
  slotId: string;
  idempotencyKey: string;
};

export type PutWorkflowVersionGrantVariables = {
  slotId: string;
  body: WorkflowCredentialGrantMutationRequestV1;
  idempotencyKey: string;
};

export type RevokeWorkflowVersionGrantVariables = {
  slotId: string;
  idempotencyKey: string;
};

export function createWorkflowDefinitionMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(scope, null, "create"),
    mutationFn: (variables: CreateWorkflowDefinitionVariables) =>
      runScopedMutation(access, (signal) =>
        transport.createDefinition(scope, variables.body, {
          signal,
          idempotencyKey: variables.idempotencyKey,
        }),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionsRootKey(scope),
      ),
  } as const;
}

export function archiveWorkflowDefinitionMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string | null | undefined,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  const targetWorkflowId = workflowId ?? "";
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      targetWorkflowId,
      "archive",
    ),
    mutationFn: (variables: ArchiveWorkflowDefinitionVariables) =>
      runScopedMutation(access, (signal) =>
        transport.archiveDefinition(scope, targetWorkflowId, variables.body, {
          signal,
          idempotencyKey: variables.idempotencyKey,
        }),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionsRootKey(scope),
      ),
  } as const;
}

export function saveWorkflowDraftMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(scope, workflowId, "save-draft"),
    mutationFn: (variables: SaveWorkflowDraftVariables) =>
      runScopedMutation(access, (signal) =>
        transport.saveDraft(scope, workflowId, variables.body, {
          signal,
          idempotencyKey: variables.idempotencyKey,
        }),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionRootKey(scope, workflowId),
      ),
  } as const;
}

export function validateWorkflowDraftMutationOptions(
  access: PrivateWorkAccess,
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      workflowId,
      "validate-draft",
    ),
    mutationFn: (variables: ValidateWorkflowDraftVariables) =>
      runScopedMutation(access, (signal) =>
        transport.validateDraft(scope, workflowId, variables.body, { signal }),
      ),
  } as const;
}

export function publishWorkflowDraftMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      workflowId,
      "publish-draft",
    ),
    mutationFn: (variables: PublishWorkflowDraftVariables) =>
      runScopedMutation(access, (signal) =>
        transport.publishDraft(scope, workflowId, variables.body, {
          signal,
          idempotencyKey: variables.idempotencyKey,
        }),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionsRootKey(scope),
      ),
  } as const;
}

export function putWorkflowDraftGrantIntentMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      workflowId,
      "put-draft-grant-intent",
    ),
    mutationFn: (variables: PutWorkflowDraftGrantIntentVariables) =>
      runScopedMutation(access, (signal) =>
        transport.putDraftGrantIntent(
          scope,
          workflowId,
          variables.slotId,
          variables.body,
          { signal, idempotencyKey: variables.idempotencyKey },
        ),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionRootKey(scope, workflowId),
      ),
  } as const;
}

export function deleteWorkflowDraftGrantIntentMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      workflowId,
      "delete-draft-grant-intent",
    ),
    mutationFn: (variables: DeleteWorkflowDraftGrantIntentVariables) =>
      runScopedMutation(access, (signal) =>
        transport.deleteDraftGrantIntent(scope, workflowId, variables.slotId, {
          signal,
          idempotencyKey: variables.idempotencyKey,
        }),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionRootKey(scope, workflowId),
      ),
  } as const;
}

export function putWorkflowVersionGrantMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string,
  versionId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      workflowId,
      "put-version-grant",
    ),
    mutationFn: (variables: PutWorkflowVersionGrantVariables) =>
      runScopedMutation(access, (signal) =>
        transport.putVersionGrant(
          scope,
          workflowId,
          versionId,
          variables.slotId,
          variables.body,
          { signal, idempotencyKey: variables.idempotencyKey },
        ),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionRootKey(scope, workflowId),
      ),
  } as const;
}

export function revokeWorkflowVersionGrantMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  workflowId: string,
  versionId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const scope = access.scope;
  return {
    mutationKey: workflowDefinitionMutationKey(
      scope,
      workflowId,
      "revoke-version-grant",
    ),
    mutationFn: (variables: RevokeWorkflowVersionGrantVariables) =>
      runScopedMutation(access, (signal) =>
        transport.revokeVersionGrant(
          scope,
          workflowId,
          versionId,
          variables.slotId,
          { signal, idempotencyKey: variables.idempotencyKey },
        ),
      ),
    onSuccess: () =>
      invalidateWhenActive(
        queryClient,
        access,
        workflowDefinitionRootKey(scope, workflowId),
      ),
  } as const;
}

export function useWorkflowDefinitions(
  query: WorkflowDefinitionListInput = {},
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  return useInfiniteQuery(
    workflowDefinitionsQueryOptions(access, query, enabled, transport),
  );
}

export function useWorkflowDefinition(
  workflowId: string | null | undefined,
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  return useQuery(
    workflowDefinitionQueryOptions(access, workflowId, enabled, transport),
  );
}

export function useWorkflowDraft(
  workflowId: string | null | undefined,
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  return useQuery(
    workflowDraftQueryOptions(access, workflowId, enabled, transport),
  );
}

export function useWorkflowVersions(
  workflowId: string | null | undefined,
  query: WorkflowVersionListInput = {},
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  return useInfiniteQuery(
    workflowVersionsQueryOptions(access, workflowId, query, enabled, transport),
  );
}

export function useWorkflowVersion(
  workflowId: string | null | undefined,
  versionId: string | null | undefined,
  enabled = true,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  return useQuery(
    workflowVersionQueryOptions(
      access,
      workflowId,
      versionId,
      enabled,
      transport,
    ),
  );
}

export function useCreateWorkflowDefinition(
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    createWorkflowDefinitionMutationOptions(queryClient, access, transport),
  );
}

export function useArchiveWorkflowDefinition(
  workflowId: string | null | undefined,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    archiveWorkflowDefinitionMutationOptions(
      queryClient,
      access,
      workflowId,
      transport,
    ),
  );
}

export function useSaveWorkflowDraft(
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    saveWorkflowDraftMutationOptions(
      queryClient,
      access,
      workflowId,
      transport,
    ),
  );
}

export function useValidateWorkflowDraft(
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  return useMutation(
    validateWorkflowDraftMutationOptions(access, workflowId, transport),
  );
}

export function usePublishWorkflowDraft(
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    publishWorkflowDraftMutationOptions(
      queryClient,
      access,
      workflowId,
      transport,
    ),
  );
}

export function usePutWorkflowDraftGrantIntent(
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    putWorkflowDraftGrantIntentMutationOptions(
      queryClient,
      access,
      workflowId,
      transport,
    ),
  );
}

export function useDeleteWorkflowDraftGrantIntent(
  workflowId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    deleteWorkflowDraftGrantIntentMutationOptions(
      queryClient,
      access,
      workflowId,
      transport,
    ),
  );
}

export function usePutWorkflowVersionGrant(
  workflowId: string,
  versionId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    putWorkflowVersionGrantMutationOptions(
      queryClient,
      access,
      workflowId,
      versionId,
      transport,
    ),
  );
}

export function useRevokeWorkflowVersionGrant(
  workflowId: string,
  versionId: string,
  transport: WorkflowDefinitionTransport = createWorkflowDefinitionTransport(),
) {
  const access = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  return useMutation(
    revokeWorkflowVersionGrantMutationOptions(
      queryClient,
      access,
      workflowId,
      versionId,
      transport,
    ),
  );
}
