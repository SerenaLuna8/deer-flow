"use client";

import { FileTextIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/workspace/tooltip";
import { useI18n } from "@/core/i18n/hooks";

export function AgentBuilderBlueprintTrigger({
  available,
  conflictCount,
  onOpen,
}: {
  available: boolean;
  conflictCount: number;
  onOpen: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.blueprint;
  if (!available) return null;

  return (
    <Tooltip content={copy.openAria}>
      <Button
        type="button"
        variant="ghost"
        className="text-muted-foreground hover:text-foreground"
        aria-label={copy.openAria}
        data-testid="agent-builder-blueprint-trigger"
        onClick={onOpen}
      >
        <FileTextIcon aria-hidden />
        <span className="hidden sm:inline">{copy.triggerLabel}</span>
        {conflictCount > 0 ? (
          <span
            className="bg-destructive/10 text-destructive min-w-5 rounded-full px-1.5 py-0.5 text-center text-[11px] font-semibold"
            aria-label={copy.conflictCount(conflictCount)}
          >
            {conflictCount}
          </span>
        ) : null}
      </Button>
    </Tooltip>
  );
}
