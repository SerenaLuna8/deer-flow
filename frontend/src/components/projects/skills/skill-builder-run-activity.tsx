"use client";

import {
  CheckCircle2Icon,
  CircleIcon,
  Loader2Icon,
  TriangleAlertIcon,
  WrenchIcon,
} from "lucide-react";

import { useI18n } from "@/core/i18n/hooks";
import type {
  SkillBuilderRunAdmission,
  SkillBuilderRunPresentation,
  SkillBuilderRunPresentationStatus,
  SkillBuilderRunStreamProjection,
  SkillBuilderRunToolStepProjection,
} from "@/core/skill-builder";
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
