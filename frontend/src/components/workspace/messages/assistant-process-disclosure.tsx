"use client";

import { ChevronRightIcon, ListTreeIcon } from "lucide-react";
import type { ReactNode } from "react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useI18n } from "@/core/i18n/hooks";

export function AssistantProcessDisclosure({
  children,
  defaultOpen = false,
  stepCount,
}: {
  children?: ReactNode;
  defaultOpen?: boolean;
  stepCount: number;
}) {
  const { t } = useI18n();

  if (stepCount <= 0 || children == null) {
    return null;
  }

  return (
    <Collapsible
      className="not-prose mb-3 w-full"
      data-testid="assistant-process-disclosure"
      defaultOpen={defaultOpen}
    >
      <CollapsibleTrigger className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 group flex min-h-8 w-fit items-center gap-2 py-1 text-sm font-normal transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none">
        <ListTreeIcon className="size-3.5 text-[#3964fe]" />
        <span>{t.toolCalls.executionDetails}</span>
        <span className="text-muted-foreground/70" aria-hidden="true">
          ·
        </span>
        <span>{t.toolCalls.stepCount(stepCount)}</span>
        <ChevronRightIcon className="size-3.5 transition-transform duration-200 group-data-[state=open]:rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent className="border-border/70 data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-1 data-[state=open]:slide-in-from-top-1 data-[state=closed]:animate-out data-[state=open]:animate-in ml-1.5 space-y-3 border-l py-1 pr-0 pl-5">
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}
