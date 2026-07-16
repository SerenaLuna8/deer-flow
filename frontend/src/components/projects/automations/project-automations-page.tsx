"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
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
import { useProjectAssets, type ProjectAssetList } from "@/core/shared-assets";

import { executableProjectAgents } from "../private-work/agent-selector-dialog";

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
  migrationRequired,
  code,
  onRetry,
}: {
  migrationRequired: boolean;
  code?: string;
  onRetry?: () => void;
}) {
  return (
    <main className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center p-6 text-center">
      <section
        data-testid={
          migrationRequired
            ? "automation-migration-required"
            : "automation-unavailable"
        }
        className="bg-card w-full rounded-2xl border p-8"
      >
        <h1 className="text-xl font-semibold">
          {migrationRequired
            ? "Automation 尚未完成迁移"
            : "Automation 暂时不可用"}
        </h1>
        <p className="text-muted-foreground mt-3 text-sm">
          {migrationRequired
            ? "项目 Automation 在迁移完成前保持关闭。"
            : "服务当前无法安全读取 Automation，请稍后重试。"}
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
    readiness.data.automation_cutover_ready;
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
  const agents: AutomationAgentOption[] = executableProjectAgents(
    agentsQuery.data as ProjectAssetList | undefined,
  ).map((agent) => ({
    id: agent.id,
    scope: agent.scope,
    displayName: agent.display_name,
  }));

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
  if (readiness.data?.status === "migration_required") {
    return (
      <AutomationUnavailableState
        migrationRequired
        code={readiness.data.code}
      />
    );
  }
  if (readiness.data?.status === "unavailable" || readiness.error) {
    return (
      <AutomationUnavailableState
        migrationRequired={false}
        code={readiness.data?.code}
        onRetry={() => void readiness.refetch()}
      />
    );
  }
  if (readiness.data?.status === "ready" && !readinessReady) {
    return (
      <AutomationUnavailableState
        migrationRequired={false}
        code="AUTOMATION_READINESS_INCONSISTENT"
        onRetry={() => void readiness.refetch()}
      />
    );
  }
  if (!permissions.canRead) {
    return (
      <AutomationUnavailableState
        migrationRequired={false}
        code="AUTOMATION_FORBIDDEN"
      />
    );
  }
  if (invalidThreadFilter) {
    return (
      <AutomationUnavailableState
        migrationRequired={false}
        code="AUTOMATION_THREAD_FILTER_INVALID"
      />
    );
  }
  if (listQuery.isLoading) return <AutomationPageSkeleton />;
  if (listQuery.error) {
    return (
      <AutomationUnavailableState
        migrationRequired={false}
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
      agentsLoading={agentsQuery.isLoading}
      agentsError={agentsQuery.error}
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
              perform("resume", () =>
                resumeAutomation.mutateAsync({
                  taskId: automation.id,
                  expectedVersion: automation.version,
                }),
              )
          : undefined
      }
      onTrigger={
        permissions.canExecute
          ? (automation) =>
              perform("trigger", () =>
                triggerAutomation.mutateAsync(automation.id),
              )
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
