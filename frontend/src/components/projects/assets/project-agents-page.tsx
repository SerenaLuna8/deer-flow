"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  Loader2Icon,
  MessageSquareIcon,
  PowerIcon,
  RotateCcwIcon,
  StarIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AgentBuilderResumeBanner } from "@/components/projects/agents/agent-builder-resume-banner";
import {
  createProjectChatForAgent,
  type ExecutableProjectAgent,
} from "@/components/projects/private-work/agent-selector-dialog";
import { ProjectAgentStartContinuation } from "@/components/projects/private-work/project-agent-start-continuation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
} from "@/components/ui/card";
import {
  agentBuilderCanAuthor,
  useAgentBuilderSessions,
} from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Capability, Project } from "@/core/projects/types";
import {
  useChangeProjectAssetStatus,
  useProjectDefaultAgent,
  useSetProjectDefaultAgent,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";

import { AgentInstructionsWorkbench } from "./agent-instructions-workbench";
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
  if (mcpDependencyReason) {
    return { enabled: false, reason: mcpDependencyReason };
  }
  return { enabled: true, reason: null };
}

export function projectAgentCanActivate(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
): boolean {
  return (
    item.status === "suspended" &&
    projectCapabilities.includes("shared_assets.manage_bindings") &&
    item.capabilities.includes("shared_assets.manage_bindings") &&
    item.current_published_version_id !== null
  );
}

export function projectAgentDefaultAvailability(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
  mcpDependencyReason: string | null = null,
): ProjectAgentChatAvailability {
  if (
    !projectCapabilities.includes("shared_assets.manage_bindings") ||
    !item.capabilities.includes("shared_assets.manage_bindings")
  ) {
    return { enabled: false, reason: "仅项目管理员可以修改默认 Agent" };
  }
  if (item.scope !== "project" || item.status !== "active") {
    return { enabled: false, reason: "该 Agent 当前不可设为默认" };
  }
  if (!item.capabilities.includes("shared_assets.execute")) {
    return { enabled: false, reason: "当前账号没有执行该 Agent 的权限" };
  }
  if (!item.current_published_version_id) {
    return { enabled: false, reason: "请先完成 Agent 配置并发布" };
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
  activatingAgentId = null,
  defaultAgentId = null,
  settingDefaultAgentTarget = null,
  defaultAgentLoading = false,
  defaultAgentError = false,
  mcpDependencyReasons = new Map(),
  mcpDependencyError = false,
  onSelect,
  onStartChat,
  onActivate,
  onSetDefault,
  onRestoreMain,
}: {
  items: ProjectAssetItem[];
  projectCapabilities: readonly Capability[];
  selectedAssetId: string | null;
  creatingChatForAgentId: string | null;
  activatingAgentId?: string | null;
  defaultAgentId?: string | null;
  settingDefaultAgentTarget?: string | "main" | null;
  defaultAgentLoading?: boolean;
  defaultAgentError?: boolean;
  mcpDependencyReasons?: ReadonlyMap<string, string>;
  mcpDependencyError?: boolean;
  onSelect: (item: ProjectAssetItem) => void;
  onStartChat: (item: ExecutableProjectAgent) => void;
  onActivate?: (item: ProjectAssetItem) => void;
  onSetDefault?: (item: ProjectAssetItem) => void;
  onRestoreMain?: () => void;
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
      {defaultAgentError ? (
        <p role="alert" className="text-destructive text-sm">
          无法加载项目默认 Agent，请稍后重试。
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
          const activating = activatingAgentId === item.id;
          const canActivate =
            projectAgentCanActivate(item, projectCapabilities) &&
            Boolean(onActivate);
          const isDefault = defaultAgentId === item.id;
          const defaultAvailability = projectAgentDefaultAvailability(
            item,
            projectCapabilities,
            mcpDependencyReasons.get(item.id) ?? null,
          );
          const canManageDefault =
            projectCapabilities.includes("shared_assets.manage_bindings") &&
            item.capabilities.includes("shared_assets.manage_bindings") &&
            Boolean(onSetDefault && onRestoreMain);
          const defaultPending = settingDefaultAgentTarget !== null;
          const settingThisDefault = settingDefaultAgentTarget === item.id;
          const restoringMain =
            isDefault && settingDefaultAgentTarget === "main";
          const defaultActionReason = defaultAgentError
            ? "无法加载项目默认 Agent，请稍后重试"
            : defaultAgentLoading
              ? "正在加载项目默认 Agent"
              : isDefault
                ? null
                : defaultAvailability.reason;
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
                    {isDefault ? (
                      <Badge variant="secondary">
                        <StarIcon aria-hidden /> 默认
                      </Badge>
                    ) : null}
                    {item.status !== "active" ? (
                      <AssetStatusBadge
                        status={item.status}
                        label={
                          item.status === "suspended" ? "已停用" : undefined
                        }
                      />
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
              <CardFooter className="flex-wrap gap-2 px-6 pb-6">
                {canManageDefault ? (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-10 w-full"
                    disabled={
                      defaultPending ||
                      defaultAgentLoading ||
                      defaultAgentError ||
                      (!isDefault && !defaultAvailability.enabled)
                    }
                    aria-label={
                      defaultActionReason
                        ? `${isDefault ? "恢复 Main" : `将 ${item.display_name} 设为默认`}，${defaultActionReason}`
                        : isDefault
                          ? "恢复 Main"
                          : `将 ${item.display_name} 设为默认`
                    }
                    title={defaultActionReason ?? undefined}
                    onClick={() =>
                      isDefault ? onRestoreMain?.() : onSetDefault?.(item)
                    }
                  >
                    {settingThisDefault || restoringMain ? (
                      <Loader2Icon
                        aria-hidden
                        className="size-4 animate-spin"
                      />
                    ) : isDefault ? (
                      <RotateCcwIcon aria-hidden className="size-4" />
                    ) : (
                      <StarIcon aria-hidden className="size-4" />
                    )}
                    {restoringMain
                      ? "恢复中…"
                      : settingThisDefault
                        ? "设置中…"
                        : isDefault
                          ? "恢复 Main"
                          : "设为默认"}
                  </Button>
                ) : null}
                {canActivate ? (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={activating}
                    aria-label={`启用 ${item.display_name}`}
                    onClick={() => onActivate?.(item)}
                  >
                    {activating ? (
                      <Loader2Icon
                        aria-hidden
                        className="size-4 animate-spin"
                      />
                    ) : (
                      <PowerIcon aria-hidden className="size-4" />
                    )}
                    {activating ? "启用中…" : "启用"}
                  </Button>
                ) : null}
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
  const changeStatus = useChangeProjectAssetStatus(
    privateWork.scope.accountId,
    project.id,
    "agents",
  );
  const defaultAgent = useProjectDefaultAgent(
    privateWork.scope.accountId,
    project.id,
  );
  const setDefaultAgent = useSetProjectDefaultAgent(
    privateWork.scope.accountId,
    project.id,
  );
  const mcpDependencyRuntime = useAgentMcpDependencyRuntime({
    accountId: privateWork.scope.accountId,
    projectId: project.id,
    agents: items,
    enabled:
      (project.capabilities.includes("private_work.create") &&
        project.capabilities.includes("shared_assets.execute")) ||
      project.capabilities.includes("shared_assets.manage_bindings"),
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

  function activate(agent: ProjectAssetItem) {
    if (
      !projectAgentCanActivate(agent, project.capabilities) ||
      changeStatus.isPending
    ) {
      return;
    }
    changeStatus.mutate(
      {
        assetId: agent.id,
        action: "activate",
        input: { expected_asset_version: agent.version },
      },
      {
        onSuccess: () => toast.success(`${agent.display_name} 已启用`),
        onError: (error) => toast.error(adminAssetErrorMessage(error)),
      },
    );
  }

  function updateDefaultAgent(agent: ProjectAssetItem) {
    const availability = projectAgentDefaultAvailability(
      agent,
      project.capabilities,
      mcpDependencyReasons.get(agent.id) ?? null,
    );
    if (
      !availability.enabled ||
      !defaultAgent.data ||
      defaultAgent.error ||
      setDefaultAgent.isPending
    ) {
      return;
    }
    setDefaultAgent.mutate(
      {
        agent_asset_id: agent.id,
        expected_revision: defaultAgent.data.revision,
      },
      {
        onSuccess: () =>
          toast.success(`已将 ${agent.display_name} 设为项目默认 Agent`),
        onError: (error) => toast.error(adminAssetErrorMessage(error)),
      },
    );
  }

  function restoreMainDefault() {
    if (
      !project.capabilities.includes("shared_assets.manage_bindings") ||
      !defaultAgent.data ||
      defaultAgent.error ||
      setDefaultAgent.isPending
    ) {
      return;
    }
    setDefaultAgent.mutate(
      {
        agent_asset_id: null,
        expected_revision: defaultAgent.data.revision,
      },
      {
        onSuccess: () => toast.success("已恢复 Main 为项目默认 Agent"),
        onError: (error) => toast.error(adminAssetErrorMessage(error)),
      },
    );
  }

  return (
    <ProjectAgentCardGridView
      items={items}
      projectCapabilities={project.capabilities}
      selectedAssetId={selectedAssetId}
      creatingChatForAgentId={creatingChatForAgentId}
      activatingAgentId={
        changeStatus.isPending
          ? (changeStatus.variables?.assetId ?? null)
          : null
      }
      defaultAgentId={defaultAgent.data?.agent_asset_id ?? null}
      settingDefaultAgentTarget={
        setDefaultAgent.isPending
          ? (setDefaultAgent.variables?.agent_asset_id ?? "main")
          : null
      }
      defaultAgentLoading={defaultAgent.isLoading}
      defaultAgentError={Boolean(defaultAgent.error)}
      mcpDependencyReasons={mcpDependencyReasons}
      mcpDependencyError={Boolean(mcpDependencyRuntime.error)}
      onSelect={onSelect}
      onStartChat={(agent) => void startChat(agent)}
      onActivate={activate}
      onSetDefault={updateDefaultAgent}
      onRestoreMain={restoreMainDefault}
    />
  );
}

function ProjectAgentBuilderLead({
  project,
  data,
  startChatIntent,
  startChatIntentId,
}: {
  project: Project;
  data: Parameters<typeof ProjectAgentStartContinuation>[0]["catalog"];
  startChatIntent: boolean;
  startChatIntentId: string | null;
}) {
  const { user } = useAuth();
  const canCreate = agentBuilderCanAuthor(project.capabilities);
  const sessions = useAgentBuilderSessions(
    user?.id ?? "",
    project.id,
    Boolean(user && canCreate),
  );

  return (
    <>
      {canCreate ? (
        <AgentBuilderResumeBanner
          projectSlug={project.slug}
          sessions={sessions.data ?? []}
        />
      ) : null}
      <ProjectAgentStartContinuation
        project={project}
        catalog={data}
        requested={startChatIntent}
        intentId={startChatIntentId}
      />
    </>
  );
}

export function ProjectAgentsPage({
  startChatIntent = false,
  startChatIntentId = null,
  selectedAssetId = null,
}: {
  startChatIntent?: boolean;
  startChatIntentId?: string | null;
  selectedAssetId?: string | null;
}) {
  return (
    <ProjectAssetPageShell
      kind="agents"
      title="Agent"
      description="创建和管理具有专属 Prompt 与能力的自定义 Agent。系统默认 Main 不在此列表中展示。"
      layout="agent-cards"
      initialSelectedAssetId={selectedAssetId}
      renderLead={({ project, data }) => (
        <ProjectAgentBuilderLead
          project={project}
          data={data}
          startChatIntent={startChatIntent}
          startChatIntentId={startChatIntentId}
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
      renderAssetEditor={(version, context) => (
        <AgentInstructionsWorkbench
          accountId={context.accountId}
          projectId={context.projectId}
          item={context.item}
          version={version && "agent_id" in version ? version : null}
          canAuthor={context.canAuthor}
          editing={context.editing}
          onEditingChange={context.onEditingChange}
          onDirtyChange={context.onDirtyChange}
          onVersionCreated={context.onVersionCreated}
        />
      )}
    />
  );
}
