import type { Translations } from "@/core/i18n/locales/types";
import type { UserMemory } from "@/core/private-work/memory";

export type MemoryViewFilter = "all" | "facts" | "summaries";
export type MemoryFact = UserMemory["facts"][number];
export type ConfidenceLevel = "veryHigh" | "high" | "normal" | "unknown";
export type MemoryCategoryVisual =
  | "preference"
  | "work"
  | "project"
  | "context"
  | "default";

export type MemorySection = {
  title: string;
  summary: string;
  updatedAt?: string;
};

export type MemorySectionGroup = {
  title: string;
  sections: MemorySection[];
};

export type MemorySummaryPreview = MemorySection & {
  groupTitle: string;
};

export function buildMemorySectionGroups(
  memory: UserMemory,
  t: Translations,
): MemorySectionGroup[] {
  return [
    {
      title: t.settings.memory.markdown.userContext,
      sections: [
        {
          title: t.settings.memory.markdown.work,
          summary: memory.user.workContext.summary,
          updatedAt: memory.user.workContext.updatedAt,
        },
        {
          title: t.settings.memory.markdown.personal,
          summary: memory.user.personalContext.summary,
          updatedAt: memory.user.personalContext.updatedAt,
        },
        {
          title: t.settings.memory.markdown.topOfMind,
          summary: memory.user.topOfMind.summary,
          updatedAt: memory.user.topOfMind.updatedAt,
        },
      ],
    },
    {
      title: t.settings.memory.markdown.historyBackground,
      sections: [
        {
          title: t.settings.memory.markdown.recentMonths,
          summary: memory.history.recentMonths.summary,
          updatedAt: memory.history.recentMonths.updatedAt,
        },
        {
          title: t.settings.memory.markdown.earlierContext,
          summary: memory.history.earlierContext.summary,
          updatedAt: memory.history.earlierContext.updatedAt,
        },
        {
          title: t.settings.memory.markdown.longTermBackground,
          summary: memory.history.longTermBackground.summary,
          updatedAt: memory.history.longTermBackground.updatedAt,
        },
      ],
    },
  ];
}

export function countPopulatedSummaries(groups: MemorySectionGroup[]) {
  return groups.reduce(
    (count, group) =>
      count + group.sections.filter((section) => section.summary.trim()).length,
    0,
  );
}

export function getMemorySummaryPreviews(
  groups: MemorySectionGroup[],
  excludedTitle: string,
  limit = 2,
): MemorySummaryPreview[] {
  return groups
    .flatMap((group) =>
      group.sections.map((section) => ({
        ...section,
        groupTitle: group.title,
      })),
    )
    .filter(
      (section) =>
        section.title !== excludedTitle && section.summary.trim().length > 0,
    )
    .slice(0, limit);
}

export function isMemorySummaryEmpty(memory: UserMemory) {
  return [
    memory.user.workContext.summary,
    memory.user.personalContext.summary,
    memory.user.topOfMind.summary,
    memory.history.recentMonths.summary,
    memory.history.earlierContext.summary,
    memory.history.longTermBackground.summary,
  ].every((summary) => summary.trim() === "");
}

export function confidenceToLevelKey(confidence: unknown): {
  key: ConfidenceLevel;
  value?: number;
} {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) {
    return { key: "unknown" };
  }
  const value = Math.min(1, Math.max(0, confidence));
  if (value >= 0.85) return { key: "veryHigh", value };
  if (value >= 0.65) return { key: "high", value };
  return { key: "normal", value };
}

export function getMemoryCategoryVisual(
  category: string,
): MemoryCategoryVisual {
  const normalized = category.trim().toLowerCase();
  if (normalized.includes("preference") || normalized.includes("personal")) {
    return "preference";
  }
  if (normalized.includes("work")) return "work";
  if (normalized.includes("project")) return "project";
  if (normalized.includes("context")) return "context";
  return "default";
}

export function truncateFactPreview(content: string, maxLength = 140) {
  const normalized = content.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  const ellipsis = "...";
  if (maxLength <= ellipsis.length) return normalized.slice(0, maxLength);
  return `${normalized.slice(0, maxLength - ellipsis.length)}${ellipsis}`;
}

export function upperFirst(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
