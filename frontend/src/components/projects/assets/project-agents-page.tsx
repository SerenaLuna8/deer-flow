"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  LayoutGridIcon,
  ListIcon,
  Loader2Icon,
  MessageSquareIcon,
  PowerIcon,
  StarIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  type AssetVersion,
  type ProjectAssetItem,
} from "@/core/shared-assets";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";
import { cn } from "@/lib/utils";

import { AgentCapabilityWorkbench } from "./agent-capability-workbench";
import { AgentInstructionsWorkbench } from "./agent-instructions-workbench";
import { ProjectAssetPageShell } from "./project-asset-page-shell";
import {
  isMainProjectAgent,
  useAgentMcpDependencyRuntime,
} from "./use-mcp-dependency-runtime";

export type ProjectAgentViewMode = "cards" | "list";

type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;

function AgentDetailWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  editing,
  onEditingChange,
  onDirtyChange,
  onVersionCreated,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: AgentAssetVersion | null;
  canAuthor: boolean;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
  const [instructionDirty, setInstructionDirty] = useState(false);
  const [capabilityDirty, setCapabilityDirty] = useState(false);
  const handleInstructionDirty = useCallback(
    (dirty: boolean) => setInstructionDirty(dirty),
    [],
  );
  const handleCapabilityDirty = useCallback(
    (dirty: boolean) => setCapabilityDirty(dirty),
    [],
  );

  useEffect(() => {
    onDirtyChange(instructionDirty || capabilityDirty);
  }, [capabilityDirty, instructionDirty, onDirtyChange]);

  useEffect(
    () => () => {
      onDirtyChange(false);
    },
    [onDirtyChange],
  );

  if (item.scope !== "project") {
    return (
      <AgentInstructionsWorkbench
        accountId={accountId}
        projectId={projectId}
        item={item}
        version={version}
        canAuthor={canAuthor}
        editing={editing}
        onEditingChange={onEditingChange}
        onDirtyChange={handleInstructionDirty}
        onVersionCreated={onVersionCreated}
      />
    );
  }

  return (
    <Tabs defaultValue="instructions" className="gap-5">
      <TabsList aria-label="Agent 详情配置">
        <TabsTrigger value="instructions">Agent 设定</TabsTrigger>
        <TabsTrigger value="capabilities">工具绑定</TabsTrigger>
      </TabsList>
      <TabsContent
        value="instructions"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <AgentInstructionsWorkbench
          accountId={accountId}
          projectId={projectId}
          item={item}
          version={version}
          canAuthor={canAuthor}
          editing={editing}
          onEditingChange={onEditingChange}
          onDirtyChange={handleInstructionDirty}
          onVersionCreated={onVersionCreated}
        />
      </TabsContent>
      <TabsContent
        value="capabilities"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <AgentCapabilityWorkbench
          accountId={accountId}
          projectId={projectId}
          item={item}
          version={version}
          canAuthor={canAuthor}
          onDirtyChange={handleCapabilityDirty}
          onVersionCreated={onVersionCreated}
        />
      </TabsContent>
    </Tabs>
  );
}

export function sortProjectAgentsWithDefaultFirst(
  items: readonly ProjectAssetItem[],
  defaultAgentId: string | null | undefined,
): ProjectAssetItem[] {
  if (!defaultAgentId) return [...items];
  return items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      const leftDefault = left.item.id === defaultAgentId;
      const rightDefault = right.item.id === defaultAgentId;
      if (leftDefault !== rightDefault) return leftDefault ? -1 : 1;
      return left.index - right.index;
    })
    .map(({ item }) => item);
}

export function ProjectAgentViewToggle({
  value,
  onChange,
}: {
  value: ProjectAgentViewMode;
  onChange: (value: ProjectAgentViewMode) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Agent 展示方式"
      className="border-border bg-background inline-flex h-10 overflow-hidden rounded-lg border p-0.5"
    >
      {(
        [
          ["cards", "卡片", LayoutGridIcon],
          ["list", "列表", ListIcon],
        ] as const
      ).map(([mode, label, Icon]) => {
        const selected = value === mode;
        return (
          <button
            key={mode}
            type="button"
            aria-pressed={selected}
            className={cn(
              "focus-visible:ring-ring inline-flex min-w-20 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:outline-none",
              selected
                ? "bg-foreground text-background"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
            onClick={() => onChange(mode)}
          >
            <Icon aria-hidden className="size-4" />
            {label}
          </button>
        );
      })}
    </div>
  );
}

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

function projectMainDefaultAvailability(
  item: ProjectAssetItem,
  projectCapabilities: readonly Capability[],
): ProjectAgentChatAvailability {
  if (!isMainProjectAgent(item)) {
    return { enabled: false, reason: "该系统 Agent 不能设为项目默认" };
  }
  if (!projectCapabilities.includes("shared_assets.manage_bindings")) {
    return { enabled: false, reason: "仅项目管理员可以修改默认 Agent" };
  }
  if (item.status !== "active") {
    return { enabled: false, reason: "Main Agent 当前不可用" };
  }
  if (!item.capabilities.includes("shared_assets.execute")) {
    return { enabled: false, reason: "当前账号没有执行 Main Agent 的权限" };
  }
  if (!item.current_published_version_id) {
    return { enabled: false, reason: "Main Agent 当前没有可用版本" };
  }
  return { enabled: true, reason: null };
}

function ProjectAgentCollectionView({
  items,
  source,
  viewMode,
  projectCapabilities,
  selectedAssetId,
  creatingChatForAgentId,
  activatingAgentId = null,
  defaultAgentId,
  settingDefaultAgentTarget = null,
  defaultAgentLoading = false,
  defaultAgentError = false,
  mcpDependencyReasons = new Map(),
  onSelect,
  onStartChat,
  onActivate,
  onSetDefault,
  onSetMainDefault,
}: {
  items: ProjectAssetItem[];
  source: "system" | "project";
  viewMode: ProjectAgentViewMode;
  projectCapabilities: readonly Capability[];
  selectedAssetId: string | null;
  creatingChatForAgentId: string | null;
  activatingAgentId?: string | null;
  defaultAgentId?: string | null;
  settingDefaultAgentTarget?: string | "main" | null;
  defaultAgentLoading?: boolean;
  defaultAgentError?: boolean;
  mcpDependencyReasons?: ReadonlyMap<string, string>;
  onSelect: (item: ProjectAssetItem) => void;
  onStartChat: (item: ExecutableProjectAgent) => void;
  onActivate?: (item: ProjectAssetItem) => void;
  onSetDefault?: (item: ProjectAssetItem) => void;
  onSetMainDefault?: () => void;
}) {
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm">
        {source === "system"
          ? "暂无可用于当前项目的系统 Agent。"
          : "暂无项目自建的 Agent。"}
      </p>
    );
  }

  return (
    <div
      role="list"
      data-agent-view={viewMode}
      className={cn(
        viewMode === "cards"
          ? "grid gap-3 sm:grid-cols-2 lg:gap-4 xl:grid-cols-3"
          : "overflow-hidden rounded-xl border",
      )}
    >
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
        const main = isMainProjectAgent(item);
        const isDefault =
          defaultAgentId !== undefined &&
          (main ? defaultAgentId === null : defaultAgentId === item.id);
        const defaultAvailability = main
          ? projectMainDefaultAvailability(item, projectCapabilities)
          : projectAgentDefaultAvailability(
              item,
              projectCapabilities,
              mcpDependencyReasons.get(item.id) ?? null,
            );
        const canOfferMainDefault =
          main &&
          projectCapabilities.includes("shared_assets.manage_bindings") &&
          Boolean(onSetMainDefault);
        const canOfferProjectDefault =
          item.scope === "project" &&
          projectCapabilities.includes("shared_assets.manage_bindings") &&
          item.capabilities.includes("shared_assets.manage_bindings") &&
          Boolean(onSetDefault);
        const canSetDefault =
          !isDefault && (canOfferMainDefault || canOfferProjectDefault);
        const defaultPending = settingDefaultAgentTarget !== null;
        const settingThisDefault =
          settingDefaultAgentTarget === (main ? "main" : item.id);
        const defaultActionReason = defaultAgentError
          ? "无法加载项目默认 Agent，请稍后重试"
          : defaultAgentLoading
            ? "正在加载项目默认 Agent"
            : defaultAgentId === undefined
              ? "无法确认项目默认 Agent，请稍后重试"
              : defaultAvailability.reason;
        const description = item.description?.trim();
        const defaultButton = canSetDefault ? (
          <Button
            type="button"
            variant="outline"
            size={viewMode === "list" ? "sm" : "default"}
            className={cn(viewMode === "cards" && "min-h-10 w-full")}
            disabled={
              defaultPending ||
              defaultAgentLoading ||
              defaultAgentError ||
              defaultAgentId === undefined ||
              !defaultAvailability.enabled
            }
            aria-label={
              defaultActionReason
                ? `将 ${item.display_name} 设为默认，${defaultActionReason}`
                : `将 ${item.display_name} 设为默认`
            }
            title={defaultActionReason ?? undefined}
            onClick={() => (main ? onSetMainDefault?.() : onSetDefault?.(item))}
          >
            {settingThisDefault ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : (
              <StarIcon aria-hidden className="size-4" />
            )}
            {settingThisDefault ? "设置中…" : "设为默认"}
          </Button>
        ) : null;
        const activateButton = canActivate ? (
          <Button
            type="button"
            variant="outline"
            size={viewMode === "list" ? "sm" : "default"}
            className={cn(viewMode === "cards" && "min-h-10 w-full")}
            disabled={activating}
            aria-label={`启用 ${item.display_name}`}
            onClick={() => onActivate?.(item)}
          >
            {activating ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : (
              <PowerIcon aria-hidden className="size-4" />
            )}
            {activating ? "启用中…" : "启用"}
          </Button>
        ) : null;
        const chatButton = (
          <Button
            type="button"
            size={viewMode === "list" ? "sm" : "default"}
            className={cn(viewMode === "cards" && "min-h-10 w-full")}
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
        );
        const statusBadges = (
          <>
            {main ? <Badge variant="secondary">系统内置</Badge> : null}
            {isDefault ? (
              <Badge variant="secondary">
                <StarIcon aria-hidden /> 当前默认
              </Badge>
            ) : null}
            {item.status !== "active" ? (
              <AssetStatusBadge
                status={item.status}
                label={item.status === "suspended" ? "已停用" : undefined}
              />
            ) : null}
          </>
        );

        if (viewMode === "list") {
          return (
            <div
              key={item.id}
              role="listitem"
              className={cn(
                "group flex flex-col border-b last:border-b-0 sm:flex-row sm:items-stretch",
                selectedAssetId === item.id
                  ? "bg-selection-subtle/60"
                  : "hover:bg-muted/40",
              )}
            >
              <button
                type="button"
                aria-haspopup="dialog"
                aria-label={`查看 ${item.display_name} 详情`}
                className="focus-visible:ring-ring flex min-w-0 flex-1 items-center gap-4 px-5 py-4 text-left focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
                onClick={() => onSelect(item)}
              >
                <span className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-xl">
                  <BotIcon aria-hidden className="size-5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-semibold sm:text-base">
                      {item.display_name}
                    </span>
                    {statusBadges}
                  </span>
                  <span className="text-muted-foreground mt-1 block truncate text-sm">
                    {description && description.length > 0
                      ? description
                      : main
                        ? "系统内置的通用 Agent，适用于大多数对话与协作场景。"
                        : "暂无简介，可进入详情完善 Agent 设置。"}
                  </span>
                </span>
              </button>
              <div className="border-border/70 flex shrink-0 items-center gap-2 border-t px-5 py-3 sm:border-t-0 sm:border-l">
                {defaultButton}
                {activateButton}
                {chatButton}
              </div>
            </div>
          );
        }

        return (
          <Card
            key={item.id}
            role="listitem"
            className={cn(
              "group gap-0 overflow-hidden py-0 transition-[border-color,box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:shadow-md",
              selectedAssetId === item.id
                ? "border-selection ring-selection/15 ring-2"
                : "hover:border-foreground/20",
            )}
          >
            <button
              type="button"
              aria-haspopup="dialog"
              aria-label={`查看 ${item.display_name} 详情`}
              className="focus-visible:ring-ring flex min-h-0 flex-col text-left focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
              onClick={() => onSelect(item)}
            >
              <CardHeader className="w-full gap-0 px-5 pt-5">
                <span className="flex min-w-0 items-center gap-2.5">
                  <span className="bg-muted flex size-9 shrink-0 items-center justify-center rounded-lg">
                    <BotIcon aria-hidden className="size-4.5" />
                  </span>
                  <span className="min-w-0 flex-1 truncate text-base font-semibold">
                    {item.display_name}
                  </span>
                  {statusBadges}
                </span>
              </CardHeader>
              <CardContent className="w-full px-5 pt-3 pb-3">
                <span className="text-muted-foreground line-clamp-2 h-10 overflow-hidden text-sm leading-5">
                  {description && description.length > 0
                    ? description
                    : main
                      ? "系统内置的通用 Agent，适用于大多数对话与协作场景。"
                      : "暂无简介，可进入详情完善 Agent 设置。"}
                </span>
              </CardContent>
            </button>
            <CardFooter
              className={cn(
                "grid grid-cols-1 gap-2 px-5 pb-4",
                (canSetDefault || canActivate) && "sm:grid-cols-2",
              )}
            >
              {defaultButton}
              {activateButton}
              {chatButton}
            </CardFooter>
          </Card>
        );
      })}
    </div>
  );
}

export function ProjectAgentCatalogView({
  systemItems,
  projectItems,
  projectCapabilities,
  viewMode,
  selectedAssetId,
  creatingChatForAgentId,
  activatingAgentId = null,
  defaultAgentId,
  settingDefaultAgentTarget = null,
  defaultAgentLoading = false,
  defaultAgentError = false,
  mcpDependencyReasons = new Map(),
  mcpDependencyError = false,
  onSelect,
  onStartChat,
  onActivate,
  onSetDefault,
  onSetMainDefault,
}: {
  systemItems: ProjectAssetItem[];
  projectItems: ProjectAssetItem[];
  projectCapabilities: readonly Capability[];
  viewMode: ProjectAgentViewMode;
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
  onSetMainDefault?: () => void;
}) {
  const orderedSystemItems = [...systemItems].sort((left, right) => {
    const leftMain = isMainProjectAgent(left);
    const rightMain = isMainProjectAgent(right);
    return leftMain === rightMain ? 0 : leftMain ? -1 : 1;
  });
  const orderedProjectItems = sortProjectAgentsWithDefaultFirst(
    projectItems,
    defaultAgentId,
  );
  const sharedProps = {
    viewMode,
    projectCapabilities,
    selectedAssetId,
    creatingChatForAgentId,
    activatingAgentId,
    defaultAgentId,
    settingDefaultAgentTarget,
    defaultAgentLoading,
    defaultAgentError,
    mcpDependencyReasons,
    onSelect,
    onStartChat,
    onActivate,
    onSetDefault,
    onSetMainDefault,
  };

  return (
    <div className="space-y-8">
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

      <section
        aria-labelledby="system-agent-section-title"
        className="space-y-3"
      >
        <div>
          <div className="flex items-center gap-2.5">
            <h2
              id="system-agent-section-title"
              className="text-lg font-semibold"
            >
              系统 Agent
            </h2>
            <Badge variant="secondary" className="tabular-nums">
              {orderedSystemItems.length}
            </Badge>
          </div>
          <p className="text-muted-foreground mt-1 text-sm">
            由系统提供，可直接用于项目协作
          </p>
        </div>
        <ProjectAgentCollectionView
          {...sharedProps}
          source="system"
          items={orderedSystemItems}
        />
      </section>

      <section
        aria-labelledby="project-agent-section-title"
        className="space-y-3"
      >
        <div>
          <div className="flex items-center gap-2.5">
            <h2
              id="project-agent-section-title"
              className="text-lg font-semibold"
            >
              项目 Agent
            </h2>
            <Badge variant="secondary" className="tabular-nums">
              {orderedProjectItems.length}
            </Badge>
          </div>
          <p className="text-muted-foreground mt-1 text-sm">
            由当前项目创建和管理
          </p>
        </div>
        <ProjectAgentCollectionView
          {...sharedProps}
          source="project"
          items={orderedProjectItems}
        />
      </section>
    </div>
  );
}

function ProjectAgentCatalog({
  project,
  data,
  viewMode,
  selectedAssetId,
  onSelect,
}: {
  project: Project;
  data: Parameters<typeof ProjectAgentStartContinuation>[0]["catalog"];
  viewMode: ProjectAgentViewMode;
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
  const systemItems = data.system_items.filter(
    (item) => isMainProjectAgent(item) || item.binding?.enabled === true,
  );
  const projectItems = data.project_items;
  const items = [...systemItems, ...projectItems];
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

  function setMainDefault() {
    const mainAgent = systemItems.find(isMainProjectAgent);
    if (
      !mainAgent ||
      !projectMainDefaultAvailability(mainAgent, project.capabilities)
        .enabled ||
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
        onSuccess: () => toast.success("已将 Main 设为项目默认 Agent"),
        onError: (error) => toast.error(adminAssetErrorMessage(error)),
      },
    );
  }

  return (
    <ProjectAgentCatalogView
      systemItems={systemItems}
      projectItems={projectItems}
      projectCapabilities={project.capabilities}
      viewMode={viewMode}
      selectedAssetId={selectedAssetId}
      creatingChatForAgentId={creatingChatForAgentId}
      activatingAgentId={
        changeStatus.isPending
          ? (changeStatus.variables?.assetId ?? null)
          : null
      }
      defaultAgentId={defaultAgent.data?.agent_asset_id}
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
      onSetMainDefault={setMainDefault}
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
      {user && canCreate ? (
        <AgentBuilderResumeBanner
          accountId={user.id}
          projectId={project.id}
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
  const [viewMode, setViewMode] = useState<ProjectAgentViewMode>("cards");

  return (
    <ProjectAssetPageShell
      kind="agents"
      title="Agent"
      layout="agent-cards"
      headerActions={
        <ProjectAgentViewToggle value={viewMode} onChange={setViewMode} />
      }
      initialSelectedAssetId={selectedAssetId}
      renderLead={({ project, data }) => (
        <ProjectAgentBuilderLead
          project={project}
          data={data}
          startChatIntent={startChatIntent}
          startChatIntentId={startChatIntentId}
        />
      )}
      renderList={({ project, data, selectedAssetId, onSelect }) => (
        <ProjectAgentCatalog
          project={project}
          data={data}
          viewMode={viewMode}
          selectedAssetId={selectedAssetId}
          onSelect={onSelect}
        />
      )}
      renderAssetEditor={(version, context) => (
        <AgentDetailWorkbench
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
