import { describe, expect, test } from "@rstest/core";

import {
  automationListSchema,
  automationReadinessSchema,
  automationRunListSchema,
  automationRunSchema,
  automationSchema,
  createAutomationInputSchema,
  updateAutomationInputSchema,
} from "@/core/project-automations/types";

import { AUTOMATION, AUTOMATION_READINESS, AUTOMATION_RUN } from "./fixtures";

describe("project automation contracts", () => {
  test("accepts the complete public automation, run, list, and readiness contracts", () => {
    expect(automationSchema.parse(AUTOMATION)).toEqual(AUTOMATION);
    expect(automationListSchema.parse({ items: [AUTOMATION] })).toEqual({
      items: [AUTOMATION],
    });
    expect(automationRunSchema.parse(AUTOMATION_RUN)).toEqual(AUTOMATION_RUN);
    expect(automationRunListSchema.parse({ items: [AUTOMATION_RUN] })).toEqual({
      items: [AUTOMATION_RUN],
    });
    expect(automationReadinessSchema.parse(AUTOMATION_READINESS)).toEqual(
      AUTOMATION_READINESS,
    );
  });

  test("rejects owner, lease, membership, and other internal response fields", () => {
    expect(
      automationSchema.safeParse({
        ...AUTOMATION,
        owner_user_id: "11111111-1111-4111-8111-111111111111",
      }).success,
    ).toBe(false);
    expect(
      automationRunSchema.safeParse({
        ...AUTOMATION_RUN,
        lease_owner: "worker-secret",
      }).success,
    ).toBe(false);
    expect(
      automationRunSchema.safeParse({
        ...AUTOMATION_RUN,
        resolved_membership: { role: "admin" },
      }).success,
    ).toBe(false);
    expect(
      automationReadinessSchema.safeParse({
        ...AUTOMATION_READINESS,
        migration_digest: "private-digest",
      }).success,
    ).toBe(false);
  });

  test("validates create context ownership without accepting extra authority", () => {
    const fresh = {
      title: AUTOMATION.title,
      prompt: AUTOMATION.prompt,
      context_mode: "fresh_thread_per_run" as const,
      thread_id: null,
      agent_asset_id: AUTOMATION.agent_asset_id,
      agent_scope: AUTOMATION.agent_scope,
      schedule_type: AUTOMATION.schedule_type,
      schedule_spec: AUTOMATION.schedule_spec,
      timezone: AUTOMATION.timezone,
    };
    expect(createAutomationInputSchema.parse(fresh)).toEqual(fresh);
    expect(
      createAutomationInputSchema.safeParse({
        ...fresh,
        thread_id: "33333333-3333-4333-8333-333333333333",
      }).success,
    ).toBe(false);
    expect(
      createAutomationInputSchema.safeParse({
        ...fresh,
        project_id: "44444444-4444-4444-8444-444444444444",
        owner_user_id: "11111111-1111-4111-8111-111111111111",
      }).success,
    ).toBe(false);
    expect(
      createAutomationInputSchema.safeParse({
        ...fresh,
        context_mode: "reuse_thread",
      }).success,
    ).toBe(false);
  });

  test("allows only optimistic-version patch fields and requires a change", () => {
    expect(
      updateAutomationInputSchema.parse({
        expected_version: 2,
        title: "Updated title",
      }),
    ).toEqual({ expected_version: 2, title: "Updated title" });
    expect(
      updateAutomationInputSchema.safeParse({ expected_version: 2 }).success,
    ).toBe(false);
    expect(
      updateAutomationInputSchema.safeParse({
        expected_version: 2,
        agent_asset_id: "33333333-3333-4333-8333-333333333333",
      }).success,
    ).toBe(false);
  });
});
