"use client";

import { XIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { useI18n } from "@/core/i18n/hooks";
import { formatTokenCount } from "@/core/messages/usage";
import {
  CONTEXT_PROJECTION_LANES,
  type ContextProjectionLaneName,
  type ThreadContextProjection,
} from "@/core/threads/context-usage";
import { cn } from "@/lib/utils";

const GAUGE_RADIUS = 8;
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * GAUGE_RADIUS;

type IndicatorState = "ready" | "loading" | "unavailable";

const LANE_COLORS: Record<ContextProjectionLaneName, string> = {
  system_prompt: "bg-neutral-500",
  agent_instructions: "bg-emerald-700",
  tool_definitions: "bg-violet-600",
  skills: "bg-amber-600",
  mcp_dynamic_tools: "bg-fuchsia-700",
  subagent_definitions: "bg-blue-600",
  summarized_conversation: "bg-rose-600",
  conversation: "bg-orange-600",
  visual_media: "bg-cyan-600",
  provider_overhead: "bg-slate-600",
};

function percent(value: number, locale: string, maximumFractionDigits = 1) {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits,
  }).format(value);
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

function projectionProgress(usage: ThreadContextProjection) {
  return usage.totals.progress_percent;
}

export function ContextWindowDetails({
  usage,
  onClose,
}: {
  usage: ThreadContextProjection;
  onClose?: () => void;
}) {
  const { locale, t } = useI18n();
  const progress = projectionProgress(usage);
  const capacity = usage.totals.context_window_tokens;
  const lowerBound = usage.coverage === "partial";
  const visibleLanes = CONTEXT_PROJECTION_LANES.flatMap((laneName) => {
    const lane = usage.lanes.find((candidate) => candidate.lane === laneName);
    return lane && lane.projected_tokens > 0 ? [lane] : [];
  });
  const displayDenominator = Math.max(
    capacity ?? usage.totals.projected_tokens,
    usage.totals.projected_tokens,
    1,
  );
  const totalPrefix = lowerBound ? "≥" : "~";
  const total = `${totalPrefix}${formatTokenCount(usage.totals.projected_tokens)}`;
  const totalWithCapacity = capacity
    ? `${total} / ${formatTokenCount(capacity)} Tokens`
    : `${total} Tokens`;
  const totalWithFreshness =
    usage.freshness === "stale"
      ? `${totalWithCapacity} · ${t.contextWindow.stale}`
      : totalWithCapacity;
  const visualNotice = usage.notices.find(
    (notice) => notice.code === "VISUAL_COST_UNMEASURED",
  );

  return (
    <div
      className="w-[min(38rem,calc(100vw-2rem))] text-sm"
      data-context-window-details
    >
      <div className="space-y-4 p-4">
        <div className="flex items-center justify-between gap-4">
          <div className="text-base font-medium">{t.contextWindow.title}</div>
          {onClose && (
            <Button
              aria-label={t.contextWindow.close}
              className="text-muted-foreground -mr-2"
              size="icon-sm"
              type="button"
              variant="ghost"
              onClick={onClose}
            >
              <XIcon className="size-4" />
            </Button>
          )}
        </div>

        <div className="flex items-end justify-between gap-4">
          {progress === null ? (
            <span className="text-muted-foreground">
              {t.contextWindow.capacityUnknown}
            </span>
          ) : (
            <span className="text-base">
              {t.contextWindow.full(percent(progress / 100, locale, 0))}
            </span>
          )}
          <span
            className="font-mono text-base"
            data-context-total-bound={lowerBound ? "lower" : "approximate"}
          >
            {totalWithFreshness}
          </span>
        </div>

        {progress !== null && capacity !== null && (
          <div
            aria-label={t.contextWindow.usage}
            aria-valuemax={100}
            aria-valuemin={0}
            aria-valuenow={progress}
            className="bg-muted relative flex h-2 w-full gap-px overflow-hidden rounded-full"
            role="progressbar"
          >
            {visibleLanes.map((lane) => (
              <div
                aria-hidden="true"
                className={cn("h-full min-w-px", LANE_COLORS[lane.lane])}
                data-context-lane-segment={lane.lane}
                key={lane.lane}
                style={{
                  width: `${(lane.projected_tokens / displayDenominator) * 100}%`,
                }}
              />
            ))}
            {usage.totals.projected_tokens < capacity && (
              <div aria-hidden="true" className="bg-muted h-full flex-1" />
            )}
            {usage.compaction.threshold_tokens !== null && (
              <div
                aria-hidden="true"
                className="bg-foreground/50 absolute inset-y-0 w-px"
                data-context-compaction-marker
                style={{
                  left: `${Math.min(
                    100,
                    (usage.compaction.threshold_tokens / capacity) * 100,
                  )}%`,
                }}
              />
            )}
          </div>
        )}

        <div className="space-y-2">
          {visibleLanes.map((lane) => (
            <div
              className="grid grid-cols-[auto_1fr_auto] items-center gap-3"
              data-context-lane={lane.lane}
              key={lane.lane}
            >
              <span
                aria-hidden="true"
                className={cn("size-3 rounded-sm", LANE_COLORS[lane.lane])}
              />
              <span>{t.contextWindow.lanes[lane.lane]}</span>
              <span className="font-mono">
                {formatTokenCount(lane.projected_tokens)}
              </span>
            </div>
          ))}
        </div>

        <div className="border-border/70 text-muted-foreground grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 border-t pt-3 text-xs">
          {usage.last_provider_observation && (
            <>
              <span>{t.contextWindow.previousProviderInput}</span>
              <span className="font-mono">
                {formatTokenCount(usage.last_provider_observation.input_tokens)}
              </span>
            </>
          )}
          {usage.totals.safety_upper_bound_tokens !== null && (
            <>
              <span>{t.contextWindow.safetyBound}</span>
              <span className="font-mono">
                {formatTokenCount(usage.totals.safety_upper_bound_tokens)}
              </span>
            </>
          )}
          {usage.compaction.enabled &&
            usage.compaction.threshold_tokens !== null && (
              <>
                <span>{t.contextWindow.compactionThreshold}</span>
                <span className="font-mono">
                  {formatTokenCount(usage.compaction.threshold_tokens)}
                </span>
              </>
            )}
        </div>

        {(visualNotice !== undefined ||
          usage.freshness === "stale" ||
          capacity === null) && (
          <div className="space-y-1 text-xs text-amber-700 dark:text-amber-400">
            {visualNotice && (
              <div>
                {t.contextWindow.unmeasuredVisuals(visualNotice.count ?? 0)}
              </div>
            )}
            {usage.freshness === "stale" && <div>{t.contextWindow.stale}</div>}
            {capacity === null && <div>{t.contextWindow.capacityUnknown}</div>}
          </div>
        )}
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
  usage?: ThreadContextProjection | null;
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
    state === "ready" && usage ? projectionProgress(usage) : null;
  const label =
    state === "loading"
      ? t.contextWindow.loading
      : state === "unavailable" || !usage
        ? t.contextWindow.unavailable
        : usage.coverage === "partial"
          ? t.contextWindow.lowerBoundUsage(
              formatTokenCount(usage.totals.lower_bound_tokens),
            )
          : progress === null
            ? t.contextWindow.usageWithoutCapacity(
                formatTokenCount(usage.totals.projected_tokens),
              )
            : t.contextWindow.progressLabel(percent(progress / 100, locale));
  const open = hoverOpen || pinned;

  const close = () => {
    setPinned(false);
    setHoverOpen(false);
  };

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
            if (pinned) close();
            else setPinned(true);
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
        onEscapeKeyDown={close}
        onPointerDownOutside={close}
      >
        {state === "ready" && usage ? (
          <ContextWindowDetails usage={usage} onClose={close} />
        ) : (
          <div className="text-muted-foreground w-64 p-3 text-xs">{label}</div>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}
