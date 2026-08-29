"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useImperativeRequest } from "@/core/api/use-imperative-request";

import {
  createAdminKnowledgeModel,
  deleteAdminKnowledgeModel,
  listAdminKnowledgeModels,
  testAdminKnowledgeModel,
  updateAdminKnowledgeModel,
} from "./api";
import {
  adminKnowledgeModelsQueryKey,
  adminKnowledgeModelsRoot,
} from "./query-keys";
import {
  adminKnowledgeAccountIdSchema,
  type CreateAdminKnowledgeModelInput,
  type UpdateAdminKnowledgeModelInput,
} from "./types";

export function useAdminKnowledgeModels(accountId: string, enabled = true) {
  const parsed = adminKnowledgeAccountIdSchema.parse(accountId);
  return useQuery({
    queryKey: adminKnowledgeModelsQueryKey(parsed, "list"),
    queryFn: ({ signal }) => listAdminKnowledgeModels(parsed, signal),
    // Admin queries mount only after the authenticated admin state resolves.
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

function useInvalidateAdminKnowledgeModels(accountId: string) {
  const queryClient = useQueryClient();
  return () =>
    queryClient.invalidateQueries({
      queryKey: adminKnowledgeModelsRoot(accountId),
    });
}

/** Secret-bearing create: imperative so the API key never enters query state. */
export function useCreateAdminKnowledgeModel(accountId: string) {
  const parsed = adminKnowledgeAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminKnowledgeModels(parsed);
  return useImperativeRequest(async (input: CreateAdminKnowledgeModelInput) => {
    const result = await createAdminKnowledgeModel(parsed, input);
    await invalidate();
    return result;
  });
}

/** Secret-capable update: imperative because it may carry a replacement key. */
export function useUpdateAdminKnowledgeModel(accountId: string) {
  const parsed = adminKnowledgeAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminKnowledgeModels(parsed);
  return useImperativeRequest(
    async ({
      configurationId,
      input,
    }: {
      configurationId: string;
      input: UpdateAdminKnowledgeModelInput;
    }) => {
      const result = await updateAdminKnowledgeModel(
        parsed,
        configurationId,
        input,
      );
      await invalidate();
      return result;
    },
  );
}

export function useDeleteAdminKnowledgeModel(accountId: string) {
  const parsed = adminKnowledgeAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminKnowledgeModels(parsed);
  return useMutation({
    mutationFn: (configurationId: string) =>
      deleteAdminKnowledgeModel(parsed, configurationId),
    onSuccess: () => invalidate(),
  });
}

export function useTestAdminKnowledgeModel(accountId: string) {
  const parsed = adminKnowledgeAccountIdSchema.parse(accountId);
  return useMutation({
    mutationFn: (configurationId: string) =>
      testAdminKnowledgeModel(parsed, configurationId),
  });
}
