"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  createProjectThread,
  type CreateProjectThreadInput,
} from "@/core/private-work/api-client";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { ProjectClientScope } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";
import {
  useProjectAssets,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";
import { uuid } from "@/core/utils/uuid";

export type ExecutableProjectAgent = ProjectAssetItem;

export function executableProjectAgents(
  catalog: ProjectAssetList | undefined,
): ExecutableProjectAgent[] {
  if (!catalog) return [];
  const executable = (item: ProjectAssetItem) =>
    item.status === "active" &&
    item.capabilities.includes("shared_assets.execute");
  return [
    ...catalog.project_items.filter(
      (item) => executable(item) && item.current_published_version_id !== null,
    ),
    ...catalog.system_items.filter(
      (item) => executable(item) && item.binding?.enabled === true,
    ),
  ];
}

export function projectThreadAgentSelection(agent: ExecutableProjectAgent) {
  return {
    agentAssetId: agent.id,
    agentScope: agent.scope,
  } satisfies Pick<CreateProjectThreadInput, "agentAssetId" | "agentScope">;
}

type CreateProjectChatDependencies = {
  scope: ProjectClientScope;
  projectSlug: string;
  agent: ExecutableProjectAgent;
  createThreadId?: () => string;
  createThread?: (
    scope: ProjectClientScope,
    input: CreateProjectThreadInput,
  ) => Promise<unknown>;
  navigate: (path: string) => void;
};

export async function createProjectChatForAgent({
  scope,
  projectSlug,
  agent,
  createThreadId = uuid,
  createThread = createProjectThread,
  navigate,
}: CreateProjectChatDependencies): Promise<string> {
  const threadId = createThreadId();
  await createThread(scope, {
    threadId,
    ...projectThreadAgentSelection(agent),
  });
  navigate(
    `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(threadId)}`,
  );
  return threadId;
}

export function AgentSelectorDialog({
  open,
  agents,
  isCreating,
  isLoading = false,
  error = null,
  onOpenChange,
  onSelect,
}: {
  open: boolean;
  agents: ExecutableProjectAgent[];
  isCreating: boolean;
  isLoading?: boolean;
  error?: Error | null;
  onOpenChange: (open: boolean) => void;
  onSelect: (agent: ExecutableProjectAgent) => void;
}) {
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="project-agent-selector-title"
      className="bg-background fixed inset-0 z-50 m-auto h-fit max-h-[80vh] w-[min(32rem,calc(100%-2rem))] overflow-y-auto rounded-2xl border p-6 shadow-xl"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2
            id="project-agent-selector-title"
            className="text-lg font-semibold"
          >
            选择 Agent
          </h2>
          <p className="text-muted-foreground mt-1 text-sm">
            对话保存 logical Agent，运行时由服务端复核可执行版本。
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => onOpenChange(false)}
        >
          关闭
        </Button>
      </div>
      {isLoading ? (
        <p role="status" className="text-muted-foreground mt-6 text-sm">
          正在加载 Agent…
        </p>
      ) : error ? (
        <p role="alert" className="text-destructive mt-6 text-sm">
          无法加载 Agent，请稍后重试。
        </p>
      ) : (
        <div className="mt-6 space-y-2">
          {agents.map((agent) => (
            <Button
              key={`${agent.scope}:${agent.id}`}
              type="button"
              variant="outline"
              className="h-auto w-full justify-between px-4 py-3"
              disabled={isCreating}
              onClick={() => onSelect(agent)}
            >
              <span className="truncate">{agent.display_name}</span>
              <span className="text-muted-foreground text-xs">
                {agent.scope === "project" ? "项目 Agent" : "系统 Agent"}
              </span>
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

export function ProjectAgentSelectorDialog({
  project,
  open,
  onOpenChange,
}: {
  project: Project;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const { user } = useAuth();
  const privateWork = usePrivateWorkAccess();
  const assets = useProjectAssets(
    user?.id ?? "",
    project.id,
    "agents",
    open && Boolean(user),
  );
  const agents = useMemo(
    () => executableProjectAgents(assets.data as ProjectAssetList | undefined),
    [assets.data],
  );
  const [isCreating, setIsCreating] = useState(false);

  useEffect(() => {
    if (open && assets.isSuccess && agents.length === 0) {
      onOpenChange(false);
      router.push(`/projects/${encodeURIComponent(project.slug)}/agents`);
    }
  }, [
    agents.length,
    assets.isSuccess,
    onOpenChange,
    open,
    project.slug,
    router,
  ]);

  const handleSelect = async (agent: ExecutableProjectAgent) => {
    if (!privateWork.scope || isCreating) return;
    setIsCreating(true);
    try {
      await createProjectChatForAgent({
        scope: privateWork.scope,
        projectSlug: project.slug,
        agent,
        navigate: (path) => router.push(path),
      });
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法创建项目对话");
    } finally {
      setIsCreating(false);
    }
  };

  return (
    <AgentSelectorDialog
      open={open}
      agents={agents}
      isCreating={isCreating}
      isLoading={assets.isLoading}
      error={assets.error}
      onOpenChange={onOpenChange}
      onSelect={(agent) => void handleSelect(agent)}
    />
  );
}
