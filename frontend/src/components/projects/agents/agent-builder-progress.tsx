import {
  CheckCircle2Icon,
  CircleIcon,
  Loader2Icon,
  TriangleAlertIcon,
} from "lucide-react";

import type { AgentBuilderProgressItem } from "@/core/agent-builder";
import { useI18n } from "@/core/i18n/hooks";

export function AgentBuilderProgress({
  items,
  generating,
}: {
  items: AgentBuilderProgressItem[];
  generating: boolean;
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.progress;

  return (
    <section
      aria-label={copy.stepsAria}
      className="border-border/70 bg-muted/20 rounded-2xl border p-4"
    >
      <div className="flex items-center gap-2 text-sm font-medium">
        {generating ? (
          <Loader2Icon aria-hidden className="size-4 animate-spin" />
        ) : (
          <CheckCircle2Icon aria-hidden className="size-4" />
        )}
        <span>{generating ? copy.designing : copy.steps}</span>
      </div>
      {items.length > 0 ? (
        <ol className="mt-3 space-y-2">
          {items.map((item) => (
            <li
              key={item.id}
              className="text-muted-foreground flex items-center gap-2 text-sm"
            >
              {item.status === "completed" ? (
                <CheckCircle2Icon
                  aria-hidden
                  className="text-success size-4 shrink-0"
                />
              ) : item.status === "running" ? (
                <Loader2Icon
                  aria-hidden
                  className="text-foreground size-4 shrink-0 animate-spin"
                />
              ) : item.status === "failed" ? (
                <TriangleAlertIcon
                  aria-hidden
                  className="text-destructive size-4 shrink-0"
                />
              ) : (
                <CircleIcon aria-hidden className="size-4 shrink-0" />
              )}
              <span
                className={
                  item.status === "running" ? "text-foreground" : undefined
                }
              >
                {item.label}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
