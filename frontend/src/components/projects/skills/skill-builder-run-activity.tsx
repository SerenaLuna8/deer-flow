"use client";

import {
  CheckCircle2Icon,
  CircleIcon,
  Loader2Icon,
  TriangleAlertIcon,
  WrenchIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import { skillBuilderActivityTerminal } from "@/core/skill-builder";
import type {
  SkillBuilderActivity,
  SkillBuilderRunAdmission,
  SkillBuilderRunPresentation,
  SkillBuilderRunPresentationStatus,
  SkillBuilderRunStreamProjection,
  SkillBuilderRunToolStepProjection,
} from "@/core/skill-builder";
import { SafeStreamdown } from "@/core/streamdown/components";
import { cn } from "@/lib/utils";

function ToolStatusIcon({
  status,
}: {
  status: SkillBuilderRunToolStepProjection["status"];
}) {
  if (status === "running") {
    return <Loader2Icon aria-hidden className="size-3.5 animate-spin" />;
  }
  if (status === "completed") {
    return <CheckCircle2Icon aria-hidden className="text-success size-3.5" />;
  }
  if (status === "failed") {
    return (
      <TriangleAlertIcon aria-hidden className="text-destructive size-3.5" />
    );
  }
  return <CircleIcon aria-hidden className="text-muted-foreground size-3.5" />;
}

export function skillBuilderRunIsActive(
  status: SkillBuilderRunPresentationStatus,
) {
  return status === "pending" || status === "running";
}

export function SkillBuilderRunActivity({
  activeRun,
  projection,
  presentation,
  failureCode,
}: {
  activeRun?: SkillBuilderRunAdmission | null;
  projection?: SkillBuilderRunStreamProjection | null;
  presentation?: SkillBuilderRunPresentation | null;
  failureCode?: string | null;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.activity;
  const terminalPresentation =
    presentation &&
    !skillBuilderRunIsActive(presentation.status) &&
    (!activeRun || activeRun.runId === presentation.runId) &&
    (!projection || presentation.runId === projection.runId)
      ? presentation
      : null;
  const status =
    terminalPresentation?.status ??
    projection?.status ??
    activeRun?.status ??
    presentation?.status;
  if (!status) return null;

  const steps = (projection?.toolSteps ?? []).map((step) => {
    if (
      !terminalPresentation ||
      step.status === "completed" ||
      step.status === "failed"
    ) {
      return step;
    }
    return {
      ...step,
      status:
        terminalPresentation.status === "success" ? "completed" : "failed",
    } satisfies SkillBuilderRunToolStepProjection;
  });
  const active = skillBuilderRunIsActive(status);

  return (
    <details
      className="border-border/70 bg-muted/20 rounded-xl border px-3 py-2"
      data-testid="skill-builder-run-activity"
      open={active || steps.length > 0}
    >
      <summary className="flex min-h-7 cursor-pointer list-none items-center gap-2 text-xs font-medium">
        {active ? (
          <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
        ) : status === "success" ? (
          <CheckCircle2Icon aria-hidden className="text-success size-3.5" />
        ) : (
          <TriangleAlertIcon
            aria-hidden
            className="text-destructive size-3.5"
          />
        )}
        <span>{copy.run[status]}</span>
        <span className="text-muted-foreground font-normal">
          {steps.length > 0 ? copy.toolSteps(steps.length) : copy.noToolSteps}
        </span>
      </summary>

      {steps.length > 0 ? (
        <ol className="border-border/70 mt-2 space-y-1.5 border-t pt-2">
          {steps.map((step) => (
            <li
              key={step.id}
              className={cn(
                "flex items-center gap-2 text-xs",
                step.status === "failed" && "text-destructive",
              )}
            >
              <ToolStatusIcon status={step.status} />
              <WrenchIcon aria-hidden className="size-3.5" />
              <span className="min-w-0 flex-1 truncate font-mono">
                {step.toolName}
              </span>
              <span className="text-muted-foreground shrink-0">
                {copy.tool[step.status]}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
      {status === "error" && failureCode === "MODEL_OUTPUT_LIMIT" ? (
        <p
          role="alert"
          className="border-border/70 mt-2 border-t pt-2 text-xs leading-5"
        >
          {copy.outputLimit}
        </p>
      ) : null}
    </details>
  );
}

export function SkillBuilderActivityBlock({
  activities,
  onStop,
  stopPending = false,
}: {
  activities: readonly SkillBuilderActivity[];
  onStop?: () => void;
  stopPending?: boolean;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.activity;
  const terminal = skillBuilderActivityTerminal(activities);
  const active = terminal === null;
  const terminalStatus = terminal?.status ?? null;
  const startedAt = activities[0]
    ? Date.parse(activities[0].created_at)
    : Number.NaN;
  const finishedAt = terminal
    ? Date.parse(terminal.activity.created_at)
    : Number.NaN;
  const durationMs =
    Number.isFinite(startedAt) && Number.isFinite(finishedAt)
      ? Math.max(0, finishedAt - startedAt)
      : null;
  const timeline: Array<
    | { kind: "activity"; activity: SkillBuilderActivity }
    | { kind: "reasoning"; seq: string; attempt: number | null; text: string }
  > = [];
  for (const activity of activities) {
    if (activity.kind === "reasoning" && activity.payload.text) {
      const previous = timeline.at(-1);
      if (
        previous?.kind === "reasoning" &&
        previous.attempt === activity.attempt
      ) {
        previous.text += activity.payload.text;
      } else {
        timeline.push({
          kind: "reasoning",
          seq: activity.seq,
          attempt: activity.attempt,
          text: activity.payload.text,
        });
      }
    } else {
      timeline.push({ kind: "activity", activity });
    }
  }

  return (
    <details
      className="border-border/70 bg-muted/15 group ml-0 rounded-xl border px-3 py-2 sm:ml-13"
      open={active}
      data-testid="skill-builder-activity"
    >
      <summary className="text-muted-foreground flex min-h-8 cursor-pointer list-none items-center gap-2 text-xs">
        {active ? (
          <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
        ) : terminalStatus === "completed" ? (
          <CheckCircle2Icon aria-hidden className="text-success size-3.5" />
        ) : (
          <TriangleAlertIcon aria-hidden className="size-3.5" />
        )}
        <span className="font-medium">{copy.title}</span>
        {terminalStatus ? <span>{copy.terminal[terminalStatus]}</span> : null}
        {durationMs !== null ? <span>{copy.duration(durationMs)}</span> : null}
        {active && onStop ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="ml-auto h-7 px-2 text-xs"
            disabled={stopPending}
            onClick={(event) => {
              event.preventDefault();
              onStop();
            }}
          >
            {stopPending ? copy.stopping : copy.stop}
          </Button>
        ) : null}
      </summary>
      <div className="mt-3 space-y-3 pl-5">
        {timeline.map((item) => {
          if (item.kind === "reasoning") {
            return (
              <section key={item.seq} className="space-y-1.5">
                <p className="text-muted-foreground text-xs font-medium">
                  {item.attempt === null
                    ? copy.title
                    : copy.reasoning(item.attempt)}
                </p>
                <div className="text-sm leading-6">
                  <SafeStreamdown>{item.text}</SafeStreamdown>
                </div>
              </section>
            );
          }
          const { activity } = item;
          const toolDetails =
            activity.kind.startsWith("tool_") && "tool_name" in activity.payload
              ? [
                  typeof activity.payload.resource_name === "string"
                    ? activity.payload.resource_name
                    : undefined,
                  typeof activity.payload.path === "string"
                    ? activity.payload.path
                    : undefined,
                  typeof activity.payload.result_count === "number"
                    ? copy.resultCount(activity.payload.result_count)
                    : undefined,
                  typeof activity.payload.size_bytes === "number"
                    ? copy.sizeBytes(activity.payload.size_bytes)
                    : undefined,
                ].filter((value): value is string => Boolean(value))
              : [];
          return (
            <p key={activity.seq} className="text-muted-foreground text-xs">
              {copy.stages[activity.kind]}
              {activity.kind.startsWith("tool_") &&
              "tool_name" in activity.payload
                ? ` · ${activity.payload.tool_name}`
                : ""}
              {toolDetails.length > 0 ? ` · ${toolDetails.join(" · ")}` : ""}
              {activity.attempt !== null
                ? ` · ${copy.attempt(activity.attempt)}`
                : ""}
            </p>
          );
        })}
      </div>
    </details>
  );
}
