import type { ProjectAutomationSchedule } from "./types";

export type RecipeTitleKey = "trending" | "news" | "issues" | "weekly";

export type Recipe = {
  id: string;
  icon: string;
  titleKey: RecipeTitleKey;
  prompt: string;
  schedule: ProjectAutomationSchedule;
};

// Front-end-only starter recipes. The schedule's timezone is left empty so the
// ScheduleInput falls back to the browser-detected timezone when applied.
// `{{repo}}` style placeholders are intentional — the user fills them in the
// prompt field after applying the recipe.
export const RECIPES: Recipe[] = [
  {
    id: "trending",
    icon: "🔥",
    titleKey: "trending",
    prompt:
      "Use web_search only (do not use web_fetch) to find today's GitHub Trending repositories. Make at most 3 web_search calls. Summarize up to 10 verified repositories; for each include the name, primary language, today's star delta when available, and a one-line description of what it is and why it is trending. If GitHub Trending or the exact daily star delta cannot be verified, state the limitation, summarize the best verified results available, and do not keep retrying. Never invent missing values. Output as a markdown list.",
    schedule: {
      schedule_type: "cron",
      schedule_spec: { cron: "0 9 * * *" },
      timezone: "",
    },
  },
  {
    id: "news",
    icon: "📰",
    titleKey: "news",
    prompt:
      "Use web_search to collect today's top tech news across AI, developer tools, infrastructure, and security. Summarize the 5 most important items: headline, source, and a one-line takeaway each. Output as a markdown list.",
    schedule: {
      schedule_type: "cron",
      schedule_spec: { cron: "0 9 * * *" },
      timezone: "",
    },
  },
  {
    id: "issues",
    icon: "🏷️",
    titleKey: "issues",
    prompt:
      "Triage the open issues in {{repo}}: list the 10 most recent, label each as bug / feature / question, flag any that look stale or high-priority, and suggest 2 that are good first issues. Replace {{repo}} with the target repository (owner/name). Output as a markdown table.",
    schedule: {
      schedule_type: "cron",
      schedule_spec: { cron: "0 9 * * *" },
      timezone: "",
    },
  },
  {
    id: "weekly",
    icon: "📅",
    titleKey: "weekly",
    prompt:
      "Compile a weekly report: what was accomplished this week, what is currently blocked, and the top 3 priorities for next week. Keep it concise and skimmable.",
    schedule: {
      schedule_type: "cron",
      schedule_spec: { cron: "0 9 * * 1" },
      timezone: "",
    },
  },
];
