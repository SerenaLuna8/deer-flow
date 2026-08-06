"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

import {
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
} from "@/core/private-work/types";

import {
  createProjectChannelGroupBindingChallenge,
  deleteProjectChannelGroupBinding,
  listProjectChannelGroupBindings,
  updateProjectChannelGroupBinding,
} from "./api";
import { projectChannelGroupBindingsQueryKey } from "./query-keys";
import type {
  CreateProjectChannelGroupBindingChallengeInput,
  UpdateProjectChannelGroupBindingInput,
} from "./types";

export function useProjectChannelGroupBindings(
  access: PrivateWorkAccess,
  enabled = true,
) {
  const queryKey = useMemo(
    () => projectChannelGroupBindingsQueryKey(access.scope),
    [access.scope],
  );
  return useQuery({
    queryKey,
    queryFn: ({ signal }) => listProjectChannelGroupBindings(access, signal),
    enabled,
  });
}

export function useProjectChannelGroupBindingActions(
  access: PrivateWorkAccess,
) {
  const queryClient = useQueryClient();
  const queryKey = useMemo(
    () => projectChannelGroupBindingsQueryKey(access.scope),
    [access.scope],
  );

  const refresh = useCallback(async () => {
    const response = await runPrivateWorkAbortable(access, (signal) =>
      listProjectChannelGroupBindings(access, signal),
    );
    queryClient.setQueryData(queryKey, response);
    return response;
  }, [access, queryClient, queryKey]);

  const createChallenge = useCallback(
    (input: CreateProjectChannelGroupBindingChallengeInput) =>
      runPrivateWorkAbortable(access, (signal) =>
        createProjectChannelGroupBindingChallenge(access, input, signal),
      ),
    [access],
  );

  const update = useCallback(
    async (bindingId: string, input: UpdateProjectChannelGroupBindingInput) => {
      const binding = await runPrivateWorkAbortable(access, (signal) =>
        updateProjectChannelGroupBinding(access, bindingId, input, signal),
      );
      await queryClient.invalidateQueries({ queryKey });
      return binding;
    },
    [access, queryClient, queryKey],
  );

  const remove = useCallback(
    async (bindingId: string, expectedRevision: number) => {
      await runPrivateWorkAbortable(access, (signal) =>
        deleteProjectChannelGroupBinding(
          access,
          bindingId,
          expectedRevision,
          signal,
        ),
      );
      await queryClient.invalidateQueries({ queryKey });
    },
    [access, queryClient, queryKey],
  );

  return { createChallenge, refresh, remove, update };
}
