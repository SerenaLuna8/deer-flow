"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  MemorySettingsView,
  type MemorySettingsController,
} from "@/components/workspace/settings/memory-settings-page";
import {
  deleteProjectMemoryFact,
  exportProjectMemory,
  importProjectMemory,
  loadProjectMemory,
  projectMemoryPermissions,
  projectMemoryQueryKey,
  reloadProjectMemory,
  updateProjectMemoryFact,
  type ProjectMemorySnapshot,
} from "@/core/private-work/memory";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";

export function ProjectMemoryPage({ project }: { project: Project }) {
  const privateWork = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  const scope = privateWork.scope;
  const permissions = projectMemoryPermissions(project.capabilities);
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
    queryClient.setQueryData(queryKey, snapshot);
  };
  const reloadMemory = useMutation({
    mutationFn: () => reloadProjectMemory(privateWork),
    onSuccess: setSnapshot,
  });
  const importMemory = useMutation({
    mutationFn: async (memory: NonNullable<typeof query.data>["memory"]) => {
      if (!query.data) throw new Error("Project Memory is unavailable");
      return importProjectMemory(privateWork, query.data.version, memory);
    },
    onSuccess: setSnapshot,
  });
  const updateMemoryFact = useMutation({
    mutationFn: async ({
      factId,
      input,
    }: {
      factId: string;
      input: { content?: string; category?: string; confidence?: number };
    }) => {
      if (!query.data) throw new Error("Project Memory is unavailable");
      return updateProjectMemoryFact(
        privateWork,
        factId,
        query.data.version,
        input,
      );
    },
    onSuccess: setSnapshot,
  });
  const deleteMemoryFact = useMutation({
    mutationFn: async (factId: string) => {
      if (!query.data) throw new Error("Project Memory is unavailable");
      return deleteProjectMemoryFact(privateWork, factId, query.data.version);
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
