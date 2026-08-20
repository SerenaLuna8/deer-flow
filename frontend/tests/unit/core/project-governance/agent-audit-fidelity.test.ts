import { describe, expect, test } from "@rstest/core";

import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import { auditItemSchema } from "@/core/project-governance/audit";

const base = {
  id: "11111111-1111-4111-8111-111111111111",
  occurred_at: "2026-08-13T00:00:00Z",
  actor: "user",
  action: "asset.updated",
  target_kind: "asset",
  outcome: "success",
  public_error_code: null,
} as const;

describe("Agent audit fidelity", () => {
  test("accepts and presents the safe operation and version number", () => {
    const item = auditItemSchema.parse({
      ...base,
      metadata: {
        asset_kind: "agent",
        operation: "agent.version.activate",
        version_number: 7,
      },
    });

    const presentation = describeAuditItem(item, "zh-CN");
    expect(presentation.metadata).toEqual(
      expect.arrayContaining([
        { label: "具体操作", value: "agent.version.activate" },
        { label: "资产版本", value: "7" },
      ]),
    );
  });

  test("preserves a legacy asset row without inventing an operation", () => {
    const item = auditItemSchema.parse({
      ...base,
      metadata: { asset_kind: "agent" },
    });

    expect(item.metadata).toEqual({ asset_kind: "agent" });
    const presentation = describeAuditItem(item, "zh-CN");
    expect(presentation.metadata).toEqual([
      { label: "资产类型", value: "智能体" },
    ]);
    expect(presentation.metadata).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ label: "具体操作" })]),
    );
  });

  test("rejects raw identifiers, unknown operations, and missing version coordinates", () => {
    for (const metadata of [
      {
        asset_kind: "agent",
        operation: "agent.version.activate",
        version_id: "22222222-2222-4222-8222-222222222222",
        version_number: 7,
      },
      { asset_kind: "agent", operation: "agent.unknown", version_number: 7 },
      { asset_kind: "agent", operation: "agent.version.activate" },
      {
        asset_kind: "skill",
        operation: "agent.version.activate",
        version_number: 7,
      },
    ]) {
      expect(auditItemSchema.safeParse({ ...base, metadata }).success).toBe(
        false,
      );
    }
  });
});
