"use client";

import type { Thread } from "@langchain/langgraph-sdk";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  ScopedChatPage,
  type ScopedChatRouteScope,
} from "@/components/workspace/project-chat";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";
import { useProjectAutomationReadiness } from "@/core/project-automations/readiness";
import {
  PROJECT_AUTOMATION,
  projectAutomationEntryEnabled,
} from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export type ProjectChatRouteScope = Omit<ScopedChatRouteScope, "privateWork">;

export function projectChatRouteScope(
  project: Project,
  automationReady = false,
  automationFeatureEnabled: boolean = PROJECT_AUTOMATION,
  staticWebsiteOnly = false,
): ProjectChatRouteScope {
  const base = `/projects/${encodeURIComponent(project.slug)}/chats`;
  const canCreate = project.capabilities.includes("private_work.create");
  const canRun =
    canCreate && project.capabilities.includes("shared_assets.execute");
  const canRead = project.capabilities.includes("private_work.read_own");
  return {
    threadBasePath: base,
    newThreadPath: base,
    canCreate,
    canRun,
    canUpload: canRun,
    canDelete: canRead,
    automationVisible: projectAutomationEntryEnabled(
      automationFeatureEnabled,
      staticWebsiteOnly,
      canRead,
      automationReady ? "ready" : undefined,
    ),
    automationHref: (threadId) =>
      `/projects/${encodeURIComponent(project.slug)}/automations?thread_id=${encodeURIComponent(threadId)}`,
    goalVisible: canRead,
    compactVisible: canRun,
    branchVisible: canCreate,
    regenerateVisible: canRun,
    sidecarVisible: canRun,
    artifactsVisible: canRead,
    followupSuggestionsEnabled: canRun,
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
  const privateWork: ProjectPrivateWorkScope = useProjectPrivateWorkScope();
  const staticWebsiteOnly = isStaticWebsiteOnly();
  const canReadPrivateWork = project.capabilities.includes(
    "private_work.read_own",
  );
  const automationReadiness = useProjectAutomationReadiness(
    PROJECT_AUTOMATION && canReadPrivateWork && !staticWebsiteOnly,
  );
  const automationReady = Boolean(
    automationReadiness.data?.status === "ready" &&
    automationReadiness.data.project_private_work_ready &&
    automationReadiness.data.schema_ready,
  );
  const scope = useMemo(
    () => ({
      ...projectChatRouteScope(
        project,
        automationReady,
        PROJECT_AUTOMATION,
        staticWebsiteOnly,
      ),
      privateWork,
    }),
    [automationReady, privateWork, project, staticWebsiteOnly],
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
