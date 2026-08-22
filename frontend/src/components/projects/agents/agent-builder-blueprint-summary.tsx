"use client";

import { FileTextIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";

export function AgentBuilderBlueprintSummaryCard({
  conflictCount,
  onOpen,
}: {
  conflictCount: number;
  onOpen: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.blueprint;

  return (
    <section
      data-testid="agent-builder-blueprint-summary"
      className="border-border/70 bg-muted/15 flex flex-col gap-3 rounded-2xl border p-4 sm:flex-row sm:items-center sm:justify-between"
      aria-label={copy.readyTitle}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="bg-background flex size-10 shrink-0 items-center justify-center rounded-xl border">
          <FileTextIcon aria-hidden className="size-5" />
        </span>
        <div className="min-w-0">
          <p className="text-sm font-semibold">{copy.readyTitle}</p>
          <p className="text-muted-foreground mt-1 text-xs leading-5">
            {copy.summary(conflictCount)}
          </p>
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        className="min-h-10 shrink-0"
        onClick={onOpen}
      >
        {copy.viewBlueprint}
      </Button>
    </section>
  );
}
