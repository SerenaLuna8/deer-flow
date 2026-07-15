"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import {
  MemorySettingsView,
  type MemorySettingsController,
} from "@/components/workspace/settings/memory-settings-page";
import type { MemoryFact } from "@/core/memory/types";
import {
  deleteProjectMemoryFact,
  exportProjectMemory,
  importProjectMemory,
  loadProjectMemory,
  projectMemoryPermissions,
  projectMemoryMutationKey,
  projectMemoryQueryKey,
  reloadProjectMemory,
  updateProjectMemoryFact,
  type ProjectMemorySnapshot,
} from "@/core/private-work/memory";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";

export function projectMemorySourceThreadHref(
  projectSlug: string,
  fact: MemoryFact,
) {
  const sourceThreadId = fact.sourceThreadId ?? fact.source;
  return `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(sourceThreadId)}`;
}

export function ProjectMemoryPage({ project }: { project: Project }) {
  const privateWork = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  const scope = privateWork.scope;
  const permissions = projectMemoryPermissions(project.capabilities);
  const generation = useMemo(
    () =>
      Symbol(
        `project-memory-generation:${scope?.accountId ?? "none"}:${scope?.projectId ?? "none"}`,
      ),
    [scope],
  );
  const currentGeneration = useRef(generation);
  currentGeneration.current = generation;
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, [scope?.accountId, scope?.projectId]);
  if (!scope) {
    throw new Error("Project Memory requires an entered project scope");
  }
  const queryKey = projectMemoryQueryKey(scope);
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => loadProjectMemory(privateWork, signal),
    enabled: permissions.canRead,
  });
  const setSnapshot = (snapshot: ProjectMemorySnapshot) => {
    if (
      !mounted.current ||
      currentGeneration.current !== generation ||
      !isPrivateWorkAccessActive(privateWork)
    ) {
      return;
    }
    queryClient.setQueryData(queryKey, snapshot);
  };
  const reloadMemory = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "reload"),
    mutationFn: () =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        reloadProjectMemory(privateWork, signal),
      ),
    onSuccess: setSnapshot,
  });
  const importMemory = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "import"),
    mutationFn: async (memory: NonNullable<typeof query.data>["memory"]) => {
      if (!query.data) throw new Error("Project Memory is unavailable");
      return runPrivateWorkAbortable(privateWork, (signal) =>
        importProjectMemory(privateWork, query.data.version, memory, signal),
      );
    },
    onSuccess: setSnapshot,
  });
  const updateMemoryFact = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "update-fact"),
    mutationFn: async ({
      factId,
      input,
    }: {
      factId: string;
      input: { content?: string; category?: string; confidence?: number };
    }) => {
      if (!query.data) throw new Error("Project Memory is unavailable");
      return runPrivateWorkAbortable(privateWork, (signal) =>
        updateProjectMemoryFact(
          privateWork,
          factId,
          query.data.version,
          input,
          signal,
        ),
      );
    },
    onSuccess: setSnapshot,
  });
  const deleteMemoryFact = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "delete-fact"),
    mutationFn: async (factId: string) => {
      if (!query.data) throw new Error("Project Memory is unavailable");
      return runPrivateWorkAbortable(privateWork, (signal) =>
        deleteProjectMemoryFact(
          privateWork,
          factId,
          query.data.version,
          signal,
        ),
      );
    },
    onSuccess: setSnapshot,
  });

  const controller: MemorySettingsController = {
    memory: query.data?.memory ?? null,
    isLoading: query.isLoading,
    error: query.error,
    exportMemory: () => exportProjectMemory(privateWork),
    reloadMemory: permissions.canReload ? reloadMemory : undefined,
    importMemory: permissions.canImport ? importMemory : undefined,
    updateMemoryFact: permissions.canModify ? updateMemoryFact : undefined,
    deleteMemoryFact: permissions.canDelete ? deleteMemoryFact : undefined,
  };

  return (
    <div className="mx-auto w-full max-w-6xl p-6 lg:p-8">
      <MemorySettingsView
        sourceThreadHref={(fact) =>
          projectMemorySourceThreadHref(project.slug, fact)
        }
        controller={controller}
        permissions={{
          canAdd: false,
          canClear: false,
          canDelete: permissions.canDelete,
          canExport: permissions.canExport,
          canImport: permissions.canImport,
          canModify: permissions.canModify,
          canReload: permissions.canReload,
        }}
      />
    </div>
  );
}
