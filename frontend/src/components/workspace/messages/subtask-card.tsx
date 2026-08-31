import {
  CheckCircleIcon,
  ChevronUp,
  ClipboardListIcon,
  Clock3Icon,
  Loader2Icon,
  SparklesIcon,
  TriangleAlertIcon,
  WrenchIcon,
  XCircleIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  ChainOfThought,
  ChainOfThoughtContent,
  ChainOfThoughtStep,
} from "@/components/ai-elements/chain-of-thought";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { Button } from "@/components/ui/button";
import { ShineBorder } from "@/components/ui/shine-border";
import { useI18n } from "@/core/i18n/hooks";
import { hasToolCalls } from "@/core/messages/utils";
import { useModels } from "@/core/models/hooks";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";
import { useRehypeSplitWordsIntoSpans } from "@/core/rehype";
import { streamdownPluginsWithWordAnimation } from "@/core/streamdown";
import { SafeStreamdown } from "@/core/streamdown/components";
import { fetchSubtaskSteps } from "@/core/tasks/api";
import { useSubtask, useUpdateSubtask } from "@/core/tasks/context";
import {
  formatSubtaskTokenUsage,
  resolveSubtaskModelLabel,
} from "@/core/tasks/presentation";
import { stepsForDisplay } from "@/core/tasks/steps";
import { useThreadContextUsage } from "@/core/threads/hooks";
import { explainLastToolCall } from "@/core/tools/utils";
import { cn } from "@/lib/utils";

import { CitationLink } from "../citations/citation-link";
import { ContextWindowIndicator } from "../context-window-indicator";
import { FlipDisplay } from "../flip-display";

import { MarkdownContent } from "./markdown-content";

export function SubtaskCard({
  className,
  taskId,
  threadId,
  runId,
}: {
  className?: string;
  taskId: string;
  threadId?: string;
  runId?: string;
}) {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState(true);
  const task = useSubtask(taskId)!;
  const { models, tokenUsageEnabled } = useModels();
  const approvalStatus = task.executionApproval?.status;
  const approvalIsPaused =
    approvalStatus === "pending" || approvalStatus === "approved";
  const visuallyRunning = task.status === "in_progress" && !approvalIsPaused;
  const approvalStatusLabel = approvalStatus
    ? t.executionApproval.statuses[approvalStatus]
    : undefined;
  const rehypePlugins = useRehypeSplitWordsIntoSpans(visuallyRunning);
  const updateSubtask = useUpdateSubtask();
  const privateWork = useProjectPrivateWorkScope();
  const contextUsage = useThreadContextUsage(threadId, {
    enabled: Boolean(threadId && task.executionId),
    subject: task.executionId
      ? { kind: "subagent_task", executionId: task.executionId }
      : { kind: "lead_thread" },
    privateWork,
  });
  const modelLabel = resolveSubtaskModelLabel(task.modelName, models);
  const tokenLabel = tokenUsageEnabled
    ? formatSubtaskTokenUsage(task.usage)
    : undefined;
  const runtimeUsageLabel = tokenUsageEnabled
    ? tokenLabel
      ? `${tokenLabel} ${t.tokenUsage.label}`
      : task.status === "in_progress"
        ? undefined
        : t.tokenUsage.unavailableShort
    : undefined;
  const stopReasonLabel = task.stopReason
    ? t.subtasks.stopReasons[task.stopReason]
    : undefined;

  // The card shows the subagent's step timeline (#3779): its reasoning turns
  // (AI text) interleaved with the tools it ran (by name). See stepsForDisplay
  // for what is kept/dropped.
  const displaySteps = stepsForDisplay(task.steps, task.status);

  // Backfill step history on expand for historical runs (#3779). Live runs
  // already have steps from SSE, so the `steps.length` guard skips the fetch.
  const stepsCount = task.steps?.length ?? 0;
  const backfilledRef = useRef(false);
  useEffect(() => {
    if (collapsed || backfilledRef.current || stepsCount > 0) {
      return;
    }
    if (!threadId || !runId) {
      return;
    }
    backfilledRef.current = true;
    fetchSubtaskSteps(privateWork, threadId, runId, taskId)
      .then((steps) => {
        if (steps.length > 0) {
          updateSubtask({ id: taskId, steps });
        }
      })
      .catch(() => {
        // Allow a retry on the next expand if the fetch failed.
        backfilledRef.current = false;
      });
  }, [
    collapsed,
    stepsCount,
    threadId,
    runId,
    taskId,
    updateSubtask,
    privateWork,
  ]);
  const icon = useMemo(() => {
    if (task.status === "completed") {
      return <CheckCircleIcon className="size-3" />;
    } else if (task.status === "failed") {
      return <XCircleIcon className="size-3 text-red-500" />;
    } else if (approvalIsPaused) {
      return <Clock3Icon className="size-3" />;
    } else if (task.status === "in_progress") {
      return <Loader2Icon className="size-3 animate-spin" />;
    }
  }, [approvalIsPaused, task.status]);
  return (
    <ChainOfThought
      className={cn(
        "relative w-full min-w-0 gap-2 rounded-lg border py-0",
        className,
      )}
      open={!collapsed}
      data-subtask-stop-reason={task.stopReason}
    >
      {visuallyRunning && (
        <ShineBorder
          borderWidth={1.5}
          shineColor={["#A07CFE", "#FE8FB5", "#FFBE7B"]}
        />
      )}
      <div className="bg-background/95 flex w-full min-w-0 flex-col rounded-lg">
        <div className="flex w-full min-w-0 items-center justify-between p-0.5">
          <Button
            className="min-w-0 flex-1 items-start justify-start overflow-hidden text-left"
            variant="ghost"
            onClick={() => setCollapsed(!collapsed)}
          >
            <div className="flex w-full min-w-0 items-center justify-between">
              <ChainOfThoughtStep
                className="min-w-0 flex-1 font-normal"
                label={
                  visuallyRunning ? (
                    <Shimmer
                      as="span"
                      className="max-w-full min-w-0 truncate"
                      duration={3}
                      spread={3}
                    >
                      {task.description}
                    </Shimmer>
                  ) : (
                    <span
                      className="block min-w-0 truncate"
                      title={task.description}
                    >
                      {task.description}
                    </span>
                  )
                }
                icon={<ClipboardListIcon />}
              ></ChainOfThoughtStep>
              <div className="ml-2 flex max-w-[70%] min-w-0 items-center justify-end gap-1 overflow-hidden">
                {collapsed && (
                  <div
                    className={cn(
                      "text-muted-foreground flex min-w-0 items-center gap-1 overflow-hidden text-xs font-normal",
                      task.status === "failed" ? "text-red-500 opacity-67" : "",
                    )}
                  >
                    {modelLabel && (
                      <span className="max-w-32 truncate" title={modelLabel}>
                        {modelLabel}
                      </span>
                    )}
                    {runtimeUsageLabel && (
                      <span
                        className="max-w-28 truncate"
                        title={runtimeUsageLabel}
                      >
                        {runtimeUsageLabel}
                      </span>
                    )}
                    {icon}
                    <FlipDisplay
                      className="max-w-[420px] truncate pb-1"
                      uniqueKey={task.latestMessage?.id ?? ""}
                    >
                      {visuallyRunning &&
                      task.latestMessage &&
                      hasToolCalls(task.latestMessage)
                        ? explainLastToolCall(task.latestMessage, t)
                        : (approvalStatusLabel ??
                          (stopReasonLabel
                            ? `${t.subtasks[task.status]} · ${stopReasonLabel}`
                            : t.subtasks[task.status]))}
                    </FlipDisplay>
                  </div>
                )}
                <ChevronUp
                  className={cn(
                    "text-muted-foreground size-4 shrink-0",
                    !collapsed ? "" : "rotate-180",
                  )}
                />
              </div>
            </div>
          </Button>
          {threadId && task.executionId && (
            <div className="shrink-0" data-subtask-context-usage>
              <ContextWindowIndicator
                className="size-8"
                error={contextUsage.error}
                isLoading={contextUsage.isLoading}
                usage={contextUsage.data}
              />
            </div>
          )}
        </div>
        <ChainOfThoughtContent className="px-4 pb-4">
          {task.prompt && (
            <ChainOfThoughtStep
              label={
                <SafeStreamdown
                  {...streamdownPluginsWithWordAnimation}
                  components={{ a: CitationLink }}
                >
                  {task.prompt}
                </SafeStreamdown>
              }
            ></ChainOfThoughtStep>
          )}
          {displaySteps.map((step, i) => {
            const isLastWhileRunning =
              visuallyRunning && i === displaySteps.length - 1;
            const icon = isLastWhileRunning ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : step.kind === "tool" ? (
              <WrenchIcon className="size-4" />
            ) : (
              <SparklesIcon className="size-4" />
            );
            return (
              <ChainOfThoughtStep
                key={`${step.message_index}-${i}`}
                label={
                  step.kind === "tool" ? (
                    (step.tool_name ?? t.subtasks[task.status])
                  ) : (
                    <div className="text-muted-foreground line-clamp-3 text-sm">
                      <MarkdownContent
                        content={step.text}
                        isLoading={false}
                        rehypePlugins={rehypePlugins}
                      />
                    </div>
                  )
                }
                icon={icon}
              />
            );
          })}
          {stopReasonLabel && (
            <ChainOfThoughtStep
              label={stopReasonLabel}
              icon={<TriangleAlertIcon className="size-4 text-amber-600" />}
            />
          )}
          {task.status === "completed" && (
            <>
              <ChainOfThoughtStep
                label={t.subtasks.completed}
                icon={<CheckCircleIcon className="size-4" />}
              ></ChainOfThoughtStep>
              <ChainOfThoughtStep
                label={
                  task.result ? (
                    <MarkdownContent
                      content={task.result}
                      isLoading={false}
                      rehypePlugins={rehypePlugins}
                    />
                  ) : null
                }
              ></ChainOfThoughtStep>
            </>
          )}
          {task.status === "failed" && (
            <ChainOfThoughtStep
              label={
                <div className="text-red-500">
                  {approvalStatusLabel ?? task.error ?? t.subtasks.failed}
                </div>
              }
              icon={<XCircleIcon className="size-4 text-red-500" />}
            ></ChainOfThoughtStep>
          )}
        </ChainOfThoughtContent>
      </div>
    </ChainOfThought>
  );
}
