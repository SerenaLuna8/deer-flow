import { afterEach, expect, rs, test } from "@rstest/core";

import { createProjectPrivateClient } from "@/core/api/api-client";
import {
  readRunWorkloadProfile,
  resolveDisplayedRunWorkloadProfile,
  RUN_WORKLOAD_PROFILE_CONTEXT_KEY,
  withRunWorkloadProfileContext,
} from "@/core/private-work/workload-profile";

afterEach(() => {
  rs.unstubAllGlobals();
});

test("sends an explicit research choice as the top-level Private Run workload profile", async () => {
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
    context: withRunWorkloadProfileContext(
      { thread_id: "thread-1" },
      "research",
    ),
  });
  await expect(stream.next()).resolves.toMatchObject({ done: true });

  expect(requestBody).toMatchObject({
    workload_profile: "research",
    context: { thread_id: "thread-1" },
  });
  expect(
    Reflect.get(
      (requestBody?.context as Record<string, unknown> | undefined) ?? {},
      "__deerflow_workload_profile",
    ),
  ).toBeUndefined();
});

test("keeps generic context from selecting a Run workload profile", () => {
  expect(
    withRunWorkloadProfileContext(
      {
        thread_id: "thread-1",
        workload_profile: "research",
      },
      "interactive",
    ),
  ).toEqual({
    thread_id: "thread-1",
    [RUN_WORKLOAD_PROFILE_CONTEXT_KEY]: "interactive",
  });
});

test("reads only the server-confirmed top-level Run workload profile", () => {
  expect(
    readRunWorkloadProfile({
      workload_profile: "research",
      metadata: { workload_profile: "interactive" },
    }),
  ).toBe("research");
  expect(
    readRunWorkloadProfile({ metadata: { workload_profile: "research" } }),
  ).toBeNull();
  expect(readRunWorkloadProfile({ workload_profile: "unknown" })).toBeNull();
  expect(readRunWorkloadProfile("research")).toBeNull();
});

test("does not display a stale profile while the active Run awaits authoritative readback", () => {
  const previous = {
    run_id: "run-previous",
    workload_profile: "research",
  };

  expect(
    resolveDisplayedRunWorkloadProfile([previous], "run-active"),
  ).toBeNull();
  expect(
    resolveDisplayedRunWorkloadProfile(
      [{ run_id: "run-active", workload_profile: "interactive" }, previous],
      "run-active",
    ),
  ).toBe("interactive");
  expect(resolveDisplayedRunWorkloadProfile([previous], null)).toBe("research");
});
