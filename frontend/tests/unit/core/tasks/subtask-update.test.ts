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

  test("still allows inference to terminate a task whose last lifecycle event was running", () => {
    const transition = computeNextSubtask(
      { ...pendingToolResult, statusSource: "custom_event" },
      {
        id: pendingToolResult.id,
        status: "failed",
        statusSource: "inferred",
      },
    );

    expect(transition.next.status).toBe("failed");
    expect(transition.next.statusSource).toBe("inferred");
    expect(transition.becameTerminal).toBe(true);
    expect(transition.changed).toBe(true);
  });
});
