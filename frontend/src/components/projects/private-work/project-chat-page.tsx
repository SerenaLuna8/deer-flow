"use client";

import type { Thread } from "@langchain/langgraph-sdk";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  ScopedChatPage,
  type ScopedChatRouteScope,
} from "@/components/workspace/chats/scoped-chat-page";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";

export type ProjectChatRouteScope = Omit<ScopedChatRouteScope, "client">;

export function projectChatRouteScope(project: Project): ProjectChatRouteScope {
  const base = `/projects/${encodeURIComponent(project.slug)}/chats`;
  const canCreate = project.capabilities.includes("private_work.create");
  const canRun =
    canCreate && project.capabilities.includes("shared_assets.execute");
  return {
    threadBasePath: base,
    newThreadPath: base,
    canCreate,
    canRun,
    canUpload: canRun,
    canDelete: project.capabilities.includes("private_work.read_own"),
    scheduledTasksVisible: false,
    goalVisible: false,
    compactVisible: false,
    branchVisible: false,
    regenerateVisible: false,
    sidecarVisible: false,
    artifactsVisible: false,
    sidebarTriggerVisible: false,
    followupSuggestionsEnabled: false,
  };
}

export type ProjectThreadMetadataState = {
  data: Thread | null | undefined;
  error: Error | null;
  isLoading: boolean;
  isFetching: boolean;
};

export function projectThreadAvailability(
  state: ProjectThreadMetadataState,
): "loading" | "available" | "not-found" | "error" {
  if (state.isLoading || state.isFetching) return "loading";
  if (state.error) return "error";
  if (state.data === null) return "not-found";
  if (state.data === undefined) return "loading";
  return "available";
}

export function ProjectChatNotFound() {
  return (
    <main className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
      <h1 className="text-2xl font-semibold">找不到这个对话</h1>
      <p className="text-muted-foreground mt-3 text-sm">
        该对话不存在，或你没有访问权限。
      </p>
      <Button asChild className="mt-6">
        <a href="../">返回对话列表</a>
      </Button>
    </main>
  );
}

export function ProjectChatPage({ project }: { project: Project }) {
  const privateWork = usePrivateWorkAccess();
  const scope = useMemo(
    () => ({ ...projectChatRouteScope(project), client: privateWork.client }),
    [privateWork.client, project],
  );
  return (
    <div className="h-[calc(100vh-3.5rem)] min-h-0 md:h-screen">
      <ScopedChatPage
        scope={scope}
        missingThreadFallback={<ProjectChatNotFound />}
      />
    </div>
  );
}
