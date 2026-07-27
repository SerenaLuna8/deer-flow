"use client";

import { useQueryClient } from "@tanstack/react-query";
import { BotIcon, MessageSquareIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import {
  createProjectChatForAgent,
  type ExecutableProjectAgent,
} from "@/components/projects/private-work/agent-selector-dialog";
import { ProjectAgentStartContinuation } from "@/components/projects/private-work/project-agent-start-continuation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
} from "@/components/ui/card";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Capability, Project } from "@/core/projects/types";
import type { ProjectAssetItem } from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";

import { AgentAssetDetail } from "./agent-asset-detail";
import { ProjectAssetPageShell } from "./project-asset-page-shell";
import { useAgentMcpDependencyRuntime } from "./use-mcp-dependency-runtime";

export type ProjectAgentChatAvailability = {
  enabled: boolean;
  reason: string | null;
};

export function projectAgentChatAvailability(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
  mcpDependencyReason: string | null = null,
): ProjectAgentChatAvailability {
  if (
    !projectCapabilities.includes("private_work.create") ||
    !projectCapabilities.includes("shared_assets.execute")
  ) {
    return { enabled: false, reason: "当前账号没有创建 Agent 对话的权限" };
  }
  if (item.status !== "active") {
    return { enabled: false, reason: "该 Agent 当前不可用" };
  }
  if (!item.capabilities.includes("shared_assets.execute")) {
    return { enabled: false, reason: "当前账号没有执行该 Agent 的权限" };
  }
  if (!item.current_published_version_id) {
    return { enabled: false, reason: "请先完成 Agent 配置并发布" };
  }
  if (item.scope === "system" && item.binding?.enabled !== true) {
    return { enabled: false, reason: "请先在详情中启用该 System Agent" };
  }
  if (mcpDependencyReason) {
    return { enabled: false, reason: mcpDependencyReason };
  }
  return { enabled: true, reason: null };
}

export function ProjectAgentCardGridView({
  items,
  projectCapabilities,
  selectedAssetId,
  creatingChatForAgentId,
  mcpDependencyReasons = new Map(),
  mcpDependencyError = false,
  onSelect,
  onStartChat,
}: {
  items: ProjectAssetItem[];
  projectCapabilities: readonly Capability[];
  selectedAssetId: string | null;
  creatingChatForAgentId: string | null;
  mcpDependencyReasons?: ReadonlyMap<string, string>;
  mcpDependencyError?: boolean;
  onSelect: (item: ProjectAssetItem) => void;
  onStartChat: (item: ExecutableProjectAgent) => void;
}) {
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm">
        暂无项目自建的 Agent。
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {mcpDependencyError ? (
        <p role="alert" className="text-destructive text-sm">
          无法验证 Agent 的 MCP 依赖，请稍后重试。
        </p>
      ) : null}
      <div role="list" className="grid gap-4 sm:grid-cols-2 lg:gap-5">
        {items.map((item) => {
          const availability = projectAgentChatAvailability(
            item,
            projectCapabilities,
            mcpDependencyReasons.get(item.id) ?? null,
          );
          const creating = creatingChatForAgentId === item.id;
          const description = item.description?.trim();
          return (
            <Card
              key={item.id}
              role="listitem"
              className={`group min-h-64 gap-0 overflow-hidden py-0 transition-[border-color,box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:shadow-md ${
                selectedAssetId === item.id
                  ? "border-selection ring-selection/15 ring-2"
                  : "hover:border-foreground/20"
              }`}
            >
              <button
                type="button"
                aria-haspopup="dialog"
                aria-label={`查看 ${item.display_name} 详情`}
                className="focus-visible:ring-ring flex min-h-0 flex-1 flex-col text-left focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
                onClick={() => onSelect(item)}
              >
                <CardHeader className="w-full gap-0 px-6 pt-6">
                  <span className="flex min-w-0 items-center gap-3">
                    <span className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-xl">
                      <BotIcon aria-hidden className="size-5" />
                    </span>
                    <span className="min-w-0 flex-1 truncate text-base font-semibold">
                      {item.display_name}
                    </span>
                    {item.status !== "active" ? (
                      <AssetStatusBadge status={item.status} />
                    ) : null}
                  </span>
                </CardHeader>
                <CardContent className="w-full flex-1 px-6 pt-5 pb-6">
                  <span className="text-muted-foreground line-clamp-3 block text-sm leading-6">
                    {description && description.length > 0
                      ? description
                      : "暂无简介，可进入详情完善 Agent 设置。"}
                  </span>
                </CardContent>
              </button>
              <CardFooter className="gap-2 px-6 pb-6">
                <Button
                  type="button"
                  className="min-h-11 flex-1"
                  disabled={!availability.enabled || creating}
                  aria-label={
                    availability.reason
                      ? `与 ${item.display_name} 对话，${availability.reason}`
                      : `与 ${item.display_name} 对话`
                  }
                  title={availability.reason ?? undefined}
                  onClick={() => onStartChat(item)}
                >
                  <MessageSquareIcon aria-hidden className="size-4" />
                  {creating ? "正在创建…" : "对话"}
                </Button>
              </CardFooter>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

function ProjectAgentCardGrid({
  project,
  items,
  selectedAssetId,
  onSelect,
}: {
  project: Project;
  items: ProjectAssetItem[];
  selectedAssetId: string | null;
  onSelect: (item: ProjectAssetItem) => void;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const privateWork = usePrivateWorkAccess();
  const [creatingChatForAgentId, setCreatingChatForAgentId] = useState<
    string | null
  >(null);
  const mcpDependencyRuntime = useAgentMcpDependencyRuntime({
    accountId: privateWork.scope.accountId,
    projectId: project.id,
    agents: items,
    enabled:
      project.capabilities.includes("private_work.create") &&
      project.capabilities.includes("shared_assets.execute"),
  });
  const mcpDependencyReasons = new Map(
    mcpDependencyRuntime.assessments.flatMap((assessment, index) => {
      const item = items[index];
      return item && assessment.status !== "ready" && assessment.reason
        ? [[item.id, assessment.reason] as const]
        : [];
    }),
  );

  async function startChat(agent: ExecutableProjectAgent) {
    const availability = projectAgentChatAvailability(
      agent,
      project.capabilities,
      mcpDependencyReasons.get(agent.id) ?? null,
    );
    if (!availability.enabled || creatingChatForAgentId) return;

    setCreatingChatForAgentId(agent.id);
    try {
      await createProjectChatForAgent({
        scope: privateWork.scope,
        projectSlug: project.slug,
        agent,
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
      setCreatingChatForAgentId(null);
    }
  }

  return (
    <ProjectAgentCardGridView
      items={items}
      projectCapabilities={project.capabilities}
      selectedAssetId={selectedAssetId}
      creatingChatForAgentId={creatingChatForAgentId}
      mcpDependencyReasons={mcpDependencyReasons}
      mcpDependencyError={Boolean(mcpDependencyRuntime.error)}
      onSelect={onSelect}
      onStartChat={(agent) => void startChat(agent)}
    />
  );
}

export function ProjectAgentsPage({
  startChatIntent = false,
  startChatIntentId = null,
}: {
  startChatIntent?: boolean;
  startChatIntentId?: string | null;
}) {
  return (
    <ProjectAssetPageShell
      kind="agents"
      title="Agent"
      description="创建和维护当前项目自建 Agent 的角色设定、依赖与版本。系统默认 Main 不在此列表中展示。"
      renderLead={({ project, data }) => (
        <ProjectAgentStartContinuation
          project={project}
          catalog={data}
          requested={startChatIntent}
          intentId={startChatIntentId}
        />
      )}
      renderList={({ project, items, selectedAssetId, onSelect }) => (
        <ProjectAgentCardGrid
          project={project}
          items={items}
          selectedAssetId={selectedAssetId}
          onSelect={onSelect}
        />
      )}
      renderVersion={(version) =>
        "agent_id" in version ? (
          <AgentAssetDetail version={version} />
        ) : (
          <p role="alert" className="text-destructive text-sm">
            Agent 版本数据无效。
          </p>
        )
      }
    />
  );
}
