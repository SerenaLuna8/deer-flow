import { describe, expect, test } from "@rstest/core";

import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import { auditItemSchema } from "@/core/project-governance/audit";

const base = {
  id: "11111111-1111-4111-8111-111111111111",
  occurred_at: "2026-08-26T00:00:00Z",
  actor: "user",
  action: "asset.deleted",
  target_kind: "asset",
  outcome: "success",
  public_error_code: null,
} as const;

describe("Skill deletion audit", () => {
  test("accepts and presents the authoritative affected Agent count", () => {
    const item = auditItemSchema.parse({
      ...base,
      metadata: {
        asset_kind: "skill",
        operation: "skill.delete",
        affected_agent_count: 3,
      },
    });

    expect(describeAuditItem(item, "zh-CN").metadata).toEqual(
      expect.arrayContaining([
        { label: "具体操作", value: "skill.delete" },
        { label: "受影响 Agent 数", value: "3" },
      ]),
    );
  });

  test("rejects invalid counts and counts on other operations", () => {
    for (const metadata of [
      {
        asset_kind: "skill",
        operation: "skill.delete",
        affected_agent_count: -1,
      },
      {
        asset_kind: "skill",
        operation: "skill.delete",
        affected_agent_count: "3",
      },
      {
        asset_kind: "skill",
        operation: "skill.delete",
      },
      {
        asset_kind: "skill",
        operation: "skill.enable",
        affected_agent_count: 3,
      },
    ]) {
      expect(auditItemSchema.safeParse({ ...base, metadata }).success).toBe(
        false,
      );
    }
  });
});
