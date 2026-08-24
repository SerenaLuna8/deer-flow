"use client";

import { ChevronRightIcon, ListTreeIcon } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useI18n } from "@/core/i18n/hooks";

const AUTO_COLLAPSE_SETTLE_DELAY_MS = 160;

export function AssistantProcessDisclosure({
  autoCollapseOnMount = false,
  children,
  defaultOpen = false,
  stepCount,
}: {
  autoCollapseOnMount?: boolean;
  children?: ReactNode;
  defaultOpen?: boolean;
  stepCount: number;
}) {
  const { t } = useI18n();
  const shouldAutoCollapse = useRef(autoCollapseOnMount);
  const animationFrame = useRef<number | null>(null);
  const autoCollapseTimer = useRef<number | null>(null);
  const [isOpen, setIsOpen] = useState(
    () => defaultOpen || shouldAutoCollapse.current,
  );
  const [isAwaitingAutoCollapse, setIsAwaitingAutoCollapse] = useState(
    shouldAutoCollapse.current,
  );

  const cancelAutoCollapse = useCallback(() => {
    if (animationFrame.current !== null) {
      window.cancelAnimationFrame(animationFrame.current);
      animationFrame.current = null;
    }
    if (autoCollapseTimer.current !== null) {
      window.clearTimeout(autoCollapseTimer.current);
      autoCollapseTimer.current = null;
    }
  }, []);

  useEffect(() => {
    if (!shouldAutoCollapse.current) {
      return;
    }

    animationFrame.current = window.requestAnimationFrame(() => {
      animationFrame.current = null;
      autoCollapseTimer.current = window.setTimeout(() => {
        autoCollapseTimer.current = null;
        setIsAwaitingAutoCollapse(false);
        setIsOpen(false);
      }, AUTO_COLLAPSE_SETTLE_DELAY_MS);
    });

    return cancelAutoCollapse;
  }, [cancelAutoCollapse]);

  const handleOpenChange = (open: boolean) => {
    cancelAutoCollapse();
    setIsAwaitingAutoCollapse(false);
    setIsOpen(open);
  };

  if (stepCount <= 0 || children == null) {
    return null;
  }

  return (
    <Collapsible
      className="not-prose mb-3 w-full"
      data-testid="assistant-process-disclosure"
      onOpenChange={handleOpenChange}
      open={isOpen}
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
      <CollapsibleContent
        className="animate-assistant-process-collapse border-border/70 ml-1.5 space-y-3 overflow-hidden border-l py-1 pr-0 pl-5"
        data-initial-open={isAwaitingAutoCollapse ? "true" : undefined}
      >
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}
