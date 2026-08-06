import type { Locale } from "@/core/i18n";
import type { ProjectUsage } from "@/core/project-governance/usage";

export type UsageDimension = ProjectUsage["dimensions"][number]["dimension"];

const LIMIT_KEYS = {
  members: "member_limit",
  storage_bytes: "storage_bytes_limit",
  concurrent_runs: "concurrent_run_limit",
  mcp_calls_daily: "mcp_calls_daily_limit",
} as const satisfies Record<
  UsageDimension,
  keyof ProjectUsage["policy"]["effective"]
>;

export const usageViewCopy = {
  "zh-CN": {
    currentOccupancy: "当前占用",
    occupancyHint: "已使用与已预留之和",
    effectiveLimit: "当前有效上限",
    inheritedLimit: "继承平台上限",
    configuredLimit: "项目自定义上限",
    policyExplanation:
      "有效上限是当前实际生效的配额；未填写项目值时，会继承平台上限。",
    editorDescription:
      "仅可为当前项目设置更严格的上限。留空表示继承平台上限，不会创建新的配额维度。",
    configuredValue: "项目设置",
    inheritedValue: "继承平台",
    effectiveValue: "当前有效",
    lifetimeBucket: "累计",
    datedBucket: "统计日期",
    progressLabel: "配额使用进度",
    bytesInputHint: "以字节为单位填写",
  },
  "en-US": {
    currentOccupancy: "Current occupancy",
    occupancyHint: "Used plus reserved",
    effectiveLimit: "Current effective limit",
    inheritedLimit: "Inherits platform limit",
    configuredLimit: "Project-specific limit",
    policyExplanation:
      "The effective limit is what applies now. An empty project value inherits the platform limit.",
    editorDescription:
      "Project limits can only be tightened. Leave a field empty to inherit the platform limit.",
    configuredValue: "Project value",
    inheritedValue: "Platform value",
    effectiveValue: "Effective now",
    lifetimeBucket: "Lifetime",
    datedBucket: "Usage date",
    progressLabel: "Quota usage progress",
    bytesInputHint: "Enter a value in bytes",
  },
} as const;

function numberFormatter(locale: Locale, maximumFractionDigits = 0) {
  return new Intl.NumberFormat(locale, { maximumFractionDigits });
}

export function formatUsageValue(
  dimension: UsageDimension,
  value: number,
  locale: Locale,
): string {
  if (dimension !== "storage_bytes") {
    return numberFormatter(locale).format(value);
  }
  if (value === 0) return "0 B";

  const units = ["B", "KiB", "MiB", "GiB", "TiB"] as const;
  const unitIndex = Math.min(
    Math.floor(Math.log(value) / Math.log(1024)),
    units.length - 1,
  );
  const normalized = value / 1024 ** unitIndex;
  const maximumFractionDigits =
    normalized < 10 && !Number.isInteger(normalized) ? 1 : 0;
  return `${numberFormatter(locale, maximumFractionDigits).format(normalized)} ${units[unitIndex]}`;
}

function formatPercent(value: number, locale: Locale): string {
  if (value > 0 && value < 0.1) return "<0.1%";
  return `${numberFormatter(locale, 1).format(value)}%`;
}

export interface UsageDimensionPresentation {
  dimension: UsageDimension;
  used: string;
  reserved: string;
  consumed: string;
  limit: string;
  configuredLimit: string | null;
  effectiveLimit: string;
  inheritsPlatformLimit: boolean;
  progressValue: number;
  progressText: string;
  bucket: string;
}

export function describeUsageDimension(
  usage: ProjectUsage,
  dimension: UsageDimension,
  locale: Locale,
): UsageDimensionPresentation {
  const item = usage.dimensions.find(
    (candidate) => candidate.dimension === dimension,
  );
  if (!item) {
    throw new Error(`Missing usage dimension: ${dimension}`);
  }

  const limitKey = LIMIT_KEYS[dimension];
  const configured = usage.policy.configured[limitKey];
  const effective = usage.policy.effective[limitKey];
  const consumed = item.used + item.reserved;
  const rawProgress =
    item.limit === 0 ? (consumed > 0 ? 100 : 0) : (consumed / item.limit) * 100;
  const progressValue = Math.min(100, Math.round(rawProgress * 10) / 10);
  const copy = usageViewCopy[locale];

  return {
    dimension,
    used: formatUsageValue(dimension, item.used, locale),
    reserved: formatUsageValue(dimension, item.reserved, locale),
    consumed: formatUsageValue(dimension, consumed, locale),
    limit: formatUsageValue(dimension, item.limit, locale),
    configuredLimit:
      configured === null
        ? null
        : formatUsageValue(dimension, configured, locale),
    effectiveLimit: formatUsageValue(dimension, effective, locale),
    inheritsPlatformLimit: configured === null,
    progressValue,
    progressText: formatPercent(progressValue, locale),
    bucket:
      item.bucket === "lifetime"
        ? copy.lifetimeBucket
        : `${copy.datedBucket} ${item.bucket}`,
  };
}
