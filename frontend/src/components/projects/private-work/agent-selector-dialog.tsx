"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
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
  type EnableCurrentSystemBindingInput,
  type ProjectDefaultAgent,
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
} from "@/core/shared-assets";
import {
  supportedMcpVersionIds,
  type ScopedMcpVersion,
} from "@/core/shared-assets/mcp-runtime";
import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";
import { uuid } from "@/core/utils/uuid";

import {
  agentMcpDependencyAssessment,
  isMainProjectAgent,
  MAIN_PROJECT_AGENT_SLUG,
  useMcpDependencyRuntime,
} from "../assets/use-mcp-dependency-runtime";

export { projectAgentsStartChatPath };
export { MAIN_PROJECT_AGENT_SLUG };

export function projectAgentCreatePath(projectSlug: string): string {
  return `/projects/${encodeURIComponent(projectSlug)}/agents/new`;
}

export type ExecutableProjectAgent = ProjectAssetItem;

export type ProjectThreadAgentSelection = {
  agentAssetId: string;
  agentScope: "project" | "system";
};

export function otherProjectAgents(
  agents: readonly ProjectAssetItem[],
  currentAgent: ProjectThreadAgentSelection | null | undefined,
): ProjectAssetItem[] {
  if (!currentAgent) return [...agents];
  return agents.filter(
    (agent) =>
      agent.id !== currentAgent.agentAssetId ||
      agent.scope !== currentAgent.agentScope,
  );
}

export function configurableSystemAgents(
  catalog: ProjectAssetList | undefined,
): ProjectAssetItem[] {
  if (!catalog) return [];
  return catalog.system_items.filter(
    (item) =>
      item.status === "active" &&
      item.current_version_id !== null &&
      !isMainProjectAgent(item) &&
      item.binding?.enabled !== true &&
      item.capabilities.includes("shared_assets.execute") &&
      item.capabilities.includes("shared_assets.manage_bindings"),
  );
}

export type SystemAgentDependencyAvailability = "loading" | "ready" | "blocked";

function selectedAgentVersion(
  agent: ProjectAssetItem,
  history: VersionHistoryResponse,
) {
  const versionId = agent.current_version_id;
  return history.data.find(
    (version) =>
      "agent_id" in version &&
      version.id === versionId &&
      version.relation === "current",
  );
}

export function agentMcpDependencyAvailability(
  agent: ProjectAssetItem,
  history: VersionHistoryResponse | undefined,
  mcpVersions: readonly ScopedMcpVersion[] | undefined,
): SystemAgentDependencyAvailability {
  return agentMcpDependencyAssessment(agent, history, mcpVersions).status;
}

export function systemAgentDependencyAvailability(
  agent: ProjectAssetItem,
  history: VersionHistoryResponse | undefined,
  boundSkillAssetRefs: ReadonlySet<string>,
  boundMcpVersionIds: ReadonlySet<string>,
): SystemAgentDependencyAvailability {
  if (!history) return "loading";
  const currentVersion = history.data.find(
    (version) =>
      "agent_id" in version &&
      version.id === agent.current_version_id &&
      version.relation === "current",
  );
  if (!currentVersion || !("agent_id" in currentVersion)) return "blocked";
  return currentVersion.skill_refs.every((ref) =>
    boundSkillAssetRefs.has(`${ref.scope}:${ref.asset_id}`),
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
      .map((item) => item.binding?.current_version_id)
      .filter((id): id is string => Boolean(id)),
  );
}

function boundSystemSkillAssetRefs(
  catalog: ProjectAssetList | undefined,
): Set<string> {
  return new Set(
    (catalog?.system_items ?? [])
      .filter(
        (item) =>
          item.binding?.enabled === true && item.current_version_id !== null,
      )
      .map((item) => `system:${item.id}`),
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
      (item) => executable(item) && item.current_version_id !== null,
    ),
    ...catalog.system_items.filter(
      (item) =>
        executable(item) &&
        item.current_version_id !== null &&
        (item.binding?.enabled === true || isMainProjectAgent(item)),
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
        isMainProjectAgent(item) &&
        item.status === "active" &&
        item.current_version_id !== null &&
        item.capabilities.includes("shared_assets.execute"),
    ) ?? null
  );
}

export type ProjectDefaultAgentResolution =
  | {
      status: "ready";
      source: "main" | "project";
      agent: ExecutableProjectAgent;
    }
  | {
      status: "unavailable";
      reason: "unknown" | "main-unavailable" | "project-unavailable";
    };

export function projectDefaultAgentUnavailableMessage(
  reason: Extract<
    ProjectDefaultAgentResolution,
    { status: "unavailable" }
  >["reason"],
  copy: Translations["agents"]["newChat"],
): string {
  switch (reason) {
    case "unknown":
      return copy.defaultUnknown;
    case "main-unavailable":
      return copy.mainUnavailable;
    case "project-unavailable":
      return copy.projectUnavailable;
  }
}

export function resolveProjectDefaultAgent(
  catalog: ProjectAssetList | undefined,
  setting: ProjectDefaultAgent | undefined,
): ProjectDefaultAgentResolution {
  if (!catalog || !setting) {
    return {
      status: "unavailable",
      reason: "unknown",
    };
  }
  if (setting.agent_asset_id === null) {
    const agent = mainProjectAgent(catalog);
    return agent
      ? { status: "ready", source: "main", agent }
      : {
          status: "unavailable",
          reason: "main-unavailable",
        };
  }
  const agent = catalog.project_items.find(
    (item) =>
      item.id === setting.agent_asset_id &&
      item.status === "active" &&
      item.current_version_id !== null &&
      item.capabilities.includes("shared_assets.execute"),
  );
  return agent
    ? { status: "ready", source: "project", agent }
    : {
        status: "unavailable",
        reason: "project-unavailable",
      };
}

export function projectThreadAgentSelection(
  agent: ExecutableProjectAgent,
): ProjectThreadAgentSelection {
  return {
    agentAssetId: agent.id,
    agentScope: agent.scope,
  };
}

type CreateProjectChatBaseDependencies = {
  scope: ProjectClientScope;
  projectSlug: string;
  threadDisplayName: string;
  createThreadId?: () => string;
  createThread?: (
    scope: ProjectClientScope,
    input: CreateProjectThreadInput,
  ) => Promise<unknown>;
  invalidateThreadLists?: () => void;
  navigate: (path: string) => void;
};

type CreateProjectChatDependencies = CreateProjectChatBaseDependencies & {
  agent: ExecutableProjectAgent;
};

export async function createProjectChatForAgent({
  scope,
  projectSlug,
  agent,
  threadDisplayName,
  createThreadId = uuid,
  createThread = createProjectThread,
  invalidateThreadLists,
  navigate,
}: CreateProjectChatDependencies): Promise<string> {
  const threadId = createThreadId();
  await createThread(scope, {
    threadId,
    ...projectThreadAgentSelection(agent),
    displayName: threadDisplayName,
  });
  invalidateThreadLists?.();
  navigate(
    `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(threadId)}`,
  );
  return threadId;
}

export async function createProjectChatWithDefaultAgent({
  scope,
  projectSlug,
  threadDisplayName,
  createThreadId = uuid,
  createThread = createProjectThread,
  invalidateThreadLists,
  navigate,
}: CreateProjectChatBaseDependencies): Promise<string> {
  const threadId = createThreadId();
  await createThread(scope, { threadId, displayName: threadDisplayName });
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
  systemAgentUnavailableMessage: string;
  enableBinding: (input: EnableCurrentSystemBindingInput) => Promise<unknown>;
};

export async function enableSystemAgentAndCreateProjectChat({
  enableBinding,
  agent,
  systemAgentUnavailableMessage,
  ...createDependencies
}: EnableSystemAgentAndCreateProjectChatDependencies): Promise<string> {
  if (
    agent.scope !== "system" ||
    agent.status !== "active" ||
    agent.current_version_id === null
  ) {
    throw new Error(systemAgentUnavailableMessage);
  }
  await enableBinding({
    asset_id: agent.id,
    ...(agent.binding
      ? { expected_binding_version: agent.binding.version }
      : {}),
  });
  return createProjectChatForAgent({
    ...createDependencies,
    agent,
  });
}

function McpBlockedAgentsNotice({ agents }: { agents: ProjectAssetItem[] }) {
  const { t } = useI18n();
  const copy = t.agents.selector;
  if (agents.length === 0) return null;
  return (
    <div className="rounded-xl border border-dashed p-4">
      <p className="text-sm font-medium">{copy.mcpBlockedTitle}</p>
      <p className="text-muted-foreground mt-1 text-sm">
        {copy.mcpBlockedDescription}
      </p>
      <ul className="mt-3 space-y-1 text-sm">
        {agents.map((agent) => (
          <li key={agent.id}>{agent.display_name}</li>
        ))}
      </ul>
    </div>
  );
}

export function AgentSelectorDialog({
  open,
  agents,
  configurableSystemAgents: systemAgents = [],
  blockedSystemAgents = [],
  blockedRuntimeAgents = [],
  canAuthorProjectAgent = false,
  agentsPath,
  title,
  description,
  emptyTitle,
  emptyDescription,
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
  blockedRuntimeAgents?: ProjectAssetItem[];
  canAuthorProjectAgent?: boolean;
  agentsPath?: string;
  title?: string;
  description?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  isCreating: boolean;
  isLoading?: boolean;
  error?: Error | null;
  onOpenChange: (open: boolean) => void;
  onSelect: (agent: ExecutableProjectAgent) => void;
  onEnableSystemAgent?: (agent: ProjectAssetItem) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.selector;
  const resolvedTitle = title ?? copy.title;
  const resolvedDescription = description ?? copy.description;
  const resolvedEmptyTitle = emptyTitle ?? copy.emptyTitle;
  const resolvedEmptyDescription = emptyDescription ?? copy.emptyDescription;
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
              {resolvedTitle}
            </h2>
            <p
              id="project-agent-selector-description"
              className="text-muted-foreground mt-1 text-sm"
            >
              {resolvedDescription}
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-dialog-initial-focus
            onClick={() => onOpenChange(false)}
          >
            {t.agents.common.close}
          </Button>
        </div>
        {isLoading ? (
          <p role="status" className="text-muted-foreground mt-6 text-sm">
            {copy.loading}
          </p>
        ) : error ? (
          <p role="alert" className="text-destructive mt-6 text-sm">
            {copy.loadFailed}
          </p>
        ) : agents.length > 0 ? (
          <div className="mt-6 space-y-4">
            <div className="space-y-2">
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
                    {agent.scope === "project"
                      ? copy.projectAgent
                      : copy.systemAgent}
                  </span>
                </Button>
              ))}
            </div>
            <McpBlockedAgentsNotice agents={blockedRuntimeAgents} />
          </div>
        ) : (
          <div className="mt-6 space-y-5">
            <div>
              <h3 className="font-medium">{resolvedEmptyTitle}</h3>
              <p className="text-muted-foreground mt-2 text-sm">
                {resolvedEmptyDescription}
              </p>
            </div>
            {systemAgents.length > 0 && (
              <div className="space-y-2">
                <p className="text-sm font-medium">{copy.enableNow}</p>
                {systemAgents.map((agent) => (
                  <Button
                    key={agent.id}
                    type="button"
                    className="h-auto w-full justify-between px-4 py-3"
                    disabled={isCreating}
                    onClick={() => onEnableSystemAgent?.(agent)}
                  >
                    <span className="truncate">
                      {copy.enableAndChat(agent.display_name)}
                    </span>
                    <span className="text-primary-foreground/75 text-xs">
                      {copy.systemAgent}
                    </span>
                  </Button>
                ))}
              </div>
            )}
            {blockedSystemAgents.length > 0 && (
              <div className="rounded-xl border border-dashed p-4">
                <p className="text-sm font-medium">{copy.dependencyTitle}</p>
                <p className="text-muted-foreground mt-1 text-sm">
                  {copy.dependencyDescription}
                </p>
                <ul className="mt-3 space-y-1 text-sm">
                  {blockedSystemAgents.map((agent) => (
                    <li key={agent.id}>{agent.display_name}</li>
                  ))}
                </ul>
              </div>
            )}
            <McpBlockedAgentsNotice agents={blockedRuntimeAgents} />
            {canAuthorProjectAgent && agentsPath && (
              <Button asChild variant="outline" className="w-full">
                <Link href={agentsPath}>
                  {blockedSystemAgents.length > 0 ||
                  blockedRuntimeAgents.length > 0
                    ? copy.configure
                    : copy.createProjectAgent}
                </Link>
              </Button>
            )}
            {systemAgents.length === 0 && !canAuthorProjectAgent && (
              <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
                {copy.contactEditor}
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
  currentAgent,
  onOpenChange,
}: {
  project: Project;
  open: boolean;
  currentAgent?: ProjectThreadAgentSelection | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.selector;
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
  const candidateAgents = useMemo(
    () =>
      otherProjectAgents(executableProjectAgents(assets.data), currentAgent),
    [assets.data, currentAgent],
  );
  const systemAgents = useMemo(
    () =>
      otherProjectAgents(configurableSystemAgents(assets.data), currentAgent),
    [assets.data, currentAgent],
  );
  const shouldCheckDependencies = open && Boolean(user);
  const systemDependencyCheck =
    shouldCheckDependencies && systemAgents.length > 0;
  const skillAssets = useProjectAssets(
    user?.id ?? "",
    project.id,
    "skills",
    systemDependencyCheck,
  );
  const mcpAssets = useProjectAssets(
    user?.id ?? "",
    project.id,
    "mcp-servers",
    systemDependencyCheck,
  );
  const candidateAgentHistories = useQueries({
    queries: candidateAgents.map((agent) => ({
      queryKey: projectAssetVersionsKey(
        user?.id ?? "",
        project.id,
        "agents",
        agent.id,
      ),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listProjectAssetVersions(project.id, "agents", agent.id, signal),
      enabled: shouldCheckDependencies && !isMainProjectAgent(agent),
    })),
  });
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
      enabled: systemDependencyCheck,
    })),
  });
  const requiredMcpVersionIds = useMemo(() => {
    const ids = new Set<string>();
    for (const [index, agent] of candidateAgents.entries()) {
      if (isMainProjectAgent(agent)) continue;
      const version = candidateAgentHistories[index]?.data
        ? selectedAgentVersion(agent, candidateAgentHistories[index].data)
        : undefined;
      if (version && "agent_id" in version) {
        for (const id of version.mcp_version_ids) ids.add(id);
      }
    }
    for (const [index, agent] of systemAgents.entries()) {
      const version = systemAgentHistories[index]?.data
        ? selectedAgentVersion(agent, systemAgentHistories[index].data)
        : undefined;
      if (version && "agent_id" in version) {
        for (const id of version.mcp_version_ids) ids.add(id);
      }
    }
    return [...ids];
  }, [
    candidateAgentHistories,
    candidateAgents,
    systemAgentHistories,
    systemAgents,
  ]);
  const mcpDependencyRuntime = useMcpDependencyRuntime({
    accountId: user?.id ?? "",
    projectId: project.id,
    requiredVersionIds: requiredMcpVersionIds,
    enabled: shouldCheckDependencies,
  });
  const candidateDependencyAvailability = candidateAgents.map((agent, index) =>
    agentMcpDependencyAvailability(
      agent,
      candidateAgentHistories[index]?.data,
      mcpDependencyRuntime.isLoading || mcpDependencyRuntime.error
        ? undefined
        : mcpDependencyRuntime.versions,
    ),
  );
  const agents = candidateAgents.filter(
    (_agent, index) => candidateDependencyAvailability[index] === "ready",
  );
  const blockedRuntimeAgents = candidateAgents.filter(
    (_agent, index) => candidateDependencyAvailability[index] === "blocked",
  );
  const boundSkillAssetRefs = useMemo(
    () => boundSystemSkillAssetRefs(skillAssets.data),
    [skillAssets.data],
  );
  const boundMcpVersionIds = useMemo(() => {
    const bound = boundSystemVersionIds(mcpAssets.data);
    const supported = supportedMcpVersionIds(mcpDependencyRuntime.versions);
    return new Set([...bound].filter((id) => supported.has(id)));
  }, [mcpAssets.data, mcpDependencyRuntime.versions]);
  const dependencyAvailability = systemAgents.map((agent, index) =>
    systemAgentDependencyAvailability(
      agent,
      systemAgentHistories[index]?.data,
      boundSkillAssetRefs,
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
    shouldCheckDependencies &&
    (candidateAgentHistories.some((query) => query.isLoading) ||
      mcpDependencyRuntime.isLoading ||
      (systemDependencyCheck &&
        (skillAssets.isLoading ||
          mcpAssets.isLoading ||
          systemAgentHistories.some((query) => query.isLoading))));
  const rawDependencyError =
    candidateAgentHistories.find((query) => query.error)?.error ??
    mcpDependencyRuntime.error ??
    (systemDependencyCheck ? skillAssets.error : null) ??
    (systemDependencyCheck ? mcpAssets.error : null) ??
    systemAgentHistories.find((query) => query.error)?.error ??
    null;
  const dependencyError =
    rawDependencyError instanceof Error
      ? rawDependencyError
      : rawDependencyError
        ? new Error(copy.dependencyLoadFailed)
        : null;
  const enableSystemAgent = useEnableProjectSystemBinding(
    user?.id ?? "",
    project.id,
    "agent",
  );
  const [isCreating, setIsCreating] = useState(false);

  const handleSelect = async (agent: ExecutableProjectAgent) => {
    const scope = privateWork.scope;
    if (!scope || isCreating) return;
    setIsCreating(true);
    try {
      await createProjectChatForAgent({
        scope,
        projectSlug: project.slug,
        agent,
        threadDisplayName: t.agents.newChat.threadName,
        invalidateThreadLists: () =>
          invalidateStoppedThreadCaches(queryClient, null, false, scope),
        navigate: (path) => router.push(path),
      });
      onOpenChange(false);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : copy.createChatFailed,
      );
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
        threadDisplayName: t.agents.newChat.threadName,
        systemAgentUnavailableMessage: copy.systemUnavailable,
        enableBinding: (input) => enableSystemAgent.mutateAsync(input),
        invalidateThreadLists: () =>
          invalidateStoppedThreadCaches(queryClient, null, false, scope),
        navigate: (path) => router.push(path),
      });
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : copy.enableFailed);
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
      blockedRuntimeAgents={blockedRuntimeAgents}
      title={copy.alternateTitle}
      description={copy.alternateDescription}
      emptyTitle={copy.alternateEmptyTitle}
      emptyDescription={copy.alternateEmptyDescription}
      canAuthorProjectAgent={project.capabilities.includes(
        "shared_assets.edit",
      )}
      agentsPath={projectAgentCreatePath(project.slug)}
      isCreating={isCreating || enableSystemAgent.isPending}
      isLoading={assets.isLoading || dependencyLoading}
      error={assets.error ?? dependencyError}
      onOpenChange={onOpenChange}
      onSelect={(agent) => void handleSelect(agent)}
      onEnableSystemAgent={(agent) => void handleEnableSystemAgent(agent)}
    />
  );
}
