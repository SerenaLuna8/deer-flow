"use client";

import { PlusIcon } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  ScheduledTaskScheduleInput,
  type ScheduleValue,
} from "@/components/workspace/scheduled-task-schedule-input";
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";
import { useI18n } from "@/core/i18n/hooks";
import {
  useCreateScheduledTask,
  useUpdateScheduledTask,
  useDeleteScheduledTask,
  usePauseScheduledTask,
  useResumeScheduledTask,
  useScheduledTaskRuns,
  useScheduledTasks,
  useTriggerScheduledTask,
  useThreadScheduledTasks,
} from "@/core/scheduled-tasks/hooks";
import { RECIPES, type Recipe } from "@/core/scheduled-tasks/recipes";
import type {
  ScheduledTask,
  ScheduledTaskRun,
} from "@/core/scheduled-tasks/types";
import { cn } from "@/lib/utils";

const NONE = "—";

function formatTimestamp(value: string | null, locale: string): string {
  if (!value) {
    return NONE;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  // Use a locale-aware short format like "2026-07-03 09:00". Future timestamps
  // (next_run_at) render as an absolute time, not a relative "ago" string.
  const intlLocale = locale === "zh-CN" ? "zh-CN" : "en-US";
  return new Intl.DateTimeFormat(intlLocale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function statusBadgeVariant(status: ScheduledTask["status"]) {
  if (status === "failed" || status === "cancelled") {
    return "destructive" as const;
  }
  if (status === "paused" || status === "completed") {
    return "secondary" as const;
  }
  if (status === "enabled" || status === "running") {
    return "default" as const;
  }
  return "outline" as const;
}

export default function ScheduledTasksPage() {
  const { t, locale } = useI18n();
  const st = t.scheduledTasks;
  const searchParams = useSearchParams();
  const threadId = searchParams.get("thread_id");
  const allTasksQuery = useScheduledTasks();
  const threadTasksQuery = useThreadScheduledTasks(threadId);
  const data = threadId ? threadTasksQuery.data : allTasksQuery.data;
  const queryError = threadId ? threadTasksQuery.error : allTasksQuery.error;
  const [createSheetOpen, setCreateSheetOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [contextMode, setContextMode] = useState<
    "fresh_thread_per_run" | "reuse_thread"
  >(threadId ? "reuse_thread" : "fresh_thread_per_run");
  const [targetThreadId, setTargetThreadId] = useState(threadId ?? "");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [createSchedule, setCreateSchedule] = useState<ScheduleValue>({
    schedule_type: "cron",
    schedule_spec: { cron: "0 9 * * *" },
    timezone: "",
  });
  const [statusFilter, setStatusFilter] = useState<
    "all" | "enabled" | "paused" | "running" | "completed" | "failed"
  >("all");
  const [typeFilter, setTypeFilter] = useState<"all" | "once" | "cron">("all");
  const [formError, setFormError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editPrompt, setEditPrompt] = useState("");
  const [editSchedule, setEditSchedule] = useState<ScheduleValue>({
    schedule_type: "cron",
    schedule_spec: { cron: "0 9 * * *" },
    timezone: "UTC",
  });
  const [createNonce, setCreateNonce] = useState(0);
  const filteredData = (data ?? []).filter((task) => {
    const statusPass = statusFilter === "all" || task.status === statusFilter;
    const typePass = typeFilter === "all" || task.schedule_type === typeFilter;
    return statusPass && typePass;
  });
  const selectedTask =
    filteredData.find((task) => task.id === selectedTaskId) ?? filteredData[0];
  const taskRunsQuery = useScheduledTaskRuns(selectedTask?.id);
  const createTask = useCreateScheduledTask();
  const updateTask = useUpdateScheduledTask(selectedTask?.id ?? "");
  const pauseTask = usePauseScheduledTask();
  const resumeTask = useResumeScheduledTask();
  const triggerTask = useTriggerScheduledTask();
  const deleteTask = useDeleteScheduledTask();

  const scheduleTypeLabel = (v: string) =>
    v === "cron"
      ? st.scheduleType.cron
      : v === "once"
        ? st.scheduleType.once
        : v;
  const statusLabel = (v: string) =>
    (st.status as Record<string, string>)[v] ?? v;
  const contextModeLabel = (v: string) =>
    v === "fresh_thread_per_run"
      ? st.context.fresh
      : v === "reuse_thread"
        ? st.context.reuse
        : v;
  const runTriggerLabel = (v: string) =>
    (st.runTrigger as Record<string, string>)[v] ?? v;
  const runStatusLabel = (v: string) =>
    (st.runStatus as Record<string, string>)[v] ?? v;
  const runSummary = (run: ScheduledTaskRun) =>
    `${runTriggerLabel(run.trigger)} · ${runStatusLabel(run.status)}`;
  const runsCountLabel = (count: number) =>
    (count === 1 ? st.detail.runsCountOne : st.detail.runsCount).replace(
      "{count}",
      String(count),
    );
  const applyRecipe = (recipe: Recipe) => {
    const labels = st.recipes[recipe.titleKey];
    setTitle(labels.title);
    setPrompt(recipe.prompt);
    setCreateSchedule(recipe.schedule);
    setContextMode("fresh_thread_per_run");
    setCreateNonce((n) => n + 1);
  };

  useEffect(() => {
    document.title = `${t.sidebar.scheduledTasks} - ${t.pages.appName}`;
  }, [t.pages.appName, t.sidebar.scheduledTasks]);

  useEffect(() => {
    if (!selectedTaskId) {
      return;
    }
    const stillVisible = filteredData.some(
      (task) => task.id === selectedTaskId,
    );
    if (!stillVisible) {
      setSelectedTaskId(filteredData[0]?.id ?? null);
      setEditing(false);
    }
  }, [filteredData, selectedTaskId]);

  useEffect(() => {
    if (!selectedTask) {
      setEditing(false);
      return;
    }
    setEditTitle(selectedTask.title);
    setEditPrompt(selectedTask.prompt);
    const spec = selectedTask.schedule_spec as {
      cron?: string;
      run_at?: string;
    };
    setEditSchedule({
      schedule_type: selectedTask.schedule_type,
      schedule_spec: {
        cron: typeof spec.cron === "string" ? spec.cron : undefined,
        run_at: typeof spec.run_at === "string" ? spec.run_at : undefined,
      },
      timezone: selectedTask.timezone || "UTC",
    });
    // Depend on id only so a background refetch (same task, new object reference)
    // does not wipe edits in progress.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTask?.id]);

  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div className="mx-auto flex w-full max-w-[1440px] min-w-0 flex-col gap-5 p-4 sm:p-6 md:p-8">
          <header className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="space-y-1">
              <h1 className="text-2xl font-semibold tracking-tight">
                {t.sidebar.scheduledTasks}
              </h1>
              {threadId ? (
                <p className="text-muted-foreground text-sm [overflow-wrap:anywhere]">
                  {st.detail.filteredByThread.replace("{id}", threadId)}
                </p>
              ) : null}
            </div>
            <Sheet open={createSheetOpen} onOpenChange={setCreateSheetOpen}>
              <SheetTrigger asChild>
                <Button data-testid="scheduled-task-create-trigger">
                  <PlusIcon className="size-4" />
                  {st.create.title}
                </Button>
              </SheetTrigger>
              <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
                <SheetHeader className="border-b px-6 py-5">
                  <SheetTitle>{st.create.title}</SheetTitle>
                  <SheetDescription>
                    {t.sidebar.scheduledTasks}
                  </SheetDescription>
                </SheetHeader>
                <div
                  className="space-y-5 px-6 pb-8"
                  data-testid="scheduled-task-create-form"
                >
                  <div
                    className="flex flex-wrap items-center gap-1"
                    data-testid="schedule-recipes"
                  >
                    <span className="text-muted-foreground text-sm">
                      {st.recipes.label}:
                    </span>
                    {RECIPES.map((recipe) => (
                      <Button
                        key={recipe.id}
                        variant="outline"
                        size="sm"
                        onClick={() => applyRecipe(recipe)}
                      >
                        <span aria-hidden>{recipe.icon}</span>
                        {st.recipes[recipe.titleKey].title}
                      </Button>
                    ))}
                  </div>
                  <div
                    className="flex gap-2"
                    role="group"
                    aria-label={st.detail.contextMode}
                  >
                    <Button
                      variant={
                        contextMode === "fresh_thread_per_run"
                          ? "default"
                          : "outline"
                      }
                      size="sm"
                      aria-pressed={contextMode === "fresh_thread_per_run"}
                      onClick={() => setContextMode("fresh_thread_per_run")}
                    >
                      {st.context.fresh}
                    </Button>
                    <Button
                      variant={
                        contextMode === "reuse_thread" ? "default" : "outline"
                      }
                      size="sm"
                      aria-pressed={contextMode === "reuse_thread"}
                      onClick={() => setContextMode("reuse_thread")}
                    >
                      {st.context.reuse}
                    </Button>
                  </div>
                  {contextMode === "reuse_thread" && (
                    <Input
                      value={targetThreadId}
                      onChange={(event) =>
                        setTargetThreadId(event.target.value)
                      }
                      placeholder={st.context.threadIdPlaceholder}
                    />
                  )}
                  <Input
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                    placeholder={st.create.taskTitle}
                  />
                  <Textarea
                    rows={4}
                    value={prompt}
                    onChange={(event) => setPrompt(event.target.value)}
                    placeholder={st.create.prompt}
                  />
                  <ScheduledTaskScheduleInput
                    key={createNonce}
                    initial={createSchedule}
                    onChange={setCreateSchedule}
                  />
                  {formError && (
                    <div className="text-destructive text-sm">{formError}</div>
                  )}
                  <Button
                    onClick={() => {
                      const hasSchedule =
                        Boolean(createSchedule.schedule_spec.cron) ||
                        Boolean(createSchedule.schedule_spec.run_at);
                      if (
                        !title ||
                        !prompt ||
                        !hasSchedule ||
                        (contextMode === "reuse_thread" && !targetThreadId)
                      ) {
                        setFormError(st.create.fillRequired);
                        return;
                      }
                      setFormError(null);
                      createTask.mutate(
                        {
                          context_mode: contextMode,
                          thread_id:
                            contextMode === "reuse_thread"
                              ? targetThreadId
                              : null,
                          title,
                          prompt,
                          schedule_type: createSchedule.schedule_type,
                          schedule_spec: createSchedule.schedule_spec,
                          timezone: createSchedule.timezone || "UTC",
                        },
                        {
                          onSuccess: () => {
                            // Clear the form so a follow-up task starts fresh.
                            setTitle("");
                            setPrompt("");
                            setTargetThreadId("");
                            setContextMode("fresh_thread_per_run");
                            setCreateSchedule({
                              schedule_type: "cron",
                              schedule_spec: { cron: "0 9 * * *" },
                              timezone: "",
                            });
                            setCreateNonce((n) => n + 1);
                            setCreateSheetOpen(false);
                          },
                        },
                      );
                    }}
                    disabled={
                      !title ||
                      !prompt ||
                      (!createSchedule.schedule_spec.cron &&
                        !createSchedule.schedule_spec.run_at) ||
                      (contextMode === "reuse_thread" && !targetThreadId) ||
                      createTask.isPending
                    }
                  >
                    {st.create.submit}
                  </Button>
                </div>
              </SheetContent>
            </Sheet>
          </header>

          {queryError ? (
            <div
              className="text-destructive text-sm"
              data-testid="scheduled-task-load-error"
            >
              {st.detail.loadFailed}: {queryError.message}
            </div>
          ) : null}

          <section className="bg-muted/25 flex min-w-0 flex-wrap gap-3 rounded-xl border p-3">
            <div
              className="flex flex-wrap items-center gap-2"
              role="group"
              aria-label={st.filters.allStatuses}
            >
              <span className="text-muted-foreground px-1 text-xs font-medium">
                {st.filters.allStatuses}
              </span>
              <Button
                variant={statusFilter === "all" ? "default" : "outline"}
                size="sm"
                aria-pressed={statusFilter === "all"}
                onClick={() => setStatusFilter("all")}
              >
                {st.filters.allStatuses}
              </Button>
              <Button
                variant={statusFilter === "enabled" ? "default" : "outline"}
                size="sm"
                aria-pressed={statusFilter === "enabled"}
                onClick={() => setStatusFilter("enabled")}
              >
                {st.filters.enabled}
              </Button>
              <Button
                variant={statusFilter === "paused" ? "default" : "outline"}
                size="sm"
                aria-pressed={statusFilter === "paused"}
                onClick={() => setStatusFilter("paused")}
              >
                {st.filters.paused}
              </Button>
              <Button
                variant={statusFilter === "completed" ? "default" : "outline"}
                size="sm"
                aria-pressed={statusFilter === "completed"}
                onClick={() => setStatusFilter("completed")}
              >
                {st.filters.completed}
              </Button>
              <Button
                variant={statusFilter === "failed" ? "default" : "outline"}
                size="sm"
                aria-pressed={statusFilter === "failed"}
                onClick={() => setStatusFilter("failed")}
              >
                {st.filters.failed}
              </Button>
            </div>
            <div
              className="flex flex-wrap items-center gap-2"
              role="group"
              aria-label={st.filters.allTypes}
            >
              <span className="text-muted-foreground px-1 text-xs font-medium">
                {st.filters.allTypes}
              </span>
              <Button
                variant={typeFilter === "all" ? "default" : "outline"}
                size="sm"
                aria-pressed={typeFilter === "all"}
                onClick={() => setTypeFilter("all")}
              >
                {st.filters.allTypes}
              </Button>
              <Button
                variant={typeFilter === "cron" ? "default" : "outline"}
                size="sm"
                aria-pressed={typeFilter === "cron"}
                onClick={() => setTypeFilter("cron")}
              >
                {st.filters.cron}
              </Button>
              <Button
                variant={typeFilter === "once" ? "default" : "outline"}
                size="sm"
                aria-pressed={typeFilter === "once"}
                onClick={() => setTypeFilter("once")}
              >
                {st.filters.once}
              </Button>
            </div>
          </section>

          <div
            className="grid min-w-0 items-start gap-4 lg:grid-cols-[minmax(280px,0.36fr)_minmax(0,0.64fr)]"
            data-testid="scheduled-task-workbench"
          >
            <section
              className="bg-card flex min-w-0 flex-col gap-3 rounded-xl border p-3 shadow-xs"
              data-testid="scheduled-task-list"
              aria-labelledby="scheduled-task-list-heading"
            >
              <h2 id="scheduled-task-list-heading" className="sr-only">
                {t.sidebar.scheduledTasks}
              </h2>
              {filteredData.map((task) => {
                const isSelected = selectedTask?.id === task.id;
                return (
                  <button
                    type="button"
                    key={task.id}
                    onClick={() => setSelectedTaskId(task.id)}
                    data-testid={`scheduled-task-item-${task.id}`}
                    aria-current={isSelected ? "true" : undefined}
                    aria-controls="scheduled-task-detail-panel"
                    className={cn(
                      "hover:bg-muted/40 min-w-0 rounded-lg border p-4 text-left [overflow-wrap:anywhere] transition-colors",
                      isSelected
                        ? "border-foreground bg-muted/50"
                        : "border-border",
                    )}
                  >
                    <div className="min-w-0 font-medium [overflow-wrap:anywhere]">
                      {task.title}
                    </div>
                    <div className="mt-2 flex min-w-0 flex-wrap gap-1.5">
                      <Badge variant="outline">
                        {scheduleTypeLabel(task.schedule_type)}
                      </Badge>
                      <Badge variant={statusBadgeVariant(task.status)}>
                        {statusLabel(task.status)}
                      </Badge>
                    </div>
                    <div className="text-muted-foreground mt-3 text-xs">
                      {st.detail.nextRun}:{" "}
                      {formatTimestamp(task.next_run_at, locale)}
                    </div>
                  </button>
                );
              })}
            </section>

            <section
              id="scheduled-task-detail-panel"
              className="bg-card min-w-0 rounded-xl border p-5 shadow-xs"
              data-testid="scheduled-task-detail"
              aria-labelledby="scheduled-task-detail-heading"
            >
              {selectedTask ? (
                <div className="flex min-w-0 flex-col gap-5">
                  <div className="flex min-w-0 items-start justify-between gap-3">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <h2
                        id="scheduled-task-detail-heading"
                        className="min-w-0 text-lg font-semibold [overflow-wrap:anywhere]"
                      >
                        {selectedTask.title}
                      </h2>
                      <Badge variant={statusBadgeVariant(selectedTask.status)}>
                        {statusLabel(selectedTask.status)}
                      </Badge>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0"
                      onClick={() => setEditing((value) => !value)}
                    >
                      {editing ? st.actions.cancelEdit : st.actions.edit}
                    </Button>
                  </div>

                  <div className="grid min-w-0 gap-x-6 gap-y-4 sm:grid-cols-2 [&>*]:min-w-0">
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.contextMode}
                      </div>
                      <div className="text-sm">
                        {contextModeLabel(selectedTask.context_mode)}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {selectedTask.context_mode === "reuse_thread"
                          ? st.detail.thread
                          : st.detail.lastThread}
                      </div>
                      <div className="text-sm [overflow-wrap:anywhere]">
                        {selectedTask.context_mode === "reuse_thread"
                          ? (selectedTask.thread_id ?? NONE)
                          : (selectedTask.last_thread_id ?? NONE)}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.schedule}
                      </div>
                      <div className="text-sm">
                        {scheduleTypeLabel(selectedTask.schedule_type)}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.nextRun}
                      </div>
                      <div className="text-sm">
                        {formatTimestamp(selectedTask.next_run_at, locale)}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.lastRun}
                      </div>
                      <div className="text-sm">
                        {formatTimestamp(selectedTask.last_run_at, locale)}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.runCount}
                      </div>
                      <div
                        className="text-sm"
                        data-testid="scheduled-task-run-count"
                      >
                        {selectedTask.run_count}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.lastRunId}
                      </div>
                      <div className="text-sm [overflow-wrap:anywhere]">
                        {selectedTask.last_run_id ?? NONE}
                      </div>
                    </div>
                    <div className="min-w-0 space-y-1 sm:col-span-2">
                      <div className="text-muted-foreground text-xs">
                        {st.detail.lastError}
                      </div>
                      <div className="text-sm [overflow-wrap:anywhere]">
                        {selectedTask.last_error ?? NONE}
                      </div>
                    </div>
                  </div>

                  {editing ? (
                    <div className="flex flex-col gap-2 rounded-lg border p-3">
                      <Input
                        value={editTitle}
                        onChange={(event) => setEditTitle(event.target.value)}
                        placeholder={st.edit.titlePlaceholder}
                      />
                      <Textarea
                        rows={4}
                        value={editPrompt}
                        onChange={(event) => setEditPrompt(event.target.value)}
                        placeholder={st.edit.promptPlaceholder}
                      />
                      <ScheduledTaskScheduleInput
                        key={selectedTask.id}
                        initial={editSchedule}
                        onChange={setEditSchedule}
                        scheduleTypeLocked
                      />
                      <Button
                        size="sm"
                        onClick={() =>
                          updateTask.mutate({
                            title: editTitle,
                            prompt: editPrompt,
                            schedule_spec: editSchedule.schedule_spec,
                            timezone: editSchedule.timezone || "UTC",
                          })
                        }
                        disabled={updateTask.isPending}
                      >
                        {st.edit.submit}
                      </Button>
                    </div>
                  ) : (
                    <div className="bg-muted/30 min-w-0 rounded-lg p-4 text-sm [overflow-wrap:anywhere]">
                      {selectedTask.prompt}
                    </div>
                  )}

                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        selectedTask.status === "paused"
                          ? resumeTask.mutate(selectedTask.id)
                          : pauseTask.mutate(selectedTask.id)
                      }
                    >
                      {selectedTask.status === "paused"
                        ? st.actions.resume
                        : st.actions.pause}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => triggerTask.mutate(selectedTask.id)}
                    >
                      {st.actions.trigger}
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => setDeleteOpen(true)}
                    >
                      {st.actions.delete}
                    </Button>
                  </div>

                  <section
                    className="min-w-0 space-y-3 border-t pt-5"
                    data-testid="scheduled-task-runs"
                    aria-labelledby="scheduled-task-runs-heading"
                  >
                    <h3
                      id="scheduled-task-runs-heading"
                      className="font-medium"
                    >
                      {runsCountLabel(selectedTask.run_count)}
                    </h3>
                    <div
                      className="flex min-w-0 flex-col gap-2"
                      data-testid="scheduled-task-run-list"
                    >
                      {(taskRunsQuery.data ?? []).length > 0 ? (
                        (taskRunsQuery.data ?? []).map((run) => (
                          <div
                            key={run.id}
                            className="min-w-0 rounded-md border p-3 text-sm [overflow-wrap:anywhere]"
                          >
                            <div className="font-medium">{runSummary(run)}</div>
                            <div className="text-muted-foreground text-xs [overflow-wrap:anywhere]">
                              {run.run_id ?? NONE}
                            </div>
                            <div className="text-muted-foreground text-xs">
                              {formatTimestamp(run.scheduled_for, locale)}
                            </div>
                            {run.error && (
                              <div className="text-destructive text-xs [overflow-wrap:anywhere]">
                                {run.error}
                              </div>
                            )}
                          </div>
                        ))
                      ) : (
                        <div className="text-muted-foreground text-sm">
                          {st.detail.noRuns}
                        </div>
                      )}
                    </div>
                  </section>
                </div>
              ) : (
                <h2
                  id="scheduled-task-detail-heading"
                  className="text-muted-foreground text-sm"
                >
                  {st.detail.noSelection}
                </h2>
              )}
            </section>
          </div>
        </div>
      </WorkspaceBody>

      {/* Delete confirm — follows the agent-card confirm pattern. */}
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{st.actions.delete}</DialogTitle>
            <DialogDescription>{st.deleteConfirm}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteTask.isPending}
            >
              {t.common.cancel}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (selectedTask) {
                  deleteTask.mutate(selectedTask.id, {
                    onSuccess: () => setDeleteOpen(false),
                  });
                }
              }}
              disabled={deleteTask.isPending}
            >
              {deleteTask.isPending ? t.common.loading : st.actions.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </WorkspaceContainer>
  );
}
