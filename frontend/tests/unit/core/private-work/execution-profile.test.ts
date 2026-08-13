import { afterEach, expect, rs, test } from "@rstest/core";

import { createProjectPrivateClient } from "@/core/api/api-client";
import { enUS } from "@/core/i18n/locales/en-US";
import { zhCN } from "@/core/i18n/locales/zh-CN";
import {
  buildOutputLimitRetryProfile,
  collectRunExecutionProfiles,
  RUN_EXECUTION_PROFILE_CONTEXT_KEY,
  withRunExecutionProfileContext,
} from "@/core/private-work/execution-profile";
import {
  agentModeForRunExecutionProfile,
  buildRunExecutionProfileRequest,
} from "@/core/threads/agent-mode";

afterEach(() => {
  rs.unstubAllGlobals();
});

test("disables thinking for every output-limit recovery replay", () => {
  for (const reasoningEffort of ["high", "medium", "low", null] as const) {
    expect(
      buildOutputLimitRetryProfile({
        model_name: "deepseek-v4-pro",
        thinking_enabled: true,
        reasoning_effort: reasoningEffort,
      }),
    ).toEqual({
      model_name: "deepseek-v4-pro",
      thinking_enabled: false,
      reasoning_effort: "none",
    });
  }
});

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

test("carries requested choices through the SDK and consumes the effective Run profile", async () => {
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
  let requestBody: Record<string, unknown> | undefined;
  rs.stubGlobal(
    "fetch",
    rs.fn(async (_url: string | URL, init?: RequestInit) => {
      if (typeof init?.body !== "string") {
        throw new TypeError("expected a serialized JSON request body");
      }
      requestBody = JSON.parse(init.body) as Record<string, unknown>;
      return new Response("", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }),
  );

  const stream = createProjectPrivateClient({
    apiUrl: "http://localhost:2026/api/projects/project/private-work",
  }).runs.stream("thread-1", "project-assistant-v1", {
    input: { messages: [] },
    context,
  });
  await expect(stream.next()).resolves.toMatchObject({ done: true });

  const body = requestBody as {
    execution_profile: typeof requested;
    context: Record<string, unknown>;
  };

  expect(body.execution_profile).toEqual(requested);
  expect(body.context).toEqual({ thread_id: "thread-1" });

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

test("labels vision as a model capability rather than claiming image input", () => {
  expect(
    zhCN.conversation.runExecutionProfile("gpt-5.6-luna", "Pro", true),
  ).toBe("实际执行：gpt-5.6-luna · Pro · 支持视觉");
  expect(
    enUS.conversation.runExecutionProfile("gpt-5.6-luna", "Pro", true),
  ).toBe("Effective run: gpt-5.6-luna · Pro · vision-capable");
});
