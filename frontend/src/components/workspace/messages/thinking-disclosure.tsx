"use client";

import { CollapsibleContent } from "@radix-ui/react-collapsible";
import { ChevronDownIcon, LightbulbIcon } from "lucide-react";
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
      className={cn(
        "border-border/70 bg-background mb-4 overflow-hidden rounded-xl border",
        className,
      )}
      data-testid="thinking-disclosure"
      defaultOpen={false}
      isStreaming={isStreaming}
      onOpenChange={setIsOpen}
      open={isOpen}
      {...props}
    >
      <ThinkingDisclosureTrigger hasContent={children != null} />
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
        "border-border/60 bg-muted/25 text-foreground/75 border-t px-4 py-4 text-sm leading-6",
        "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-1 data-[state=open]:slide-in-from-top-1 data-[state=closed]:animate-out data-[state=open]:animate-in outline-none",
        className,
      )}
      {...props}
    >
      {children}
    </CollapsibleContent>
  );
}

function ThinkingDisclosureTrigger({ hasContent }: { hasContent: boolean }) {
  const { duration, isOpen, isStreaming, startTime } = useReasoning();

  return (
    <ReasoningTrigger
      className={cn(
        "text-foreground/75 hover:bg-muted/45 hover:text-foreground min-h-10 justify-start gap-2.5 px-4 py-2.5 font-medium",
        !hasContent && "cursor-default",
      )}
      hasContent={hasContent}
    >
      <LightbulbIcon className="text-muted-foreground size-4 shrink-0" />
      <ThinkingStatus
        duration={duration}
        isStreaming={isStreaming}
        startTime={startTime}
      />
      {hasContent && (
        <ChevronDownIcon
          className={cn(
            "text-muted-foreground ml-auto size-4 shrink-0 transition-transform duration-200",
            isOpen && "rotate-180",
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
