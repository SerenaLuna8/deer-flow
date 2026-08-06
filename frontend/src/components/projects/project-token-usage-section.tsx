"use client";

import { ChartNoAxesCombinedIcon, Clock3Icon } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useI18n } from "@/core/i18n/hooks";
import type { Locale } from "@/core/i18n/locale";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  useProjectTokenUsageSeries,
  type ProjectTokenUsageSeries,
} from "@/core/project-governance/usage";

import {
  buildTokenUsageChartModel,
  type TokenUsageSeriesKey,
} from "./project-token-usage-view-model";

export type ProjectTokenUsageState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: ProjectTokenUsageSeries };

const SERIES_COLORS: Record<TokenUsageSeriesKey, string> = {
  total_tokens: "var(--chart-1)",
  input_tokens: "var(--chart-2)",
  output_tokens: "var(--chart-3)",
};

function formatDateTime(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function ProjectTokenUsageStateView({
  state,
  onRetry,
}: {
  state: ProjectTokenUsageState;
  onRetry?: () => void;
}) {
  const { locale, t } = useI18n();
  const labels = t.project.governance.tokenSeries;
  const sectionTitleId = useId();
  const chartTitleId = useId();
  const chartDescriptionId = useId();
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);
  const [keyboardIndex, setKeyboardIndex] = useState(23);
  const pointButtonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    const clearHoverOutsideChartPoint = (event: PointerEvent) => {
      if (
        !(event.target instanceof Element) ||
        !event.target.closest("[data-token-usage-index]")
      ) {
        setHoverIndex(null);
      }
    };
    window.addEventListener("pointermove", clearHoverOutsideChartPoint, {
      passive: true,
    });
    return () =>
      window.removeEventListener("pointermove", clearHoverOutsideChartPoint);
  }, []);

  if (state.status === "loading") {
    return (
      <Card
        data-testid="project-token-usage"
        aria-busy="true"
        aria-label={labels.loading}
      >
        <CardHeader>
          <CardTitle>{labels.title}</CardTitle>
          <CardDescription>{labels.description}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Skeleton className="h-20 w-full rounded-xl" />
          <Skeleton className="h-64 w-full rounded-xl" />
        </CardContent>
      </Card>
    );
  }

  if (state.status === "error") {
    return (
      <Card
        data-testid="project-token-usage"
        className="border-destructive/30"
        role="alert"
      >
        <CardHeader>
          <CardTitle>{labels.unavailableTitle}</CardTitle>
          <CardDescription>{labels.unavailableDescription}</CardDescription>
        </CardHeader>
        {onRetry ? (
          <CardContent>
            <Button type="button" variant="outline" onClick={onRetry}>
              {t.project.governance.retry}
            </Button>
          </CardContent>
        ) : null}
      </Card>
    );
  }

  const { data } = state;
  const chart = buildTokenUsageChartModel(data, locale);
  const numberFormatter = new Intl.NumberFormat(locale);
  const lineLabels: Record<TokenUsageSeriesKey, string> = {
    total_tokens: t.tokenUsage.total,
    input_tokens: t.tokenUsage.input,
    output_tokens: t.tokenUsage.output,
  };
  const isEmpty = (
    ["input_tokens", "output_tokens", "total_tokens"] as const
  ).every((field) => data.totals[field] === 0);
  const windowLabel = `${formatDateTime(data.window_start, locale)} – ${formatDateTime(data.window_end, locale)}`;
  const totalSeries = chart.series.find(
    (series) => series.key === "total_tokens",
  )!;
  const activeIndex = hoverIndex ?? focusIndex;
  const activePoint =
    activeIndex === null ? null : (totalSeries.points[activeIndex] ?? null);

  return (
    <Card
      data-testid="project-token-usage"
      role="region"
      aria-labelledby={sectionTitleId}
    >
      <CardHeader className="gap-4 sm:grid-cols-[1fr_auto]">
        <div className="flex min-w-0 items-start gap-3">
          <span className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-xl">
            <ChartNoAxesCombinedIcon aria-hidden className="size-5" />
          </span>
          <div className="min-w-0">
            <CardTitle id={sectionTitleId}>{labels.title}</CardTitle>
            <CardDescription className="mt-1.5">
              {labels.description}
            </CardDescription>
          </div>
        </div>
        <div className="text-muted-foreground flex flex-col items-start gap-1 text-xs sm:items-end">
          <span className="bg-muted inline-flex items-center gap-1.5 rounded-full px-3 py-1 font-medium">
            <Clock3Icon aria-hidden className="size-3.5" />
            {labels.window}
          </span>
          <span>{windowLabel}</span>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <dl className="grid gap-3 sm:grid-cols-3">
          {(
            [
              ["total_tokens", t.tokenUsage.total],
              ["input_tokens", t.tokenUsage.input],
              ["output_tokens", t.tokenUsage.output],
            ] as const
          ).map(([field, label]) => (
            <div
              key={field}
              className="bg-muted/45 rounded-xl border px-4 py-3"
            >
              <dt className="text-muted-foreground flex items-center gap-2 text-xs">
                <span
                  aria-hidden
                  className="size-2 rounded-full"
                  style={{ backgroundColor: SERIES_COLORS[field] }}
                />
                {label}
              </dt>
              <dd className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">
                {numberFormatter.format(data.totals[field])}
              </dd>
            </div>
          ))}
        </dl>

        {isEmpty ? (
          <div className="bg-muted/35 rounded-xl border px-4 py-3">
            <p className="text-sm font-medium">{labels.emptyTitle}</p>
            <p className="text-muted-foreground mt-1 text-xs">
              {labels.emptyDescription}
            </p>
          </div>
        ) : null}

        <div>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div
              aria-label={labels.chartLabel}
              className="flex flex-wrap gap-x-4 gap-y-2 text-xs"
            >
              {chart.series.map((series) => (
                <span
                  key={series.key}
                  className="text-muted-foreground inline-flex items-center gap-1.5"
                >
                  <span
                    aria-hidden
                    className="h-0.5 w-5 rounded-full"
                    style={{
                      backgroundColor: SERIES_COLORS[series.key],
                    }}
                  />
                  {lineLabels[series.key]}
                </span>
              ))}
            </div>
            <div className="text-muted-foreground text-xs sm:text-right">
              <p>{labels.settlementNote}</p>
              <p>{labels.interactionHint}</p>
            </div>
          </div>

          <div className="max-w-full overflow-x-auto pb-1">
            <div className="relative min-w-[640px]">
              <svg
                role="img"
                aria-labelledby={`${chartTitleId} ${chartDescriptionId}`}
                viewBox={chart.viewBox}
                className="h-auto w-full"
              >
                <title id={chartTitleId}>{labels.chartLabel}</title>
                <desc id={chartDescriptionId}>
                  {`${labels.window}。${t.tokenUsage.total} ${numberFormatter.format(data.totals.total_tokens)}，${t.tokenUsage.input} ${numberFormatter.format(data.totals.input_tokens)}，${t.tokenUsage.output} ${numberFormatter.format(data.totals.output_tokens)}。`}
                </desc>

                {chart.yTicks.map((tick) => (
                  <g key={`${tick.value}-${tick.y}`}>
                    <line
                      x1={chart.plot.left}
                      x2={chart.plot.right}
                      y1={tick.y}
                      y2={tick.y}
                      stroke="currentColor"
                      className="text-border"
                      strokeWidth="1"
                      vectorEffect="non-scaling-stroke"
                    />
                    <text
                      x={chart.plot.left - 10}
                      y={tick.y}
                      fill="currentColor"
                      className="text-muted-foreground text-[12px]"
                      dominantBaseline="middle"
                      textAnchor="end"
                    >
                      {tick.label}
                    </text>
                  </g>
                ))}

                {chart.xTicks.map((tick, index) => (
                  <text
                    key={`${tick.x}-${tick.label}`}
                    x={tick.x}
                    y={chart.height - 10}
                    fill="currentColor"
                    className="text-muted-foreground text-[12px]"
                    textAnchor={
                      index === 0
                        ? "start"
                        : index === chart.xTicks.length - 1
                          ? "end"
                          : "middle"
                    }
                  >
                    {tick.label}
                  </text>
                ))}

                {[...chart.series].reverse().map((series) => (
                  <path
                    key={series.key}
                    d={series.path}
                    fill="none"
                    stroke={SERIES_COLORS[series.key]}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={series.key === "total_tokens" ? 3 : 2}
                    strokeOpacity={
                      series.key === "total_tokens" ? undefined : 0.85
                    }
                    vectorEffect="non-scaling-stroke"
                  />
                ))}

                {activePoint ? (
                  <line
                    x1={activePoint.x}
                    x2={activePoint.x}
                    y1={chart.plot.top}
                    y2={chart.plot.bottom}
                    stroke="currentColor"
                    className="text-foreground/35"
                    strokeDasharray="4 4"
                    strokeWidth="1"
                    vectorEffect="non-scaling-stroke"
                  />
                ) : null}

                {totalSeries.points.map((point) => (
                  <circle
                    key={point.bucketStart}
                    cx={point.x}
                    cy={point.y}
                    r={isEmpty ? 2 : 3}
                    fill={SERIES_COLORS.total_tokens}
                    stroke="var(--card)"
                    strokeWidth="1.5"
                    vectorEffect="non-scaling-stroke"
                    pointerEvents="none"
                  />
                ))}

                {activeIndex === null
                  ? null
                  : chart.series.map((series) => {
                      const point = series.points[activeIndex];
                      return point ? (
                        <circle
                          key={`active-${series.key}`}
                          cx={point.x}
                          cy={point.y}
                          r={series.key === "total_tokens" ? 5 : 4}
                          fill={SERIES_COLORS[series.key]}
                          stroke="var(--card)"
                          strokeWidth="2"
                          vectorEffect="non-scaling-stroke"
                          pointerEvents="none"
                        />
                      ) : null;
                    })}
              </svg>

              <div className="pointer-events-none absolute inset-0">
                {chart.hitRegions.map((region) => {
                  const bucket = data.points[region.index]!;
                  const tooltipLabel = `${formatDateTime(bucket.bucket_start, locale)} · ${t.tokenUsage.total} ${numberFormatter.format(bucket.total_tokens)} · ${t.tokenUsage.input} ${numberFormatter.format(bucket.input_tokens)} · ${t.tokenUsage.output} ${numberFormatter.format(bucket.output_tokens)}`;
                  return (
                    <Tooltip
                      key={bucket.bucket_start}
                      open={activeIndex === region.index}
                    >
                      <TooltipTrigger asChild>
                        <button
                          ref={(node) => {
                            pointButtonRefs.current[region.index] = node;
                          }}
                          type="button"
                          data-token-usage-index={region.index}
                          className="pointer-events-auto absolute cursor-crosshair rounded-sm bg-transparent outline-none"
                          style={{
                            left: `${(region.x / chart.width) * 100}%`,
                            top: `${(chart.plot.top / chart.height) * 100}%`,
                            width: `${(region.width / chart.width) * 100}%`,
                            height: `${((chart.plot.bottom - chart.plot.top) / chart.height) * 100}%`,
                          }}
                          tabIndex={keyboardIndex === region.index ? 0 : -1}
                          aria-label={tooltipLabel}
                          onPointerMove={() => setHoverIndex(region.index)}
                          onPointerLeave={() =>
                            setHoverIndex((current) =>
                              current === region.index ? null : current,
                            )
                          }
                          onFocus={() => {
                            setKeyboardIndex(region.index);
                            setFocusIndex(region.index);
                          }}
                          onBlur={() =>
                            setFocusIndex((current) =>
                              current === region.index ? null : current,
                            )
                          }
                          onKeyDown={(event) => {
                            let nextIndex: number | null = null;
                            if (event.key === "ArrowLeft") {
                              nextIndex = Math.max(0, region.index - 1);
                            } else if (event.key === "ArrowRight") {
                              nextIndex = Math.min(
                                chart.hitRegions.length - 1,
                                region.index + 1,
                              );
                            } else if (event.key === "Home") {
                              nextIndex = 0;
                            } else if (event.key === "End") {
                              nextIndex = chart.hitRegions.length - 1;
                            }
                            if (nextIndex === null) return;
                            event.preventDefault();
                            setKeyboardIndex(nextIndex);
                            pointButtonRefs.current[nextIndex]?.focus();
                          }}
                        />
                      </TooltipTrigger>
                      <TooltipContent
                        side="top"
                        sideOffset={8}
                        data-testid="project-token-usage-tooltip"
                        className="min-w-48 px-3 py-2.5"
                      >
                        <time
                          dateTime={bucket.bucket_start}
                          className="font-medium"
                        >
                          {formatDateTime(bucket.bucket_start, locale)}
                        </time>
                        <dl className="mt-2 space-y-1.5 tabular-nums">
                          {(
                            [
                              ["total_tokens", t.tokenUsage.total],
                              ["input_tokens", t.tokenUsage.input],
                              ["output_tokens", t.tokenUsage.output],
                            ] as const
                          ).map(([field, label]) => (
                            <div
                              key={field}
                              className="flex items-center justify-between gap-5"
                            >
                              <dt className="flex items-center gap-1.5 opacity-75">
                                <span
                                  aria-hidden
                                  className="size-2 rounded-full"
                                  style={{
                                    backgroundColor: SERIES_COLORS[field],
                                  }}
                                />
                                {label}
                              </dt>
                              <dd className="font-medium">
                                {numberFormatter.format(bucket[field])}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      </TooltipContent>
                    </Tooltip>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="sr-only">
            <table>
              <caption>{labels.tableCaption}</caption>
              <thead>
                <tr>
                  <th scope="col">{labels.bucket}</th>
                  <th scope="col">{t.tokenUsage.input}</th>
                  <th scope="col">{t.tokenUsage.output}</th>
                  <th scope="col">{t.tokenUsage.total}</th>
                </tr>
              </thead>
              <tbody>
                {data.points.map((point) => (
                  <tr key={point.bucket_start}>
                    <th scope="row">
                      <time dateTime={point.bucket_start}>
                        {formatDateTime(point.bucket_start, locale)}
                      </time>
                    </th>
                    <td>{point.input_tokens}</td>
                    <td>{point.output_tokens}</td>
                    <td>{point.total_tokens}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function ProjectTokenUsageSection() {
  const access = usePrivateWorkAccess();
  const query = useProjectTokenUsageSeries(access.scope);

  if (query.isLoading) {
    return <ProjectTokenUsageStateView state={{ status: "loading" }} />;
  }
  if (query.error || !query.data) {
    return (
      <ProjectTokenUsageStateView
        state={{ status: "error" }}
        onRetry={() => void query.refetch()}
      />
    );
  }
  return (
    <ProjectTokenUsageStateView state={{ status: "ready", data: query.data }} />
  );
}
