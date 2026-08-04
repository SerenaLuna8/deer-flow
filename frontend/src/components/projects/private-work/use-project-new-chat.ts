"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import { useAgentMcpDependencyRuntime } from "@/components/projects/assets/use-mcp-dependency-runtime";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";
import {
  useProjectAssets,
  useProjectDefaultAgent,
  type ProjectAssetList,
} from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";

import {
  createProjectChatWithDefaultAgent,
  resolveProjectDefaultAgent,
} from "./agent-selector-dialog";
import { projectNewChatErrorMessage } from "./project-new-chat-error";

export function useProjectNewChat(project: Project) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const assets = useProjectAssets(
    privateWork.scope.accountId,
    project.id,
    "agents",
  );
  const defaultAgent = useProjectDefaultAgent(
    privateWork.scope.accountId,
    project.id,
  );
  const refetchDefaultAgent = defaultAgent.refetch;
  const resolution = useMemo(
    () =>
      resolveProjectDefaultAgent(
        assets.data as ProjectAssetList | undefined,
        defaultAgent.data,
      ),
    [assets.data, defaultAgent.data],
  );
  const customAgent =
    resolution.status === "ready" && resolution.source === "project"
      ? resolution.agent
      : null;
  const customAgentDependencies = useAgentMcpDependencyRuntime({
    accountId: privateWork.scope.accountId,
    projectId: project.id,
    agents: customAgent ? [customAgent] : [],
    enabled: Boolean(customAgent),
  });
  const [isCreating, setIsCreating] = useState(false);
  const isLoading =
    assets.isLoading ||
    defaultAgent.isLoading ||
    customAgentDependencies.isLoading;
  const customAgentAssessment = customAgentDependencies.assessments[0];

  const startNewChat = useCallback(async () => {
    if (isCreating || isLoading) return;
    if (assets.error || defaultAgent.error) {
      toast.error("无法加载项目默认 Agent，请稍后重试");
      return;
    }
    if (resolution.status === "unavailable") {
      toast.error(resolution.reason);
      return;
    }
    if (
      resolution.source === "project" &&
      customAgentAssessment?.status !== "ready"
    ) {
      toast.error(
        customAgentAssessment?.reason ??
          "无法验证项目默认 Agent 的运行依赖，请稍后重试。",
      );
      return;
    }

    setIsCreating(true);
    try {
      await createProjectChatWithDefaultAgent({
        scope: privateWork.scope,
        projectSlug: project.slug,
        invalidateThreadLists: () =>
          invalidateStoppedThreadCaches(
            queryClient,
            null,
            false,
            privateWork.scope,
          ),
        navigate: (path) => router.push(path),
      });
    } catch (error) {
      toast.error(
        await projectNewChatErrorMessage(
          error,
          () => refetchDefaultAgent(),
          "无法创建项目对话",
        ),
      );
    } finally {
      setIsCreating(false);
    }
  }, [
    assets.error,
    customAgentAssessment,
    defaultAgent.error,
    isCreating,
    isLoading,
    privateWork.scope,
    project.slug,
    queryClient,
    refetchDefaultAgent,
    resolution,
    router,
  ]);

  return {
    startNewChat,
    isCreating,
    isLoading,
    defaultAgentName:
      resolution.status === "ready" ? resolution.agent.display_name : null,
    unavailableReason:
      resolution.status === "unavailable"
        ? resolution.reason
        : customAgentAssessment?.status === "blocked"
          ? customAgentAssessment.reason
          : null,
  };
}
