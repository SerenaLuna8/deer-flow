import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import { taskEventToSubtaskUpdate } from "@/core/tasks/lifecycle";
import {
  SUBAGENT_ERROR_KEY,
  SUBAGENT_RESULT_BRIEF_KEY,
  SUBAGENT_STATUS_KEY,
  SUBAGENT_STOP_REASON_KEY,
  SUBAGENT_USAGE_RECEIPT_ID_KEY,
  derivePendingSubtaskStatus,
  isSubtaskRunActive,
  parseSubtaskResult,
  parseSubtaskTerminalEvent,
} from "@/core/tasks/subtask-result";

const EXECUTION_ID = "44444444-4444-4444-8444-444444444444";

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

  test("projects the server-owned historical usage receipt UUID as the Context Subject", () => {
    expect(
      parseSubtaskResult("display text", {
        [SUBAGENT_STATUS_KEY]: "completed",
        [SUBAGENT_USAGE_RECEIPT_ID_KEY]: EXECUTION_ID,
      }),
    ).toEqual({ status: "completed", executionId: EXECUTION_ID });

    expect(
      parseSubtaskResult("display text", {
        [SUBAGENT_STATUS_KEY]: "completed",
        [SUBAGENT_USAGE_RECEIPT_ID_KEY]: "tool-call-id-is-not-authority",
      }),
    ).toEqual({ status: "completed" });
  });

  test("projects the live lifecycle execution UUID independently from task_id", () => {
    expect(
      taskEventToSubtaskUpdate({
        type: "task_started",
        task_id: "tool-call-1",
        execution_id: EXECUTION_ID,
      }),
    ).toEqual({ id: "tool-call-1", executionId: EXECUTION_ID });

    expect(
      parseSubtaskTerminalEvent({
        type: "task_completed",
        task_id: "tool-call-1",
        execution_id: EXECUTION_ID,
      }),
    ).toEqual({
      id: "tool-call-1",
      status: "completed",
      executionId: EXECUTION_ID,
    });
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

  test("preserves a structured partial result while marking Provider output truncation", () => {
    expect(
      parseSubtaskResult("display text is not the status protocol", {
        [SUBAGENT_STATUS_KEY]: "completed",
        [SUBAGENT_RESULT_BRIEF_KEY]: "usable partial result",
        [SUBAGENT_STOP_REASON_KEY]: "output_truncated",
      }),
    ).toEqual({
      status: "completed",
      result: "usable partial result",
      stopReason: "output_truncated",
    });
  });

  test("projects Provider output truncation from the live terminal lifecycle event", () => {
    expect(
      parseSubtaskTerminalEvent({
        type: "task_completed",
        task_id: "task-truncated",
        result: "usable partial result",
        stop_reason: "output_truncated",
      }),
    ).toEqual({
      id: "task-truncated",
      status: "completed",
      result: "usable partial result",
      stopReason: "output_truncated",
    });
  });
});
