import { expect, test } from "@rstest/core";

import { promotePrivateRunExecutionProfile } from "@/core/api/api-client";
import {
  collectRunExecutionProfiles,
  RUN_EXECUTION_PROFILE_CONTEXT_KEY,
  withRunExecutionProfileContext,
} from "@/core/private-work/execution-profile";
import {
  agentModeForRunExecutionProfile,
  buildRunExecutionProfileRequest,
} from "@/core/threads/agent-mode";

test("moves execution choices behind the reserved SDK adapter key", () => {
  const profile = {
    model_name: "gpt-5.6-luna",
    thinking_enabled: true,
    reasoning_effort: "high" as const,
  };

  expect(
    withRunExecutionProfileContext(
      {
        thread_id: "thread-1",
        agent_name: "project-assistant-v1",
        model_name: "untrusted-model",
        model_selection_explicit: true,
        mode: "ultra",
        mode_selection_explicit: true,
        thinking_enabled: false,
        reasoning_effort: "minimal",
        is_plan_mode: true,
        subagent_enabled: true,
      },
      profile,
    ),
  ).toEqual({
    thread_id: "thread-1",
    agent_name: "project-assistant-v1",
    [RUN_EXECUTION_PROFILE_CONTEXT_KEY]: profile,
  });
});

test("carries requested choices to the top-level request and consumes the effective Run profile", () => {
  const requested = buildRunExecutionProfileRequest({
    mode: "ultra",
    modeSelectionExplicit: true,
    modelName: "gpt-5.6-luna",
    modelSelectionExplicit: true,
    agentModelRef: "default",
    model: {
      name: "gpt-5.6-luna",
      supports_thinking: true,
      supports_reasoning_effort: true,
    },
  });
  const context = withRunExecutionProfileContext(
    { thread_id: "thread-1" },
    requested,
  );
  const promoted = promotePrivateRunExecutionProfile(
    new URL("http://localhost/api/threads/thread-1/runs/stream"),
    {
      method: "POST",
      body: JSON.stringify({ context }),
    },
  );
  if (typeof promoted.body !== "string") {
    throw new TypeError("expected a serialized promoted Run body");
  }
  const body = JSON.parse(promoted.body) as {
    execution_profile: typeof requested;
  };

  expect(body.execution_profile).toEqual(requested);

  const effectiveProfiles = collectRunExecutionProfiles([
    {
      run_id: "run-1",
      execution_profile: {
        ...body.execution_profile,
        supports_vision: true,
      },
    },
  ]);
  const effective = effectiveProfiles.get("run-1");

  expect(effective).toEqual({
    model_name: "gpt-5.6-luna",
    thinking_enabled: true,
    reasoning_effort: "high",
    supports_vision: true,
  });
  expect(effective && agentModeForRunExecutionProfile(effective)).toBe("ultra");
});
