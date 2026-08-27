"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { Progress } from "@/components/ui/progress";
import { useI18n } from "@/core/i18n/hooks";
import { formatTokenCount } from "@/core/messages/usage";
import type { ThreadContextUsageResponse } from "@/core/threads/context-usage";
import { cn } from "@/lib/utils";

const GAUGE_RADIUS = 8;
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * GAUGE_RADIUS;

type IndicatorState = "ready" | "loading" | "unavailable";

function percent(value: number, locale: string) {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

function compressionTriggerValue(
  usage: ThreadContextUsageResponse,
  messages: (count: number) => string,
  disabled: string,
  notConfigured: string,
) {
  if (!usage.enabled) return disabled;
  const trigger = usage.primary_trigger;
  if (!trigger) return notConfigured;
  if (trigger.type === "messages") {
    return messages(trigger.threshold_value);
  }
  return formatTokenCount(trigger.threshold_tokens);
}

function contextWindowProgress(usage: ThreadContextUsageResponse) {
  if (usage.context_window_tokens === null) return null;
  const percentage =
    (usage.estimated_tokens / usage.context_window_tokens) * 100;
  const clamped = Math.max(0, Math.min(100, percentage));
  return Math.round(clamped * 100) / 100;
}

function ContextGauge({
  progress,
  loading,
}: {
  progress: number | null;
  loading: boolean;
}) {
  const clampedProgress = Math.max(0, Math.min(100, progress ?? 0));
  const dashOffset = GAUGE_CIRCUMFERENCE * (1 - clampedProgress / 100);

  return (
    <svg
      aria-hidden="true"
      className={cn("size-4", loading && "animate-spin")}
      viewBox="0 0 20 20"
    >
      <circle
        className="text-muted-foreground/25"
        cx="10"
        cy="10"
        fill="none"
        r={GAUGE_RADIUS}
        stroke="currentColor"
        strokeWidth="2"
      />
      <circle
        className="text-muted-foreground"
        cx="10"
        cy="10"
        fill="none"
        r={GAUGE_RADIUS}
        stroke="currentColor"
        strokeDasharray={`${GAUGE_CIRCUMFERENCE} ${GAUGE_CIRCUMFERENCE}`}
        strokeDashoffset={loading ? GAUGE_CIRCUMFERENCE * 0.72 : dashOffset}
        strokeLinecap="round"
        strokeWidth="2"
        style={{ transform: "rotate(-90deg)", transformOrigin: "center" }}
      />
    </svg>
  );
}

export function ContextWindowDetails({
  usage,
}: {
  usage: ThreadContextUsageResponse;
}) {
  const { t } = useI18n();
  const progress = contextWindowProgress(usage);
  const trigger = compressionTriggerValue(
    usage,
    t.contextWindow.messages,
    t.contextWindow.disabled,
    t.contextWindow.notConfigured,
  );

  return (
    <div className="w-72 text-xs" data-context-window-details>
      <div className="space-y-3 p-3">
        <div className="font-medium">{t.contextWindow.title}</div>
        <Progress
          aria-disabled={progress === null ? true : undefined}
          aria-label={
            progress === null
              ? t.contextWindow.capacityUnavailable
              : t.contextWindow.usage
          }
          aria-valuemax={progress === null ? undefined : 100}
          aria-valuemin={progress === null ? undefined : 0}
          aria-valuenow={progress ?? undefined}
          aria-valuetext={
            progress === null ? t.contextWindow.capacityUnavailable : undefined
          }
          className={cn(
            "h-1.5",
            progress === null &&
              "bg-muted [&_[data-slot=progress-indicator]]:hidden",
          )}
          data-context-progress-state={
            progress === null ? "unavailable" : "ready"
          }
          value={progress ?? 0}
        />
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1.5">
          <dt className="text-muted-foreground">
            {t.contextWindow.estimatedContext}
          </dt>
          <dd className="font-mono">
            {formatTokenCount(usage.estimated_tokens)}
          </dd>
          <dt className="text-muted-foreground">{t.contextWindow.triggerAt}</dt>
          <dd className="font-mono">{trigger}</dd>
          <dt className="text-muted-foreground">
            {t.contextWindow.contextWindowLimit}
          </dt>
          <dd className="font-mono">
            {usage.context_window_tokens === null
              ? t.contextWindow.notConfigured
              : formatTokenCount(usage.context_window_tokens)}
          </dd>
        </dl>
      </div>
    </div>
  );
}

export function ContextWindowIndicator({
  usage,
  isLoading = false,
  error,
  className,
}: {
  usage?: ThreadContextUsageResponse | null;
  isLoading?: boolean;
  error?: unknown;
  className?: string;
}) {
  const { locale, t } = useI18n();
  const [hoverOpen, setHoverOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const state: IndicatorState = isLoading
    ? "loading"
    : error || !usage
      ? "unavailable"
      : "ready";
  const progress =
    state === "ready" && usage ? contextWindowProgress(usage) : null;
  const renderedProgress =
    progress === null ? null : percent(progress / 100, locale);
  const label =
    state === "loading"
      ? t.contextWindow.loading
      : state === "unavailable" || !usage
        ? t.contextWindow.unavailable
        : renderedProgress === null
          ? t.contextWindow.usageWithoutCapacity(
              formatTokenCount(usage.estimated_tokens),
            )
          : t.contextWindow.progressLabel(renderedProgress);
  const open = hoverOpen || pinned;

  return (
    <HoverCard
      closeDelay={100}
      open={open}
      openDelay={100}
      onOpenChange={setHoverOpen}
    >
      <HoverCardTrigger asChild>
        <Button
          aria-expanded={open}
          aria-haspopup="dialog"
          aria-label={label}
          className={cn("text-muted-foreground", className)}
          data-context-window-state={state}
          data-progress={progress ?? undefined}
          size="icon-sm"
          type="button"
          variant="ghost"
          onClick={() => {
            if (pinned) {
              setPinned(false);
              setHoverOpen(false);
            } else {
              setPinned(true);
            }
          }}
        >
          <ContextGauge progress={progress} loading={state === "loading"} />
        </Button>
      </HoverCardTrigger>
      <HoverCardContent
        align="end"
        aria-label={t.contextWindow.title}
        className="w-auto overflow-hidden p-0"
        role="dialog"
        side="top"
        sideOffset={8}
        onEscapeKeyDown={() => {
          setPinned(false);
          setHoverOpen(false);
        }}
        onPointerDownOutside={() => {
          setPinned(false);
          setHoverOpen(false);
        }}
      >
        {state === "ready" && usage ? (
          <ContextWindowDetails usage={usage} />
        ) : (
          <div className="text-muted-foreground w-64 p-3 text-xs">{label}</div>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}
