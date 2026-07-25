"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  createProjectThread,
  type CreateProjectThreadInput,
} from "@/core/private-work/api-client";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { projectAgentsStartChatPath } from "@/core/private-work/start-chat-intent";
import type { ProjectClientScope } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";
import {
  useEnableProjectSystemBinding,
  useProjectAssets,
  listProjectAssetVersions,
  projectAssetVersionsKey,
  type EnableSystemBindingInput,
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
} from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";
import { uuid } from "@/core/utils/uuid";

export { projectAgentsStartChatPath };

export type ExecutableProjectAgent = ProjectAssetItem;
export const MAIN_PROJECT_AGENT_SLUG = "project-assistant";

export function configurableSystemAgents(
  catalog: ProjectAssetList | undefined,
): ProjectAssetItem[] {
  if (!catalog) return [];
  return catalog.system_items.filter(
    (item) =>
      item.status === "active" &&
      item.current_published_version_id !== null &&
      item.binding?.enabled !== true &&
      item.capabilities.includes("shared_assets.execute") &&
      item.capabilities.includes("shared_assets.manage_bindings"),
  );
}

export type SystemAgentDependencyAvailability = "loading" | "ready" | "blocked";

export function systemAgentDependencyAvailability(
  agent: ProjectAssetItem,
  history: VersionHistoryResponse | undefined,
  boundSkillVersionIds: ReadonlySet<string>,
  boundMcpVersionIds: ReadonlySet<string>,
): SystemAgentDependencyAvailability {
  if (!history) return "loading";
  const currentVersion = history.data.find(
    (version) =>
      "agent_id" in version &&
      version.id === agent.current_published_version_id &&
      version.workflow_status === "published",
  );
  if (!currentVersion || !("agent_id" in currentVersion)) return "blocked";
  return currentVersion.skill_version_ids.every((id) =>
    boundSkillVersionIds.has(id),
  ) && currentVersion.mcp_version_ids.every((id) => boundMcpVersionIds.has(id))
    ? "ready"
    : "blocked";
}

function boundSystemVersionIds(
  catalog: ProjectAssetList | undefined,
): Set<string> {
  return new Set(
    (catalog?.system_items ?? [])
      .filter((item) => item.binding?.enabled === true)
      .map((item) => item.binding?.version_id)
      .filter((id): id is string => Boolean(id)),
  );
}

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

export function mainProjectAgent(
  catalog: ProjectAssetList | undefined,
): ExecutableProjectAgent | null {
  if (!catalog) return null;
  return (
    catalog.system_items.find(
      (item) =>
        item.slug === MAIN_PROJECT_AGENT_SLUG &&
        item.status === "active" &&
        item.current_published_version_id !== null &&
        item.capabilities.includes("shared_assets.execute"),
    ) ?? null
  );
}

type MainSystemBindingKind = "agent" | "skill" | "mcp";

type EnsureMainSystemAgentBindingsDependencies = {
  agent: ProjectAssetItem;
  requiredSkillVersionIds: readonly string[];
  requiredMcpVersionIds: readonly string[];
  skillCatalog: ProjectAssetList;
  mcpCatalog: ProjectAssetList;
  enableBinding: (
    kind: MainSystemBindingKind,
    input: EnableSystemBindingInput,
  ) => Promise<unknown>;
};

function bindingInput(
  item: ProjectAssetItem,
  versionId: string,
): EnableSystemBindingInput {
  return {
    asset_id: item.id,
    version_id: versionId,
    ...(item.binding ? { expected_binding_version: item.binding.version } : {}),
  };
}

async function ensureSystemDependencyBindings(
  kind: Exclude<MainSystemBindingKind, "agent">,
  requiredVersionIds: readonly string[],
  catalog: ProjectAssetList,
  enableBinding: EnsureMainSystemAgentBindingsDependencies["enableBinding"],
) {
  for (const versionId of requiredVersionIds) {
    const item = catalog.system_items.find(
      (candidate) =>
        candidate.status === "active" &&
        candidate.current_published_version_id === versionId,
    );
    if (!item) {
      throw new Error("Main 的系统依赖尚未就绪");
    }
    if (item.binding?.enabled === true) {
      if (item.binding.version_id !== versionId) {
        throw new Error("Main 的系统依赖版本与当前项目不一致");
      }
      continue;
    }
    await enableBinding(kind, bindingInput(item, versionId));
  }
}

export async function ensureMainSystemAgentBindings({
  agent,
  requiredSkillVersionIds,
  requiredMcpVersionIds,
  skillCatalog,
  mcpCatalog,
  enableBinding,
}: EnsureMainSystemAgentBindingsDependencies): Promise<void> {
  if (
    agent.scope !== "system" ||
    agent.status !== "active" ||
    agent.current_published_version_id === null
  ) {
    throw new Error("Main 智能体尚未就绪");
  }
  if (agent.binding?.enabled === true) return;

  await ensureSystemDependencyBindings(
    "skill",
    requiredSkillVersionIds,
    skillCatalog,
    enableBinding,
  );
  await ensureSystemDependencyBindings(
    "mcp",
    requiredMcpVersionIds,
    mcpCatalog,
    enableBinding,
  );
  await enableBinding(
    "agent",
    bindingInput(agent, agent.current_published_version_id),
  );
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
  invalidateThreadLists?: () => void;
  navigate: (path: string) => void;
};

export async function createProjectChatForAgent({
  scope,
  projectSlug,
  agent,
  createThreadId = uuid,
  createThread = createProjectThread,
  invalidateThreadLists,
  navigate,
}: CreateProjectChatDependencies): Promise<string> {
  const threadId = createThreadId();
  await createThread(scope, {
    threadId,
    ...projectThreadAgentSelection(agent),
    displayName: "新对话",
  });
  invalidateThreadLists?.();
  navigate(
    `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(threadId)}`,
  );
  return threadId;
}

type EnableSystemAgentAndCreateProjectChatDependencies = Omit<
  CreateProjectChatDependencies,
  "agent"
> & {
  agent: ProjectAssetItem;
  enableBinding: (input: EnableSystemBindingInput) => Promise<unknown>;
};

export async function enableSystemAgentAndCreateProjectChat({
  enableBinding,
  agent,
  ...createDependencies
}: EnableSystemAgentAndCreateProjectChatDependencies): Promise<string> {
  if (
    agent.scope !== "system" ||
    agent.status !== "active" ||
    agent.current_published_version_id === null
  ) {
    throw new Error("该系统 Agent 暂时无法启用");
  }
  await enableBinding({
    asset_id: agent.id,
    version_id: agent.current_published_version_id,
    ...(agent.binding
      ? { expected_binding_version: agent.binding.version }
      : {}),
  });
  return createProjectChatForAgent({
    ...createDependencies,
    agent,
  });
}

export function AgentSelectorDialog({
  open,
  agents,
  configurableSystemAgents: systemAgents = [],
  blockedSystemAgents = [],
  canAuthorProjectAgent = false,
  agentsPath,
  isCreating,
  isLoading = false,
  error = null,
  onOpenChange,
  onSelect,
  onEnableSystemAgent,
}: {
  open: boolean;
  agents: ExecutableProjectAgent[];
  configurableSystemAgents?: ProjectAssetItem[];
  blockedSystemAgents?: ProjectAssetItem[];
  canAuthorProjectAgent?: boolean;
  agentsPath?: string;
  isCreating: boolean;
  isLoading?: boolean;
  error?: Error | null;
  onOpenChange: (open: boolean) => void;
  onSelect: (agent: ExecutableProjectAgent) => void;
  onEnableSystemAgent?: (agent: ProjectAssetItem) => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = requestAnimationFrame(() => {
      dialogRef.current
        ?.querySelector<HTMLElement>("[data-dialog-initial-focus]")
        ?.focus();
    });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onOpenChange(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((element) => !element.hasAttribute("aria-hidden"));
      if (focusable.length === 0) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus();
    };
  }, [onOpenChange, open]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      data-testid="project-agent-selector-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onOpenChange(false);
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-agent-selector-title"
        aria-describedby="project-agent-selector-description"
        className="bg-background max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-2xl border p-6 shadow-xl"
        tabIndex={-1}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2
              id="project-agent-selector-title"
              className="text-lg font-semibold"
            >
              选择 Agent
            </h2>
            <p
              id="project-agent-selector-description"
              className="text-muted-foreground mt-1 text-sm"
            >
              选择一个 Agent 开始新的私有对话。
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-dialog-initial-focus
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
        ) : agents.length > 0 ? (
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
        ) : (
          <div className="mt-6 space-y-5">
            <div>
              <h3 className="font-medium">项目还没有可执行 Agent</h3>
              <p className="text-muted-foreground mt-2 text-sm">
                需要先启用一个系统 Agent，或创建并发布项目 Agent。
              </p>
            </div>
            {systemAgents.length > 0 && (
              <div className="space-y-2">
                <p className="text-sm font-medium">可立即启用</p>
                {systemAgents.map((agent) => (
                  <Button
                    key={agent.id}
                    type="button"
                    className="h-auto w-full justify-between px-4 py-3"
                    disabled={isCreating}
                    onClick={() => onEnableSystemAgent?.(agent)}
                  >
                    <span className="truncate">
                      启用 {agent.display_name} 并开始对话
                    </span>
                    <span className="text-primary-foreground/75 text-xs">
                      系统 Agent
                    </span>
                  </Button>
                ))}
              </div>
            )}
            {blockedSystemAgents.length > 0 && (
              <div className="rounded-xl border border-dashed p-4">
                <p className="text-sm font-medium">需要先完成依赖配置</p>
                <p className="text-muted-foreground mt-1 text-sm">
                  以下系统 Agent 依赖的 Skill 或 MCP 尚未全部在项目中启用。
                </p>
                <ul className="mt-3 space-y-1 text-sm">
                  {blockedSystemAgents.map((agent) => (
                    <li key={agent.id}>{agent.display_name}</li>
                  ))}
                </ul>
              </div>
            )}
            {(canAuthorProjectAgent || blockedSystemAgents.length > 0) &&
              agentsPath && (
                <Button asChild variant="outline" className="w-full">
                  <Link href={agentsPath}>
                    {blockedSystemAgents.length > 0
                      ? "前往 Agent 页面完成配置"
                      : "创建项目 Agent"}
                  </Link>
                </Button>
              )}
            {systemAgents.length === 0 &&
              blockedSystemAgents.length === 0 &&
              !canAuthorProjectAgent && (
                <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
                  请联系项目 Admin 或 Editor 完成配置。
                </p>
              )}
          </div>
        )}
      </div>
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
  const queryClient = useQueryClient();
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
  const systemAgents = useMemo(
    () => configurableSystemAgents(assets.data as ProjectAssetList | undefined),
    [assets.data],
  );
  const needsDependencyCheck =
    open && Boolean(user) && agents.length === 0 && systemAgents.length > 0;
  const skillAssets = useProjectAssets(
    user?.id ?? "",
    project.id,
    "skills",
    needsDependencyCheck,
  );
  const mcpAssets = useProjectAssets(
    user?.id ?? "",
    project.id,
    "mcp-servers",
    needsDependencyCheck,
  );
  const systemAgentHistories = useQueries({
    queries: systemAgents.map((agent) => ({
      queryKey: projectAssetVersionsKey(
        user?.id ?? "",
        project.id,
        "agents",
        agent.id,
      ),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listProjectAssetVersions(project.id, "agents", agent.id, signal),
      enabled: needsDependencyCheck,
    })),
  });
  const boundSkillVersionIds = useMemo(
    () =>
      boundSystemVersionIds(skillAssets.data as ProjectAssetList | undefined),
    [skillAssets.data],
  );
  const boundMcpVersionIds = useMemo(
    () => boundSystemVersionIds(mcpAssets.data as ProjectAssetList | undefined),
    [mcpAssets.data],
  );
  const dependencyAvailability = systemAgents.map((agent, index) =>
    systemAgentDependencyAvailability(
      agent,
      systemAgentHistories[index]?.data,
      boundSkillVersionIds,
      boundMcpVersionIds,
    ),
  );
  const readySystemAgents = systemAgents.filter(
    (_agent, index) => dependencyAvailability[index] === "ready",
  );
  const blockedSystemAgents = systemAgents.filter(
    (_agent, index) => dependencyAvailability[index] === "blocked",
  );
  const dependencyLoading =
    needsDependencyCheck &&
    (skillAssets.isLoading ||
      mcpAssets.isLoading ||
      systemAgentHistories.some((query) => query.isLoading));
  const dependencyError =
    skillAssets.error ??
    mcpAssets.error ??
    systemAgentHistories.find((query) => query.error)?.error ??
    null;
  const enableSystemAgent = useEnableProjectSystemBinding(
    user?.id ?? "",
    project.id,
    "agent",
  );
  const [isCreating, setIsCreating] = useState(false);
  const startChatIntentIdRef = useRef<string | null>(null);
  startChatIntentIdRef.current ??= uuid();

  const handleSelect = async (agent: ExecutableProjectAgent) => {
    const scope = privateWork.scope;
    if (!scope || isCreating) return;
    setIsCreating(true);
    try {
      await createProjectChatForAgent({
        scope,
        projectSlug: project.slug,
        agent,
        invalidateThreadLists: () =>
          invalidateStoppedThreadCaches(queryClient, null, false, scope),
        navigate: (path) => router.push(path),
      });
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法创建项目对话");
    } finally {
      setIsCreating(false);
    }
  };

  const handleEnableSystemAgent = async (agent: ProjectAssetItem) => {
    const scope = privateWork.scope;
    if (!scope || isCreating) return;
    setIsCreating(true);
    try {
      await enableSystemAgentAndCreateProjectChat({
        scope,
        projectSlug: project.slug,
        agent,
        enableBinding: (input) => enableSystemAgent.mutateAsync(input),
        invalidateThreadLists: () =>
          invalidateStoppedThreadCaches(queryClient, null, false, scope),
        navigate: (path) => router.push(path),
      });
      onOpenChange(false);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "无法启用项目 Agent",
      );
    } finally {
      setIsCreating(false);
    }
  };

  return (
    <AgentSelectorDialog
      open={open}
      agents={agents}
      configurableSystemAgents={readySystemAgents}
      blockedSystemAgents={blockedSystemAgents}
      canAuthorProjectAgent={project.capabilities.includes(
        "shared_assets.edit",
      )}
      agentsPath={projectAgentsStartChatPath(
        project.slug,
        startChatIntentIdRef.current,
      )}
      isCreating={isCreating || enableSystemAgent.isPending}
      isLoading={assets.isLoading || dependencyLoading}
      error={assets.error ?? dependencyError}
      onOpenChange={onOpenChange}
      onSelect={(agent) => void handleSelect(agent)}
      onEnableSystemAgent={(agent) => void handleEnableSystemAgent(agent)}
    />
  );
}
