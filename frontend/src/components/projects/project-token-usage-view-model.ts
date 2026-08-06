import type { Locale } from "@/core/i18n";
import type { ProjectTokenUsageSeries } from "@/core/project-governance/usage";

const WIDTH = 720;
const HEIGHT = 260;
const PADDING = {
  top: 18,
  right: 18,
  bottom: 38,
  left: 62,
} as const;
const SERIES_KEYS = ["total_tokens", "input_tokens", "output_tokens"] as const;

export type TokenUsageSeriesKey = (typeof SERIES_KEYS)[number];

export type TokenUsageChartPoint = {
  bucketStart: string;
  value: number;
  x: number;
  y: number;
};

export type TokenUsageChartSeries = {
  key: TokenUsageSeriesKey;
  path: string;
  points: TokenUsageChartPoint[];
};

export type TokenUsageChartModel = {
  width: number;
  height: number;
  viewBox: string;
  points: ProjectTokenUsageSeries["points"];
  plot: {
    left: number;
    right: number;
    top: number;
    bottom: number;
  };
  yMaximum: number;
  yTicks: Array<{ value: number; label: string; y: number }>;
  xTicks: Array<{ label: string; x: number }>;
  hitRegions: Array<{ index: number; x: number; width: number }>;
  series: TokenUsageChartSeries[];
};

function niceMaximum(value: number): number {
  if (value <= 4) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const multiplier =
    normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return multiplier * magnitude;
}

export function formatCompactTokenCount(value: number, locale: Locale): string {
  return new Intl.NumberFormat(locale, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function buildTokenUsageChartModel(
  data: ProjectTokenUsageSeries,
  locale: Locale,
): TokenUsageChartModel {
  const plot = {
    left: PADDING.left,
    right: WIDTH - PADDING.right,
    top: PADDING.top,
    bottom: HEIGHT - PADDING.bottom,
  };
  const plotWidth = plot.right - plot.left;
  const plotHeight = plot.bottom - plot.top;
  const yMaximum = niceMaximum(
    Math.max(
      0,
      ...data.points.flatMap((point) => SERIES_KEYS.map((key) => point[key])),
    ),
  );
  const pointX = (index: number) =>
    plot.left + (plotWidth * index) / (data.points.length - 1);
  const pointY = (value: number) =>
    plot.top + plotHeight * (1 - value / yMaximum);
  const series = SERIES_KEYS.map((key) => {
    const points = data.points.map((point, index) => ({
      bucketStart: point.bucket_start,
      value: point[key],
      x: pointX(index),
      y: pointY(point[key]),
    }));
    return {
      key,
      points,
      path: points
        .map(
          (point, index) =>
            `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`,
        )
        .join(" "),
    };
  });
  const hourFormatter = new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const xTickIndexes = [0, 6, 12, 18, 23];
  const totalPoints = series[0]!.points;

  return {
    width: WIDTH,
    height: HEIGHT,
    viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
    points: data.points,
    plot,
    yMaximum,
    yTicks: Array.from({ length: 5 }, (_, index) => {
      const value = (yMaximum * (4 - index)) / 4;
      return {
        value,
        label: formatCompactTokenCount(value, locale),
        y: pointY(value),
      };
    }),
    xTicks: xTickIndexes.map((index) => ({
      label: hourFormatter.format(new Date(data.points[index]!.bucket_start)),
      x: pointX(index),
    })),
    hitRegions: totalPoints.map((point, index) => {
      const previous = totalPoints[index - 1];
      const next = totalPoints[index + 1];
      const left = previous ? (previous.x + point.x) / 2 : plot.left;
      const right = next ? (point.x + next.x) / 2 : plot.right;
      return {
        index,
        x: left,
        width: right - left,
      };
    }),
    series,
  };
}
