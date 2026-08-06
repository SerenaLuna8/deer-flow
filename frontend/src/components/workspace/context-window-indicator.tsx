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
import type {
  ContextUsageTrigger,
  ThreadContextUsageResponse,
} from "@/core/threads/context-usage";
import { cn } from "@/lib/utils";

const GAUGE_RADIUS = 8;
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * GAUGE_RADIUS;

type IndicatorState = "ready" | "loading" | "unavailable" | "disabled";

function percent(value: number, locale: string) {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

function triggerLabel(
  trigger: ContextUsageTrigger,
  labels: {
    tokens: string;
    fraction: string;
    messages: string;
  },
) {
  return labels[trigger.type];
}

function triggerValue(
  trigger: ContextUsageTrigger,
  value: number,
  locale: string,
  tokens: (value: string) => string,
  messages: (count: number) => string,
) {
  switch (trigger.type) {
    case "fraction":
      return percent(value, locale);
    case "messages":
      return messages(value);
    case "tokens":
      return tokens(formatTokenCount(value));
  }
}

function sameTrigger(left: ContextUsageTrigger, right: ContextUsageTrigger) {
  return (
    left.type === right.type &&
    left.configured_value === right.configured_value &&
    left.threshold_value === right.threshold_value
  );
}

function ContextGauge({
  progress,
  loading,
}: {
  progress: number;
  loading: boolean;
}) {
  const clampedProgress = Math.max(0, Math.min(100, progress));
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
  const { locale, t } = useI18n();
  const primary = usage.primary_trigger;

  if (!usage.enabled) {
    return (
      <div className="text-muted-foreground p-3 text-xs">
        {t.contextWindow.disabled}
      </div>
    );
  }
  if (!primary) {
    return (
      <div className="text-muted-foreground p-3 text-xs">
        {t.contextWindow.unavailable}
      </div>
    );
  }

  const value = (amount: number) =>
    triggerValue(
      primary,
      amount,
      locale,
      t.contextWindow.tokens,
      t.contextWindow.messages,
    );
  const renderedProgress = percent(primary.progress_percent / 100, locale);

  return (
    <div className="w-72 text-xs" data-context-window-details>
      <div className="space-y-3 p-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-medium">{t.contextWindow.title}</div>
            <div className="text-muted-foreground mt-0.5">
              {triggerLabel(primary, t.contextWindow.triggerTypes)}
            </div>
          </div>
          <span className="font-mono font-medium">{renderedProgress}</span>
        </div>
        <Progress
          aria-label={t.contextWindow.automaticCompression}
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={primary.progress_percent}
          className="h-1.5"
          value={primary.progress_percent}
        />
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1.5">
          <dt className="text-muted-foreground">{t.contextWindow.current}</dt>
          <dd className="font-mono">{value(primary.current_value)}</dd>
          <dt className="text-muted-foreground">{t.contextWindow.triggerAt}</dt>
          <dd className="font-mono">{value(primary.threshold_value)}</dd>
          <dt className="text-muted-foreground">{t.contextWindow.remaining}</dt>
          <dd className="font-mono">{value(primary.remaining_value)}</dd>
          <dt className="text-muted-foreground">
            {t.contextWindow.estimatedContext}
          </dt>
          <dd className="font-mono">
            {usage.context_window_tokens === null
              ? t.contextWindow.tokens(formatTokenCount(usage.estimated_tokens))
              : t.contextWindow.tokenPair(
                  formatTokenCount(usage.estimated_tokens),
                  formatTokenCount(usage.context_window_tokens),
                )}
          </dd>
          {"threshold_tokens" in primary && (
            <>
              <dt className="text-muted-foreground">
                {t.contextWindow.tokenThreshold}
              </dt>
              <dd className="font-mono">
                {t.contextWindow.tokens(
                  formatTokenCount(primary.threshold_tokens),
                )}
              </dd>
            </>
          )}
        </dl>
        {usage.summary_present && (
          <p className="text-muted-foreground">
            {t.contextWindow.summaryPresent}
          </p>
        )}
      </div>
      {usage.triggers.length > 1 && (
        <div className="border-t p-3">
          <div className="font-medium">{t.contextWindow.allConditions}</div>
          <p className="text-muted-foreground mt-0.5">
            {t.contextWindow.anyCondition}
          </p>
          <ul className="mt-2 space-y-1.5">
            {usage.triggers.map((trigger, index) => (
              <li
                className="flex items-center justify-between gap-3"
                key={`${trigger.type}-${trigger.configured_value}-${index}`}
              >
                <span className="min-w-0 truncate">
                  {triggerLabel(trigger, t.contextWindow.triggerTypes)}
                  {sameTrigger(trigger, primary) && (
                    <span className="text-muted-foreground ml-1">
                      · {t.contextWindow.primary}
                    </span>
                  )}
                </span>
                <span className="shrink-0 font-mono">
                  {percent(trigger.progress_percent / 100, locale)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
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
      : !usage.enabled
        ? "disabled"
        : usage.primary_trigger
          ? "ready"
          : "unavailable";
  const progress =
    state === "ready" ? (usage?.primary_trigger?.progress_percent ?? 0) : 0;
  const renderedProgress = percent(progress / 100, locale);
  const label =
    state === "loading"
      ? t.contextWindow.loading
      : state === "disabled"
        ? t.contextWindow.disabled
        : state === "unavailable"
          ? t.contextWindow.unavailable
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
          data-progress={progress}
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
