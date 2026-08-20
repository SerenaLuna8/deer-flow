import { describe, expect, test } from "@rstest/core";

import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import { auditItemSchema } from "@/core/project-governance/audit";

describe("System Skill revocation audit", () => {
  test("accepts the safe operation with an exact version number", () => {
    const item = auditItemSchema.parse({
      id: "11111111-1111-4111-8111-111111111111",
      occurred_at: "2026-08-13T00:00:00Z",
      actor: "system_admin",
      action: "asset.deprecated",
      target_kind: "asset",
      outcome: "success",
      public_error_code: null,
      metadata: {
        asset_kind: "skill",
        operation: "skill.version.revoke",
        version_number: 3,
      },
    });

    expect(describeAuditItem(item, "zh-CN").metadata).toEqual(
      expect.arrayContaining([
        { label: "具体操作", value: "skill.version.revoke" },
        { label: "资产版本", value: "3" },
      ]),
    );
  });

  test("accepts a distinct Skill export action with the selected version", () => {
    const item = auditItemSchema.parse({
      id: "11111111-1111-4111-8111-111111111112",
      occurred_at: "2026-08-20T00:00:00Z",
      actor: "user",
      action: "asset.exported",
      target_kind: "asset",
      outcome: "success",
      public_error_code: null,
      metadata: {
        asset_kind: "skill",
        operation: "skill.export",
        version_number: 7,
      },
    });

    expect(describeAuditItem(item, "zh-CN")).toMatchObject({
      action: "已导出资产版本",
      metadata: expect.arrayContaining([
        { label: "具体操作", value: "skill.export" },
        { label: "资产版本", value: "7" },
      ]),
    });
  });
});
