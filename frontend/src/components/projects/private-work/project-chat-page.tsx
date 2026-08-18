"use client";

import { useCallback, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  ScopedChatPage,
  type ScopedChatRouteScope,
} from "@/components/workspace/project-chat";
import {
  resolveThreadAgentIdentity,
  resolveThreadAgentSelection,
  ThreadAgentIndicator,
} from "@/components/workspace/thread-agent-indicator";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";
import { useProjectAssets, type ProjectAssetList } from "@/core/shared-assets";
import type { AgentThread } from "@/core/threads";

import {
  ProjectAgentSelectorDialog,
  type ProjectThreadAgentSelection,
} from "./agent-selector-dialog";

export type ProjectChatRouteScope = Omit<ScopedChatRouteScope, "privateWork">;

export function projectChatRouteScope(
  project: Pick<Project, "slug" | "capabilities">,
): ProjectChatRouteScope {
  const base = `/projects/${encodeURIComponent(project.slug)}/chats`;
  const canCreate = project.capabilities.includes("private_work.create");
  const canRun =
    canCreate && project.capabilities.includes("shared_assets.execute");
  const canRead = project.capabilities.includes("private_work.read_own");
  return {
    threadBasePath: base,
    threadListPath: base,
    canCreate,
    canRun,
    canApproveHostExecution: project.capabilities.includes(
      "private_work.approve_host_execution",
    ),
    canUpload: canRun,
    canDelete: canCreate,
    canDeleteFiles: canRead,
    goalVisible: canRead,
    compactVisible: canRun,
    branchVisible: canCreate,
    regenerateVisible: canRun,
    sidecarVisible: canRun,
    artifactsVisible: canRead,
    followupSuggestionsEnabled: canRun,
  };
}

export function ProjectChatNotFound({ chatsPath }: { chatsPath: string }) {
  return (
    <main className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
      <h1 className="text-2xl font-semibold">找不到这个对话</h1>
      <p className="text-muted-foreground mt-3 text-sm">
        该对话不存在，或你没有访问权限。
      </p>
      <Button asChild className="mt-6">
        <a href={chatsPath}>返回对话列表</a>
      </Button>
    </main>
  );
}

export function ProjectChatPage({ project }: { project: Project }) {
  const privateWork: ProjectPrivateWorkScope = useProjectPrivateWorkScope();
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false);
  const [currentAgent, setCurrentAgent] =
    useState<ProjectThreadAgentSelection | null>(null);
  const canReadPrivateWork = project.capabilities.includes(
    "private_work.read_own",
  );
  const canStartNewChat =
    project.capabilities.includes("private_work.create") &&
    project.capabilities.includes("shared_assets.execute");
  const agents = useProjectAssets(
    privateWork.scope.accountId,
    project.id,
    "agents",
    canReadPrivateWork,
  );
  const agentCatalog = agents.data as ProjectAssetList | undefined;
  const agentCatalogSettled =
    agentCatalog !== undefined || (!agents.isLoading && !agents.isFetching);
  const renderHeaderAccessory = useCallback(
    (thread: AgentThread | null | undefined) => {
      const selection = resolveThreadAgentSelection(thread);
      return (
        <ThreadAgentIndicator
          identity={resolveThreadAgentIdentity(
            thread,
            agentCatalog,
            agentCatalogSettled,
          )}
          onStartNewChat={
            canStartNewChat
              ? () => {
                  setCurrentAgent(selection);
                  setAgentSelectorOpen(true);
                }
              : undefined
          }
        />
      );
    },
    [agentCatalog, agentCatalogSettled, canStartNewChat],
  );
  const scope = useMemo(
    () => ({
      ...projectChatRouteScope(project),
      privateWork,
    }),
    [privateWork, project],
  );
  return (
    <>
      <div className="h-[calc(100vh-3.5rem)] min-h-0 md:h-screen">
        <ScopedChatPage
          scope={scope}
          renderHeaderAccessory={renderHeaderAccessory}
          missingThreadFallback={
            <ProjectChatNotFound
              chatsPath={`/projects/${encodeURIComponent(project.slug)}/chats`}
            />
          }
        />
      </div>
      <ProjectAgentSelectorDialog
        project={project}
        open={agentSelectorOpen}
        currentAgent={currentAgent}
        onOpenChange={setAgentSelectorOpen}
      />
    </>
  );
}
