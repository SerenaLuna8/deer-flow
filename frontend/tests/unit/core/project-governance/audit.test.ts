import { describe, expect, test } from "@rstest/core";

import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import { adminAuditItemSchema } from "@/core/admin-operations/types";
import {
  auditItemSchema,
  auditPageSchema,
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
  test("accepts configuration-secret lifecycle rows returned by the audit API", () => {
    const items = [
      {
        ...recallAuditItem,
        actor: "user",
        action: "asset.updated",
        target_kind: "asset",
        metadata: {
          asset_kind: "skill",
          operation: "skill.secret.configure",
          version_number: 2,
          version_id: "22222222-2222-4222-8222-222222222222",
          secret_name: "ROUTE_DB_PASSWORD",
          generation_id: "33333333-3333-4333-8333-333333333333",
          revision: 1,
          result: "configured",
          reason: "created",
          readiness: "ready",
        },
      },
      {
        ...recallAuditItem,
        id: "44444444-4444-4444-8444-444444444444",
        actor: "user",
        action: "asset.updated",
        target_kind: "asset",
        metadata: {
          asset_kind: "mcp",
          operation: "mcp.secret.configure",
          version_id: "55555555-5555-4555-8555-555555555555",
          slot_id: "66666666-6666-4666-8666-666666666666",
          secret_name: "Authorization",
          generation_id: "77777777-7777-4777-8777-777777777777",
          revision: 1,
          result: "configured",
          reason: "created",
          readiness: "ready",
        },
      },
    ] as const;

    expect(
      auditPageSchema.safeParse({ items, next_cursor: null }).success,
    ).toBe(true);
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

  test("accepts all closed host execution approval events", () => {
    const events = [
      {
        ...recallAuditItem,
        action: "host_execution.approval_requested",
        metadata: {},
      },
      {
        ...recallAuditItem,
        action: "host_execution.approval_available",
        metadata: {},
      },
      {
        ...recallAuditItem,
        actor: "user",
        action: "host_execution.approval_decided",
        metadata: { decision: "allow_once" },
      },
      {
        ...recallAuditItem,
        action: "host_execution.approval_claimed",
        metadata: {},
      },
      {
        ...recallAuditItem,
        action: "host_execution.approval_terminal",
        metadata: { status: "finished" },
      },
    ] as const;

    for (const event of events) {
      expect(auditItemSchema.safeParse(event).success).toBe(true);
      expect(
        adminAuditItemSchema.safeParse({
          ...event,
          actor_user_id: null,
          actor_email: null,
          project_id: "22222222-2222-4222-8222-222222222222",
          project_slug: "host-approval-project",
          project_display_name: "Host approval project",
        }).success,
      ).toBe(true);
    }

    const decision = auditItemSchema.parse(events[2]);
    const terminal = auditItemSchema.parse(events[4]);
    expect(describeAuditItem(decision, "zh-CN")).toMatchObject({
      action: "已处理宿主机命令审批",
      metadata: [{ label: "审批决定", value: "允许一次" }],
    });
    expect(describeAuditItem(terminal, "en-US")).toMatchObject({
      action: "Host command approval finished",
      metadata: [{ label: "Terminal status", value: "Execution finished" }],
    });
  });

  test("rejects open or invalid host execution approval metadata", () => {
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        action: "host_execution.approval_requested",
        metadata: { command: "python private.py" },
      }).success,
    ).toBe(false);
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        actor: "user",
        action: "host_execution.approval_decided",
        metadata: { decision: "allow_session" },
      }).success,
    ).toBe(false);
    expect(
      auditItemSchema.safeParse({
        ...recallAuditItem,
        action: "host_execution.approval_terminal",
        metadata: { status: "running" },
      }).success,
    ).toBe(false);
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

  test("accepts closed Memory lifecycle events and rejects content", () => {
    const admitted = {
      ...recallAuditItem,
      actor: "scheduler",
      action: "memory.dream.admitted",
      target_kind: "job",
      metadata: {
        origin: "scheduled",
        trigger: "auto_dream",
        history_count: 4,
      },
    } as const;
    const settled = {
      ...recallAuditItem,
      action: "memory.dream.settled",
      target_kind: "job",
      metadata: { disposition: "published", version: 6 },
    } as const;
    const restored = {
      ...recallAuditItem,
      actor: "user",
      action: "memory.restore.executed",
      target_kind: "project",
      metadata: {
        source_version: 2,
        previous_version: 5,
        published_version: 6,
        changed: true,
      },
    } as const;
    const reset = {
      ...recallAuditItem,
      actor: "user",
      action: "memory.reset.executed",
      target_kind: "account",
      metadata: { scope: "project" },
    } as const;

    for (const event of [admitted, settled, restored, reset]) {
      expect(auditItemSchema.safeParse(event).success).toBe(true);
    }
    expect(
      auditItemSchema.safeParse({
        ...admitted,
        metadata: { ...admitted.metadata, content: "private Memory" },
      }).success,
    ).toBe(false);
    expect(
      auditItemSchema.safeParse({
        ...settled,
        metadata: {
          disposition: "published",
          version: 6,
          public_error_code: "MEMORY_DREAM_FAILED",
        },
      }).success,
    ).toBe(false);
    expect(
      auditItemSchema.safeParse({
        ...restored,
        metadata: { ...restored.metadata, published_version: 7 },
      }).success,
    ).toBe(false);

    const presentation = describeAuditItem(reset, "zh-CN");
    expect(presentation.action).toBe("已重置账户记忆");
    expect(presentation.target).toBe("账户");
  });
});
