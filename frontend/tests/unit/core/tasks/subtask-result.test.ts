import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import {
  SUBAGENT_ERROR_KEY,
  SUBAGENT_STATUS_KEY,
  derivePendingSubtaskStatus,
  isSubtaskRunActive,
  parseSubtaskResult,
  parseSubtaskTerminalEvent,
} from "@/core/tasks/subtask-result";

describe("Sub-Agent status authority", () => {
  test("keeps an unknown reconnect state pending instead of inferring failure", () => {
    expect(derivePendingSubtaskStatus("task-reconnect", [], false)).toBe(
      "unknown",
    );
  });

  test("prioritizes an unstructured ToolMessage over an active Run projection", () => {
    const text = "Task failed. Error: legacy text is not authority";
    const messages = [
      {
        type: "tool",
        tool_call_id: "task-reconnect",
        content: text,
      },
    ] as Message[];

    expect(derivePendingSubtaskStatus("task-reconnect", messages, true)).toBe(
      "unknown",
    );
    expect(parseSubtaskResult(text)).toEqual({ status: "unknown" });
  });

  test("matches a subtask only to the authoritative active Run", () => {
    const groupMessages = [
      {
        type: "ai",
        content: "",
        additional_kwargs: { run_id: "run-a" },
      },
    ] as Message[];

    expect(isSubtaskRunActive(groupMessages, "run-a")).toBe(true);
    expect(isSubtaskRunActive(groupMessages, "run-b")).toBe(false);
    expect(isSubtaskRunActive(groupMessages, null)).toBe(false);
  });

  test.each([
    "Task failed. Error: transient reconnect gap",
    "Task timed out after 60 seconds",
    "Task cancelled by user.",
    "Task polling timed out while reconnecting",
    "Error: provider disconnected",
  ])("does not derive failure from unstructured ToolMessage text", (text) => {
    expect(parseSubtaskResult(text)).toEqual({ status: "unknown" });
  });

  test("accepts failure from structured ToolMessage metadata", () => {
    expect(
      parseSubtaskResult("display text is not the status protocol", {
        [SUBAGENT_STATUS_KEY]: "failed",
        [SUBAGENT_ERROR_KEY]: "provider error",
      }),
    ).toEqual({ status: "failed", error: "provider error" });
  });

  test("accepts failure from an authoritative terminal lifecycle event", () => {
    expect(
      parseSubtaskTerminalEvent({
        type: "task_failed",
        task_id: "task-terminal",
        error: "execution failed",
      }),
    ).toEqual({
      id: "task-terminal",
      status: "failed",
      error: "execution failed",
    });
  });
});
