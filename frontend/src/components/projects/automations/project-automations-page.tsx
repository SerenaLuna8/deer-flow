"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { useAgentMcpDependencyRuntime } from "@/components/projects/assets/use-mcp-dependency-runtime";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { AutomationApiError } from "@/core/project-automations/api";
import {
  useCreateProjectAutomation,
  useDeleteProjectAutomation,
  usePauseProjectAutomation,
  useProjectAutomationRuns,
  useProjectAutomations,
  useResumeProjectAutomation,
  useThreadProjectAutomations,
  useTriggerProjectAutomation,
  useUpdateProjectAutomation,
} from "@/core/project-automations/hooks";
import { useProjectAutomationReadiness } from "@/core/project-automations/readiness";
import type {
  CreateAutomationInput,
  UpdateAutomationInput,
} from "@/core/project-automations/types";
import type { Capability, Project } from "@/core/projects/types";
import { useProjectAssets } from "@/core/shared-assets";
import { useThreads } from "@/core/threads/hooks";
import { titleOfThread } from "@/core/threads/utils";

import {
  executableProjectAgents,
  MAIN_PROJECT_AGENT_SLUG,
} from "../private-work/agent-selector-dialog";
import { ProjectAccessDenied } from "../project-access-denied";
import { useCurrentProject } from "../project-context";

import type { AutomationAgentOption } from "./automation-form";
import {
  AutomationWorkbench,
  type AutomationAction,
  type AutomationActionFeedback,
  type AutomationPermissions,
} from "./automation-workbench";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

export function automationPermissions(
  capabilities: Capability[],
): AutomationPermissions {
  const canRead = capabilities.includes("private_work.read_own");
  const canManage = capabilities.includes("automation.manage_own");
  const canExecute =
    canManage &&
    capabilities.includes("private_work.create") &&
    capabilities.includes("shared_assets.execute");
  return { canRead, canManage, canExecute };
}

export function automationActionFeedback(
  action: AutomationAction,
  error: unknown,
): AutomationActionFeedback {
  if (error instanceof AutomationApiError) {
    if (error.status === 429 || error.code === "AUTOMATION_CONCURRENCY_LIMIT") {
      return {
        action,
        kind: "rate_limit",
        message: "当前并发已达上限，请稍后重试。",
      };
    }
    if (
      error.status === 503 ||
      error.status === 0 ||
      error.code === "AUTOMATION_UNAVAILABLE" ||
      error.code === "AUTOMATION_NETWORK_ERROR"
    ) {
      return {
        action,
        kind: "unavailable",
        message: "Automation 暂时不可用，请稍后重试。",
      };
    }
    if (error.status === 409) {
      return {
        action,
        kind: "conflict",
        message: "状态已更新，请刷新后重试。",
      };
    }
  }
  return { action, kind: "error", message: "操作失败，请重试。" };
}

export type ScopedAutomationActionFeedback = {
  projectId: string;
  feedback: AutomationActionFeedback;
};

export function automationFeedbackForProject(
  scoped: ScopedAutomationActionFeedback | null,
  projectId: string,
): AutomationActionFeedback | null {
  return scoped?.projectId === projectId ? scoped.feedback : null;
}

function AutomationPageSkeleton() {
  return (
    <main
      aria-busy="true"
      aria-label="正在加载 Automation"
      data-testid="automation-loading"
      className="mx-auto w-full max-w-6xl space-y-5 p-6 lg:p-8"
    >
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-12 w-full rounded-xl" />
      <div className="grid gap-4 lg:grid-cols-3">
        <Skeleton className="h-80 rounded-xl" />
        <Skeleton className="h-80 rounded-xl lg:col-span-2" />
      </div>
    </main>
  );
}

function AutomationUnavailableState({
  code,
  onRetry,
}: {
  code?: string;
  onRetry?: () => void;
}) {
  return (
    <main className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center p-6 text-center">
      <section
        data-testid="automation-unavailable"
        className="bg-card w-full rounded-2xl border p-8"
      >
        <h1 className="text-xl font-semibold">Automation 暂时不可用</h1>
        <p className="text-muted-foreground mt-3 text-sm">
          服务当前无法安全读取 Automation，请稍后重试。
        </p>
        {code ? (
          <p className="text-muted-foreground mt-2 text-xs">{code}</p>
        ) : null}
        {onRetry ? (
          <Button
            className="mt-5"
            type="button"
            variant="outline"
            onClick={onRetry}
          >
            重试
          </Button>
        ) : null}
      </section>
    </main>
  );
}

export function ProjectAutomationsPage({ project }: { project: Project }) {
  const { user } = useAuth();
  const { t } = useI18n();
  const searchParams = useSearchParams();
  const rawThreadId = searchParams.get("thread_id");
  const threadId =
    rawThreadId && UUID_PATTERN.test(rawThreadId) ? rawThreadId : null;
  const invalidThreadFilter = rawThreadId !== null && threadId === null;
  const permissions = automationPermissions(project.capabilities);
  const readiness = useProjectAutomationReadiness(permissions.canRead);
  const readinessReady =
    readiness.data?.status === "ready" &&
    readiness.data.project_private_work_ready &&
    readiness.data.schema_ready;
  const listEnabled =
    permissions.canRead && readinessReady && !invalidThreadFilter;
  const allAutomations = useProjectAutomations(
    {},
    listEnabled && threadId === null,
  );
  const threadAutomations = useThreadProjectAutomations(
    threadId,
    {},
    listEnabled && threadId !== null,
  );
  const listQuery = threadId ? threadAutomations : allAutomations;
  const automations = listQuery.data ?? [];
  const [selection, setSelection] = useState<{
    projectId: string;
    taskId: string;
  } | null>(null);
  const selectedId =
    selection?.projectId === project.id &&
    automations.some(({ id }) => id === selection.taskId)
      ? selection.taskId
      : automations[0]?.id;
  const selected = automations.find(({ id }) => id === selectedId) ?? null;
  const runs = useProjectAutomationRuns(
    selected?.id,
    {},
    listEnabled && selected !== null,
  );
  const agentsQuery = useProjectAssets(
    user?.id ?? "",
    project.id,
    "agents",
    listEnabled && permissions.canExecute && Boolean(user),
  );
  const threadsQuery = useThreads(
    {
      limit: 50,
      offset: 0,
      sortBy: "updated_at",
      sortOrder: "desc",
      select: ["thread_id", "updated_at", "values", "metadata"],
    },
    undefined,
    {
      enabled: listEnabled && permissions.canExecute && Boolean(user),
    },
  );
  const agentItems = executableProjectAgents(agentsQuery.data);
  const mcpDependencyRuntime = useAgentMcpDependencyRuntime({
    accountId: user?.id ?? "",
    projectId: project.id,
    agents: agentItems,
    enabled: listEnabled && permissions.canExecute && Boolean(user),
  });
  const agents: AutomationAgentOption[] = agentItems.flatMap((agent, index) =>
    mcpDependencyRuntime.assessments[index]?.status === "ready"
      ? [
          {
            id: agent.id,
            scope: agent.scope,
            displayName: agent.display_name,
            isDefault:
              agent.scope === "system" &&
              agent.slug === MAIN_PROJECT_AGENT_SLUG,
          },
        ]
      : [],
  );
  const threadOptions = (threadsQuery.data ?? []).map((thread) => {
    const title = titleOfThread(thread).trim();
    return {
      id: thread.thread_id,
      title: title && title !== "Untitled" ? title : "新对话",
    };
  });
  const agentRuntimeReasonByKey = new Map(
    agentItems.map((agent, index) => [
      `${agent.scope}:${agent.id}`,
      mcpDependencyRuntime.assessments[index]?.status === "ready"
        ? null
        : (mcpDependencyRuntime.assessments[index]?.reason ??
          "无法验证 Agent 的 MCP 依赖，请稍后重试。"),
    ]),
  );
  const automationAgentBlockReasons = Object.fromEntries(
    automations.flatMap((automation) => {
      const key = `${automation.agent_scope}:${automation.agent_asset_id}`;
      const reason = agentRuntimeReasonByKey.has(key)
        ? agentRuntimeReasonByKey.get(key)
        : "Automation 的 Agent 当前不可执行或无法验证 MCP 依赖。";
      return reason ? [[automation.id, reason] as const] : [];
    }),
  );

  const createAutomation = useCreateProjectAutomation();
  const updateAutomation = useUpdateProjectAutomation();
  const deleteAutomation = useDeleteProjectAutomation();
  const pauseAutomation = usePauseProjectAutomation();
  const resumeAutomation = useResumeProjectAutomation();
  const triggerAutomation = useTriggerProjectAutomation();
  const [scopedActionFeedback, setScopedActionFeedback] =
    useState<ScopedAutomationActionFeedback | null>(null);
  const actionFeedback = automationFeedbackForProject(
    scopedActionFeedback,
    project.id,
  );

  useEffect(() => {
    setScopedActionFeedback(null);
  }, [project.id]);

  if (!permissions.canRead) {
    return (
      <ProjectAccessDenied
        projectSlug={project.slug}
        area={t.project.automations}
      />
    );
  }

  const perform = async <T,>(
    action: AutomationAction,
    operation: () => Promise<T>,
  ): Promise<T> => {
    const projectId = project.id;
    setScopedActionFeedback(null);
    try {
      return await operation();
    } catch (error) {
      setScopedActionFeedback({
        projectId,
        feedback: automationActionFeedback(action, error),
      });
      throw error;
    }
  };

  if (readiness.isLoading) return <AutomationPageSkeleton />;
  if (readiness.data?.status === "unavailable" || readiness.error) {
    return (
      <AutomationUnavailableState
        code={readiness.data?.code}
        onRetry={() => void readiness.refetch()}
      />
    );
  }
  if (readiness.data?.status === "ready" && !readinessReady) {
    return (
      <AutomationUnavailableState
        code="AUTOMATION_READINESS_INCONSISTENT"
        onRetry={() => void readiness.refetch()}
      />
    );
  }
  if (invalidThreadFilter) {
    return (
      <AutomationUnavailableState code="AUTOMATION_THREAD_FILTER_INVALID" />
    );
  }
  if (listQuery.isLoading) return <AutomationPageSkeleton />;
  if (listQuery.error) {
    return (
      <AutomationUnavailableState
        code="AUTOMATION_LIST_UNAVAILABLE"
        onRetry={() => void listQuery.refetch()}
      />
    );
  }

  const isMutating = [
    createAutomation,
    updateAutomation,
    deleteAutomation,
    pauseAutomation,
    resumeAutomation,
    triggerAutomation,
  ].some(({ isPending }) => isPending);

  const refresh = async () => {
    setScopedActionFeedback(null);
    await Promise.all([
      listQuery.refetch(),
      selected ? runs.refetch() : undefined,
    ]);
  };

  return (
    <AutomationWorkbench
      key={project.id}
      projectSlug={project.slug}
      automations={automations}
      selected={selected}
      runs={runs.data ?? []}
      permissions={permissions}
      schedulerEnabled={readiness.data?.scheduler_enabled ?? false}
      agents={agents}
      threads={threadOptions}
      threadsLoading={threadsQuery.isLoading}
      threadsError={threadsQuery.error}
      agentsLoading={agentsQuery.isLoading || mcpDependencyRuntime.isLoading}
      agentsError={
        agentsQuery.error ??
        (mcpDependencyRuntime.error instanceof Error
          ? mcpDependencyRuntime.error
          : mcpDependencyRuntime.error
            ? new Error("无法验证 Agent 的 MCP 依赖")
            : null)
      }
      agentRuntimeNotice={
        agentItems.length > agents.length
          ? "部分 Agent 的 MCP 依赖不受支持或无法确认，已从可选列表移除。"
          : null
      }
      automationAgentBlockReasons={automationAgentBlockReasons}
      runsLoading={runs.isLoading}
      runsError={runs.error}
      initialThreadId={threadId ?? undefined}
      actionFeedback={actionFeedback}
      isMutating={isMutating}
      onSelect={(automation) =>
        setSelection(
          automation ? { projectId: project.id, taskId: automation.id } : null,
        )
      }
      onCreate={
        permissions.canExecute
          ? (input: CreateAutomationInput) =>
              perform("create", () => createAutomation.mutateAsync(input))
          : undefined
      }
      onUpdate={
        permissions.canManage
          ? (automation, input: UpdateAutomationInput) =>
              perform("update", () =>
                updateAutomation.mutateAsync({ taskId: automation.id, input }),
              )
          : undefined
      }
      onPause={
        permissions.canManage
          ? (automation) =>
              perform("pause", () =>
                pauseAutomation.mutateAsync({
                  taskId: automation.id,
                  expectedVersion: automation.version,
                }),
              )
          : undefined
      }
      onResume={
        permissions.canExecute
          ? (automation) =>
              perform("resume", async () => {
                const reason = automationAgentBlockReasons[automation.id];
                if (reason) throw new Error(reason);
                return resumeAutomation.mutateAsync({
                  taskId: automation.id,
                  expectedVersion: automation.version,
                });
              })
          : undefined
      }
      onTrigger={
        permissions.canExecute
          ? (automation) =>
              perform("trigger", async () => {
                const reason = automationAgentBlockReasons[automation.id];
                if (reason) throw new Error(reason);
                return triggerAutomation.mutateAsync(automation.id);
              })
          : undefined
      }
      onDelete={
        permissions.canManage
          ? (automation) =>
              perform("delete", () =>
                deleteAutomation.mutateAsync({
                  taskId: automation.id,
                  expectedVersion: automation.version,
                }),
              )
          : undefined
      }
      onRefresh={refresh}
      onRefreshRuns={() => runs.refetch()}
      onDismissFeedback={() => setScopedActionFeedback(null)}
    />
  );
}

export function ProjectAutomationsRouteClient() {
  return <ProjectAutomationsPage project={useCurrentProject()} />;
}
