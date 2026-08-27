"use client";

import { InfoIcon, TriangleAlertIcon } from "lucide-react";

import { useI18n } from "@/core/i18n/hooks";
import type { RunControlObservation } from "@/core/threads/tool-call-control-events";
import { cn } from "@/lib/utils";

function isHardObservation(observation: RunControlObservation): boolean {
  return (
    observation.reason_code === "repeated_call_limit" ||
    observation.reason_code === "tool_budget_exhausted" ||
    observation.reason_code === "subagent_total_limit"
  );
}

export function RunControlProgress({
  observations,
}: {
  observations: readonly RunControlObservation[];
}) {
  const { t } = useI18n();
  const visibleObservations = observations.filter(
    (observation) => observation.reason_code !== "tool_budget_warning",
  );
  if (visibleObservations.length === 0) {
    return null;
  }

  return (
    <section
      className="mb-3 flex flex-col gap-2"
      aria-label={t.conversation.toolCallControl.progressLabel}
      data-testid="run-control-progress"
    >
      {visibleObservations.map((observation) => {
        const hard = isHardObservation(observation);
        let title: string;
        let description: string;
        switch (observation.reason_code) {
          case "repeated_call_warning":
            title = t.conversation.toolCallControl.repeatedWarningTitle;
            description =
              t.conversation.toolCallControl.repeatedWarningDescription(
                observation.count_after,
                observation.hard_limit,
              );
            break;
          case "repeated_call_limit":
            title = t.conversation.toolCallControl.repeatedLimitTitle;
            description =
              t.conversation.toolCallControl.repeatedLimitDescription;
            break;
          case "tool_budget_warning": {
            const toolName =
              "tool_name" in observation && observation.tool_name
                ? observation.tool_name
                : "unknown";
            title =
              t.conversation.toolCallControl.toolBudgetWarningTitle(toolName);
            description =
              t.conversation.toolCallControl.toolBudgetWarningDescription(
                observation.count_after,
                observation.hard_limit,
              );
            break;
          }
          case "tool_budget_exhausted":
            if (
              "budget_scope" in observation &&
              observation.budget_scope === "lead"
            ) {
              title =
                t.conversation.toolCallControl.leadToolBudgetExhaustedTitle;
              description =
                t.conversation.toolCallControl
                  .leadToolBudgetExhaustedDescription;
            } else if (
              "budget_scope" in observation &&
              observation.budget_scope === "subagent_task"
            ) {
              title =
                t.conversation.toolCallControl
                  .subagentTaskToolBudgetExhaustedTitle;
              description =
                t.conversation.toolCallControl
                  .subagentTaskToolBudgetExhaustedDescription;
            } else {
              title = t.conversation.toolCallControl.toolBudgetExhaustedTitle;
              description =
                t.conversation.toolCallControl.toolBudgetExhaustedDescription;
            }
            break;
          case "subagent_total_limit":
            title = t.conversation.toolCallControl.subagentTotalLimitTitle;
            description =
              t.conversation.toolCallControl.subagentTotalLimitDescription;
            break;
        }
        const Icon = hard ? TriangleAlertIcon : InfoIcon;
        return (
          <div
            key={observation.observation_id}
            className={cn(
              "bg-background/95 flex gap-2 rounded-lg border px-3 py-2 text-sm",
              hard
                ? "border-amber-500/35 bg-amber-500/5"
                : "border-blue-500/25 bg-blue-500/5",
            )}
            data-testid="run-control-observation"
            data-reason-code={observation.reason_code}
            data-observation-id={observation.observation_id}
            data-contributing-status="true"
          >
            <Icon
              className={cn(
                "mt-0.5 size-4 shrink-0",
                hard ? "text-amber-600" : "text-blue-600",
              )}
              aria-hidden="true"
            />
            <div className="min-w-0">
              <p className="font-medium">{title}</p>
              <p className="text-muted-foreground mt-0.5">{description}</p>
            </div>
          </div>
        );
      })}
    </section>
  );
}
