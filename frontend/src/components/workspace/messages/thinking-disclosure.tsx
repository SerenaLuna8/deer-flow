"use client";

import { CollapsibleContent } from "@radix-ui/react-collapsible";
import { AtomIcon, ChevronRightIcon } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";

import {
  Reasoning,
  ReasoningTrigger,
  useReasoning,
  type ReasoningProps,
} from "@/components/ai-elements/reasoning";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

const AUTO_COLLAPSE_DELAY_MS = 1000;

export function ThinkingDisclosure({
  children,
  className,
  defaultOpen,
  duration: observedDuration,
  isStreaming = false,
  ...props
}: Omit<ReasoningProps, "defaultOpen" | "onOpenChange" | "open"> & {
  children?: ReactNode;
  defaultOpen?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen ?? isStreaming);
  const previousIsStreaming = useRef(isStreaming);

  useEffect(() => {
    if (isStreaming && !previousIsStreaming.current) {
      setIsOpen(true);
    }

    if (!isStreaming && previousIsStreaming.current) {
      const timer = window.setTimeout(
        () => setIsOpen(false),
        AUTO_COLLAPSE_DELAY_MS,
      );
      previousIsStreaming.current = isStreaming;
      return () => window.clearTimeout(timer);
    }

    previousIsStreaming.current = isStreaming;
  }, [isStreaming]);

  return (
    <Reasoning
      className={cn("mb-3", className)}
      data-testid="thinking-disclosure"
      defaultOpen={false}
      duration={observedDuration}
      isStreaming={isStreaming}
      onOpenChange={setIsOpen}
      open={isOpen}
      {...props}
    >
      <ThinkingDisclosureTrigger
        completedDuration={observedDuration}
        hasContent={children != null}
      />
      {children}
    </Reasoning>
  );
}

export function ThinkingDisclosureContent({
  children,
  className,
  ...props
}: ComponentProps<typeof CollapsibleContent>) {
  return (
    <CollapsibleContent
      className={cn(
        "border-border/70 text-foreground/75 mt-2 ml-1.5 border-l py-1 pr-0 pl-5 text-sm leading-6",
        "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-1 data-[state=open]:slide-in-from-top-1 data-[state=closed]:animate-out data-[state=open]:animate-in outline-none",
        className,
      )}
      {...props}
    >
      {children}
    </CollapsibleContent>
  );
}

function ThinkingDisclosureTrigger({
  completedDuration,
  hasContent,
}: {
  completedDuration?: number;
  hasContent: boolean;
}) {
  const { isOpen, isStreaming, startTime } = useReasoning();

  return (
    <ReasoningTrigger
      className={cn(
        "text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 min-h-8 w-fit justify-start gap-2 px-0 py-1 font-normal hover:bg-transparent focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none",
        !hasContent && "cursor-default",
      )}
      hasContent={hasContent}
    >
      <AtomIcon className="size-3.5 shrink-0 text-[#3964fe]" />
      <ThinkingStatus
        duration={completedDuration}
        isStreaming={isStreaming}
        startTime={startTime}
      />
      {hasContent && (
        <ChevronRightIcon
          className={cn(
            "text-muted-foreground size-3.5 shrink-0 transition-transform duration-200",
            isOpen && "rotate-90",
          )}
        />
      )}
    </ReasoningTrigger>
  );
}

function ThinkingStatus({
  duration,
  isStreaming,
  startTime,
}: {
  duration?: number;
  isStreaming: boolean;
  startTime: number | null;
}) {
  const { t } = useI18n();
  const [elapsed, setElapsed] = useState(() =>
    startTime == null
      ? 0
      : Math.max(0, Math.floor((Date.now() - startTime) / 1000)),
  );

  useEffect(() => {
    if (!isStreaming || startTime == null) {
      return;
    }

    const updateElapsed = () => {
      setElapsed(Math.max(0, Math.floor((Date.now() - startTime) / 1000)));
    };
    updateElapsed();
    const interval = window.setInterval(updateElapsed, 1000);

    return () => window.clearInterval(interval);
  }, [isStreaming, startTime]);

  if (isStreaming) {
    return (
      <span className="animate-pulse">
        {t.common.thinkingInProgress(startTime == null ? undefined : elapsed)}
      </span>
    );
  }

  return <span>{t.common.thoughtFor(duration)}</span>;
}
