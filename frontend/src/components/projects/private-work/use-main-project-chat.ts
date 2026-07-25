"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";
import {
  enableProjectSystemBinding,
  invalidateProjectAssetQueries,
  listProjectAssets,
  listProjectAssetVersions,
  useProjectAssets,
  type ProjectAssetList,
} from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";

import {
  createProjectChatForAgent,
  ensureMainSystemAgentBindings,
  mainProjectAgent,
} from "./agent-selector-dialog";

export function useMainProjectChat(project: Project) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const assets = useProjectAssets(
    privateWork.scope.accountId,
    project.id,
    "agents",
  );
  const mainAgent = useMemo(
    () => mainProjectAgent(assets.data as ProjectAssetList | undefined),
    [assets.data],
  );
  const [isCreating, setIsCreating] = useState(false);

  const startMainChat = useCallback(async () => {
    if (isCreating || assets.isLoading) return;
    if (assets.error) {
      toast.error("无法加载 Main 智能体，请稍后重试");
      return;
    }
    if (!mainAgent) {
      toast.error("Main 智能体尚未在当前项目启用");
      return;
    }

    setIsCreating(true);
    try {
      if (mainAgent.binding?.enabled !== true) {
        const [history, skillCatalog, mcpCatalog] = await Promise.all([
          listProjectAssetVersions(project.id, "agents", mainAgent.id),
          listProjectAssets(project.id, "skills"),
          listProjectAssets(project.id, "mcp-servers"),
        ]);
        const currentVersion = history.data.find(
          (version) =>
            "agent_id" in version &&
            version.id === mainAgent.current_published_version_id,
        );
        if (!currentVersion || !("agent_id" in currentVersion)) {
          throw new Error("Main 智能体当前版本不可用");
        }
        await ensureMainSystemAgentBindings({
          agent: mainAgent,
          requiredSkillVersionIds: currentVersion.skill_version_ids,
          requiredMcpVersionIds: currentVersion.mcp_version_ids,
          skillCatalog,
          mcpCatalog,
          enableBinding: (kind, input) =>
            enableProjectSystemBinding(project.id, kind, input),
        });
        await Promise.all([
          invalidateProjectAssetQueries(
            queryClient,
            privateWork.scope.accountId,
            project.id,
            "agents",
          ),
          invalidateProjectAssetQueries(
            queryClient,
            privateWork.scope.accountId,
            project.id,
            "skills",
          ),
          invalidateProjectAssetQueries(
            queryClient,
            privateWork.scope.accountId,
            project.id,
            "mcp-servers",
          ),
        ]);
      }
      await createProjectChatForAgent({
        scope: privateWork.scope,
        projectSlug: project.slug,
        agent: mainAgent,
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
      toast.error(error instanceof Error ? error.message : "无法创建项目对话");
    } finally {
      setIsCreating(false);
    }
  }, [
    assets.error,
    assets.isLoading,
    isCreating,
    mainAgent,
    privateWork.scope,
    project.id,
    project.slug,
    queryClient,
    router,
  ]);

  return {
    startMainChat,
    isCreating,
    isLoading: assets.isLoading,
  };
}
