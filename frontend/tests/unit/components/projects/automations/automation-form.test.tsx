import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  applyAutomationRecipeToDraft,
  AutomationForm,
  buildAutomationFormSubmission,
  type AutomationFormDraft,
} from "@/components/projects/automations/automation-form";
import { I18nProvider } from "@/core/i18n/context";
import { RECIPES } from "@/core/project-automations/schedule/recipes";
import type { Automation } from "@/core/project-automations/types";

const AGENT = {
  id: "22222222-2222-4222-8222-222222222222",
  scope: "project" as const,
  displayName: "Research Agent",
};
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const NOW = new Date("2026-07-16T00:00:00Z");

function draft(patch: Partial<AutomationFormDraft> = {}): AutomationFormDraft {
  return {
    title: "  Daily review  ",
    prompt: "  Review the project.  ",
    contextMode: "fresh_thread_per_run",
    threadId: "",
    agentAssetId: AGENT.id,
    agentScope: AGENT.scope,
    schedule: {
      schedule_type: "cron",
      schedule_spec: { cron: "0 9 * * *" },
      timezone: "Asia/Shanghai",
    },
    ...patch,
  };
}

function automation(patch: Partial<Automation> = {}): Automation {
  return {
    id: "automation-1",
    thread_id: THREAD_ID,
    context_mode: "reuse_thread",
    agent_asset_id: AGENT.id,
    agent_scope: AGENT.scope,
    title: "Daily review",
    prompt: "Review the project.",
    schedule_type: "cron",
    schedule_spec: { cron: "0 9 * * *" },
    timezone: "Asia/Shanghai",
    status: "enabled",
    next_run_at: "2026-07-17T01:00:00Z",
    last_run_at: null,
    last_outcome: null,
    last_error_code: null,
    run_count: 0,
    version: 4,
    created_at: "2026-07-16T00:00:00Z",
    updated_at: "2026-07-16T00:00:00Z",
    ...patch,
  };
}

describe("AutomationForm", () => {
  test("builds a trimmed create payload without client authority fields", () => {
    expect(
      buildAutomationFormSubmission({ mode: "create", draft: draft() }, NOW),
    ).toEqual({
      ok: true,
      input: {
        title: "Daily review",
        prompt: "Review the project.",
        context_mode: "fresh_thread_per_run",
        thread_id: null,
        agent_asset_id: AGENT.id,
        agent_scope: "project",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "Asia/Shanghai",
      },
    });
  });

  test.each([
    ["title", draft({ title: "   " })],
    ["title length", draft({ title: "x".repeat(256) })],
    ["prompt", draft({ prompt: "   " })],
    ["Agent", draft({ agentAssetId: "" })],
    ["Thread", draft({ contextMode: "reuse_thread", threadId: "not-a-uuid" })],
    [
      "未来",
      draft({
        schedule: {
          schedule_type: "once",
          schedule_spec: { run_at: "2026-07-15T00:00:00Z" },
          timezone: "UTC",
        },
      }),
    ],
    [
      "Cron",
      draft({
        schedule: {
          schedule_type: "cron",
          schedule_spec: { cron: "0 9 * *" },
          timezone: "UTC",
        },
      }),
    ],
    [
      "时区",
      draft({
        schedule: {
          schedule_type: "cron",
          schedule_spec: { cron: "0 9 * * *" },
          timezone: "   ",
        },
      }),
    ],
    [
      "时区 length",
      draft({
        schedule: {
          schedule_type: "cron",
          schedule_spec: { cron: "0 9 * * *" },
          timezone: "x".repeat(65),
        },
      }),
    ],
  ])("rejects invalid %s input", (_field, value) => {
    expect(
      buildAutomationFormSubmission({ mode: "create", draft: value }, NOW),
    ).toEqual(expect.objectContaining({ ok: false }));
  });

  test("requires a UUID Thread in reuse mode and locks immutable edit fields", () => {
    const initial = automation();
    const result = buildAutomationFormSubmission(
      {
        mode: "edit",
        initial,
        draft: draft({ contextMode: "reuse_thread", threadId: THREAD_ID }),
      },
      NOW,
    );
    expect(result).toEqual({
      ok: true,
      input: {
        expected_version: 4,
      },
    });

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <AutomationForm
          mode="edit"
          initial={initial}
          agents={[AGENT]}
          canSubmit
          onSubmit={() => undefined}
        />
      </I18nProvider>,
    );
    expect(html).toContain('data-testid="automation-context-mode"');
    expect(html).toContain('data-testid="automation-agent"');
    expect(html).toContain("disabled");
  });

  test("builds a sparse title-only PATCH for a near-execution once Automation", () => {
    const initial = automation({
      title: "Near once",
      schedule_type: "once",
      schedule_spec: { run_at: "2026-07-16T00:00:30Z" },
      timezone: "UTC",
      next_run_at: "2026-07-16T00:00:30Z",
    });

    expect(
      buildAutomationFormSubmission(
        {
          mode: "edit",
          initial,
          draft: draft({
            title: "Near once edited",
            prompt: initial.prompt,
            contextMode: initial.context_mode,
            threadId: initial.thread_id ?? "",
            agentAssetId: initial.agent_asset_id,
            agentScope: initial.agent_scope,
            schedule: {
              schedule_type: "once",
              schedule_spec: { run_at: "2026-07-16T00:00:30+00:00" },
              timezone: "UTC",
            },
          }),
        },
        NOW,
      ),
    ).toEqual({
      ok: true,
      input: {
        expected_version: 4,
        title: "Near once edited",
      },
    });
  });

  test("keeps reusable schedule controls from submitting the parent form", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/automations/automation-schedule-input.tsx",
      ),
      "utf8",
    );

    expect(source.match(/type="button"/gu)).toHaveLength(3);
  });

  test.each([
    ["trending", "daily", "0 9 * * *"],
    ["weekly", "weekly", "0 9 * * 1"],
  ] as const)(
    "applies the %s recipe schedule with the current visible timezone",
    (recipeId, _preset, cron) => {
      const recipe = RECIPES.find(({ id }) => id === recipeId);
      expect(recipe).toBeDefined();

      const next = applyAutomationRecipeToDraft(
        draft({
          schedule: {
            schedule_type: "cron",
            schedule_spec: { cron: "15 6 * * 2" },
            timezone: "Europe/Berlin",
          },
        }),
        recipe!,
        "UTC",
      );

      expect(next.schedule).toEqual({
        schedule_type: "cron",
        schedule_spec: { cron },
        timezone: "Europe/Berlin",
      });
      expect(
        buildAutomationFormSubmission({ mode: "create", draft: next }, NOW),
      ).toEqual(
        expect.objectContaining({
          ok: true,
          input: expect.objectContaining({
            schedule_spec: { cron },
            timezone: "Europe/Berlin",
          }),
        }),
      );
    },
  );

  test("falls back from an invalid timezone without mutating a recipe", () => {
    const recipe = RECIPES[0]!;
    const original = structuredClone(recipe.schedule);

    const next = applyAutomationRecipeToDraft(
      draft({
        schedule: {
          schedule_type: "cron",
          schedule_spec: { cron: "0 9 * * *" },
          timezone: "Mars/Olympus",
        },
      }),
      recipe,
      "Asia/Tokyo",
    );

    expect(next.schedule.timezone).toBe("Asia/Tokyo");
    expect(recipe.schedule).toEqual(original);
  });
});
