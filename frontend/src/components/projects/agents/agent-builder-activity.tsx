import { ChevronRightIcon, Loader2Icon } from "lucide-react";

import type { AgentBuilderActivity } from "@/core/agent-builder";
import { useI18n } from "@/core/i18n/hooks";
import { SafeStreamdown } from "@/core/streamdown/components";

export function AgentBuilderActivityBlock({
  activities,
}: {
  activities: readonly AgentBuilderActivity[];
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.conversation.activity;
  const terminal = [...activities]
    .reverse()
    .find((activity) =>
      ["turn_terminal", "commit_terminal"].includes(activity.kind),
    );
  const active = terminal === undefined;
  const reasoningByAttempt = new Map<number, string>();
  for (const activity of activities) {
    if (
      activity.kind === "reasoning" &&
      activity.attempt !== null &&
      activity.payload.text
    ) {
      reasoningByAttempt.set(
        activity.attempt,
        `${reasoningByAttempt.get(activity.attempt) ?? ""}${activity.payload.text}`,
      );
    }
  }

  return (
    <details
      className="border-border/70 bg-muted/15 group ml-0 rounded-xl border px-3 py-2 sm:ml-13"
      open={active}
    >
      <summary className="text-muted-foreground flex cursor-pointer list-none items-center gap-2 text-xs">
        <ChevronRightIcon className="size-3.5 transition-transform group-open:rotate-90" />
        {active ? (
          <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
        ) : null}
        <span className="font-medium">{copy.title}</span>
        {terminal?.payload.status ? (
          <span>{copy.terminal[terminal.payload.status]}</span>
        ) : null}
        {terminal?.payload.duration_ms !== null &&
        terminal?.payload.duration_ms !== undefined ? (
          <span>{copy.duration(terminal.payload.duration_ms)}</span>
        ) : null}
      </summary>
      <div className="mt-3 space-y-3 pl-5">
        <ol className="text-muted-foreground space-y-1 text-xs">
          {activities
            .filter((activity) => activity.kind !== "reasoning")
            .map((activity) => (
              <li key={activity.seq}>
                {copy.stages[activity.kind]}
                {activity.attempt !== null
                  ? ` · ${copy.attempt(activity.attempt)}`
                  : ""}
              </li>
            ))}
        </ol>
        {[...reasoningByAttempt.entries()].map(([attempt, reasoning]) => (
          <section key={attempt} className="space-y-1.5">
            <p className="text-muted-foreground text-xs font-medium">
              {copy.reasoning(attempt)}
            </p>
            <div className="text-sm leading-6">
              <SafeStreamdown>{reasoning}</SafeStreamdown>
            </div>
          </section>
        ))}
      </div>
    </details>
  );
}
