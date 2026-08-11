import { describe, expect, test } from "@rstest/core";

import { adminAuditItemSchema } from "@/core/admin-operations/types";
import {
  WORKFLOW_AUDIT_ACTIONS,
  auditActionSchema,
  auditItemSchema,
  auditTargetKindSchema,
} from "@/core/project-governance/audit";

const recallAuditItem = {
  id: "11111111-1111-4111-8111-111111111111",
  occurred_at: "2026-08-09T00:00:00Z",
  actor: "worker",
  action: "memory.recall.executed",
  target_kind: "run",
  outcome: "success",
  public_error_code: null,
  metadata: {
    result_bucket: "1-2",
    matched_stage: "similarity",
    tags_filtered: true,
    query_len_bucket: "17-64",
  },
} as const;

describe("project audit contract", () => {
  test("mirrors the closed Workflow action group and target kind", () => {
    expect(WORKFLOW_AUDIT_ACTIONS).toEqual([
      "workflow.definition_created",
      "workflow.definition_updated",
      "workflow.definition_archived",
      "workflow.draft_saved",
      "workflow.version_published",
      "workflow.draft_grant_intent_updated",
      "workflow.draft_grant_intent_deleted",
      "workflow.version_grant_updated",
      "workflow.version_grant_revoked",
    ]);

    for (const action of WORKFLOW_AUDIT_ACTIONS) {
      expect(auditActionSchema.parse(action)).toBe(action);
    }
    expect(auditTargetKindSchema.parse("workflow")).toBe("workflow");
    expect(auditActionSchema.safeParse("workflow.unknown").success).toBe(false);
    expect(auditTargetKindSchema.safeParse("workflow_version").success).toBe(
      false,
    );
  });

  test("accepts only content-free Workflow control-plane audit events", () => {
    for (const action of WORKFLOW_AUDIT_ACTIONS) {
      const item = {
        ...recallAuditItem,
        actor: "user",
        action,
        target_kind: "workflow",
        metadata: {},
      };
      expect(auditItemSchema.safeParse(item).success).toBe(true);
      expect(
        auditItemSchema.safeParse({
          ...item,
          metadata: { idempotency_key: "raw-key", secret: "never" },
        }).success,
      ).toBe(false);
    }
  });

  test("keeps the Workflow action group paired with the Workflow target kind", () => {
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        actor: "user",
        action: WORKFLOW_AUDIT_ACTIONS[0],
        target_kind: "run",
        metadata: {},
      }).success,
    ).toBe(false);
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        actor: "user",
        action: "project.updated",
        target_kind: "workflow",
        metadata: {},
      }).success,
    ).toBe(false);
  });

  test("accepts the complete closed memory recall metadata vocabulary", () => {
    expect(auditItemSchema.parse(recallAuditItem).metadata).toEqual(
      recallAuditItem.metadata,
    );

    for (const query_len_bucket of ["1-4", "5-16", "17-64", "65-200"]) {
      expect(
        auditItemSchema.safeParse({
          ...recallAuditItem,
          metadata: { ...recallAuditItem.metadata, query_len_bucket },
        }).success,
      ).toBe(true);
    }
  });

  test("keeps the admin audit parser on the same strict recall contract", () => {
    expect(
      adminAuditItemSchema.parse({
        ...recallAuditItem,
        actor_user_id: null,
        actor_email: null,
        project_id: "22222222-2222-4222-8222-222222222222",
        project_slug: "recall-project",
        project_display_name: "Recall project",
      }).metadata,
    ).toEqual(recallAuditItem.metadata);
  });

  test("rejects missing or open-ended memory recall metadata", () => {
    const missingQueryLength = {
      result_bucket: recallAuditItem.metadata.result_bucket,
      matched_stage: recallAuditItem.metadata.matched_stage,
      tags_filtered: recallAuditItem.metadata.tags_filtered,
    };
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        metadata: missingQueryLength,
      }).success,
    ).toBe(false);
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        metadata: {
          ...recallAuditItem.metadata,
          query_len_bucket: "201+",
        },
      }).success,
    ).toBe(false);
  });
});
