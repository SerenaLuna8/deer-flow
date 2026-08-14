import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import {
  extractExecutionApprovalArtifact,
  findLatestExecutionApprovalArtifact,
} from "@/core/execution-approvals/artifacts";

const FIRST_APPROVAL_ID = "11111111-1111-4111-8111-111111111111";
const SECOND_APPROVAL_ID = "22222222-2222-4222-8222-222222222222";

function approvalMessage({
  approvalId,
  name,
}: {
  approvalId: string;
  name: "bash" | "task";
}) {
  return {
    type: "tool",
    content:
      name === "bash"
        ? "Host command execution requires approval."
        : "Delegated host command execution requires approval.",
    name,
    tool_call_id: `${name}-call`,
    artifact: {
      host_execution_approval: {
        schema_version: 1,
        kind: "local_shell",
        approval_id: approvalId,
        source_run_id: "33333333-3333-4333-8333-333333333333",
        source_tool_call_id: "bash-call",
      },
    },
  } as unknown as Message;
}

describe("execution approval ToolMessage artifacts", () => {
  test("recovers a lead Bash approval anchor", () => {
    expect(
      extractExecutionApprovalArtifact(
        approvalMessage({ approvalId: FIRST_APPROVAL_ID, name: "bash" }),
      )?.approval_id,
    ).toBe(FIRST_APPROVAL_ID);
  });

  test("recovers the same anchor bubbled through a child task ToolMessage", () => {
    expect(
      extractExecutionApprovalArtifact(
        approvalMessage({ approvalId: FIRST_APPROVAL_ID, name: "task" }),
      )?.approval_id,
    ).toBe(FIRST_APPROVAL_ID);
  });

  test("uses the latest persisted anchor after multiple serial approvals", () => {
    const messages = [
      approvalMessage({ approvalId: FIRST_APPROVAL_ID, name: "bash" }),
      { type: "ai", content: "between commands" } as Message,
      approvalMessage({ approvalId: SECOND_APPROVAL_ID, name: "task" }),
    ];

    expect(findLatestExecutionApprovalArtifact(messages)?.approval_id).toBe(
      SECOND_APPROVAL_ID,
    );
  });

  test("rejects malformed or authority-bearing artifacts", () => {
    const message = approvalMessage({
      approvalId: FIRST_APPROVAL_ID,
      name: "bash",
    });
    const artifact = Reflect.get(message, "artifact") as Record<
      string,
      Record<string, unknown>
    >;
    const payload = artifact.host_execution_approval;
    if (!payload)
      throw new Error("test fixture is missing its approval payload");
    payload.browser_grant = "allow";

    expect(extractExecutionApprovalArtifact(message)).toBeNull();
  });
});
