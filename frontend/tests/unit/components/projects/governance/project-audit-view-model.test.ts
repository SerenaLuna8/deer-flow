import { describe, expect, test } from "@rstest/core";

import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import {
  WORKFLOW_AUDIT_ACTIONS,
  auditItemSchema,
} from "@/core/project-governance/audit";

const recallAuditItem = auditItemSchema.parse({
  id: "11111111-1111-4111-8111-111111111111",
  occurred_at: "2026-08-09T00:00:00Z",
  actor: "worker",
  action: "memory.recall.executed",
  target_kind: "run",
  outcome: "success",
  public_error_code: null,
  metadata: {
    result_bucket: "3+",
    matched_stage: "exact",
    tags_filtered: false,
    query_len_bucket: "65-200",
  },
});

describe("describeAuditItem", () => {
  test("renders every closed Workflow action and target in both locales", () => {
    for (const action of WORKFLOW_AUDIT_ACTIONS) {
      const item = auditItemSchema.parse({
        ...recallAuditItem,
        actor: "user",
        action,
        target_kind: "workflow",
        metadata: {},
      });
      const zh = describeAuditItem(item, "zh-CN");
      const en = describeAuditItem(item, "en-US");
      expect(zh.action.length).toBeGreaterThan(0);
      expect(en.action.length).toBeGreaterThan(0);
      expect(zh.target).toBe("工作流");
      expect(en.target).toBe("Workflow");
    }
  });

  test("renders the recall query-length bucket for project and admin audit views", () => {
    expect(describeAuditItem(recallAuditItem, "zh-CN").metadata).toContainEqual(
      {
        label: "查询长度",
        value: "65-200",
      },
    );
    expect(describeAuditItem(recallAuditItem, "en-US").metadata).toContainEqual(
      {
        label: "Query length",
        value: "65-200",
      },
    );
  });
});
