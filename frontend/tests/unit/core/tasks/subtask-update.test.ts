import { describe, expect, test } from "@rstest/core";

import { computeNextSubtask } from "@/core/tasks/subtask-update";
import type { Subtask } from "@/core/tasks/types";

const pendingToolResult: Subtask = {
  id: "task-approval-1",
  status: "in_progress",
  statusSource: "tool_result",
  subagent_type: "general-purpose",
  description: "Run a host command",
  prompt: "Use bash once",
};

describe("computeNextSubtask", () => {
  test("keeps an equal in-progress tool result authoritative over render-time inference", () => {
    const transition = computeNextSubtask(pendingToolResult, {
      id: pendingToolResult.id,
      status: "in_progress",
      statusSource: "inferred",
      subagent_type: pendingToolResult.subagent_type,
      description: pendingToolResult.description,
      prompt: pendingToolResult.prompt,
    });

    expect(transition.next).toBe(pendingToolResult);
    expect(transition.changed).toBe(false);
  });

  test("does not let inference terminate a task whose lifecycle event is running", () => {
    const transition = computeNextSubtask(
      { ...pendingToolResult, statusSource: "custom_event" },
      {
        id: pendingToolResult.id,
        status: "failed",
        statusSource: "inferred",
      },
    );

    expect(transition.next.status).toBe("in_progress");
    expect(transition.next.statusSource).toBe("custom_event");
    expect(transition.becameTerminal).toBe(false);
    expect(transition.changed).toBe(false);
  });

  test("lets a running lifecycle event correct an inferred reconnect failure", () => {
    const transition = computeNextSubtask(
      {
        ...pendingToolResult,
        status: "failed",
        statusSource: "inferred",
      },
      {
        id: pendingToolResult.id,
        status: "in_progress",
        statusSource: "custom_event",
        usage: {
          inputTokens: 80_000,
          outputTokens: 6_160,
          totalTokens: 86_160,
        },
      },
    );

    expect(transition.next.status).toBe("in_progress");
    expect(transition.next.statusSource).toBe("custom_event");
    expect(transition.next.usage?.totalTokens).toBe(86_160);
    expect(transition.changed).toBe(true);
  });

  test("uses a neutral unknown state when a running task loses Run authority", () => {
    const transition = computeNextSubtask(
      { ...pendingToolResult, statusSource: "custom_event" },
      {
        id: pendingToolResult.id,
        status: "unknown",
        statusSource: "inferred",
      },
    );

    expect(transition.next.status).toBe("unknown");
    expect(transition.next.statusSource).toBe("inferred");
    expect(transition.becameTerminal).toBe(false);
    expect(transition.changed).toBe(true);
  });

  test("does not let the replayed inferred task call overwrite an approval-owned state", () => {
    const approvalOwned: Subtask = {
      ...pendingToolResult,
      statusSource: "execution_approval",
      executionApproval: {
        approvalId: "11111111-1111-4111-8111-111111111111",
        status: "pending",
      },
    };

    const transition = computeNextSubtask(approvalOwned, {
      id: approvalOwned.id,
      status: "in_progress",
      statusSource: "inferred",
      subagent_type: approvalOwned.subagent_type,
      description: approvalOwned.description,
      prompt: approvalOwned.prompt,
    });

    expect(transition.next).toBe(approvalOwned);
    expect(transition.changed).toBe(false);
  });

  test("keeps a pending approval authoritative over inferred unknown", () => {
    const approvalOwned: Subtask = {
      ...pendingToolResult,
      statusSource: "execution_approval",
      executionApproval: {
        approvalId: "11111111-1111-4111-8111-111111111111",
        status: "pending",
      },
    };

    const transition = computeNextSubtask(approvalOwned, {
      id: approvalOwned.id,
      status: "unknown",
      statusSource: "inferred",
    });

    expect(transition.next).toBe(approvalOwned);
    expect(transition.changed).toBe(false);
  });
});
