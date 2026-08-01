import { describe, expect, it } from "@rstest/core";

import { taskEventToSubtaskUpdate } from "@/core/tasks/lifecycle";

describe("taskEventToSubtaskUpdate", () => {
  it("maps task_started to the effective model", () => {
    expect(
      taskEventToSubtaskUpdate({
        type: "task_started",
        task_id: "call-1",
        model_name: "  claude-3-7-sonnet  ",
      }),
    ).toEqual({
      id: "call-1",
      modelName: "claude-3-7-sonnet",
    });
  });

  it("maps task_running to cumulative model and usage metadata", () => {
    expect(
      taskEventToSubtaskUpdate({
        type: "task_running",
        task_id: "call-1",
        model_name: "claude-3-7-sonnet",
        usage: {
          input_tokens: 100,
          output_tokens: 20,
          total_tokens: 120,
        },
      }),
    ).toEqual({
      id: "call-1",
      modelName: "claude-3-7-sonnet",
      usage: {
        inputTokens: 100,
        outputTokens: 20,
        totalTokens: 120,
      },
    });
  });

  it("rejects malformed identity and empty additive updates", () => {
    expect(
      taskEventToSubtaskUpdate({
        type: "task_started",
        task_id: "",
        model_name: "model",
      }),
    ).toBeNull();
    expect(
      taskEventToSubtaskUpdate({
        type: "task_running",
        task_id: "call-1",
        usage: { input_tokens: 1, output_tokens: 2 },
      }),
    ).toBeNull();
  });
});
