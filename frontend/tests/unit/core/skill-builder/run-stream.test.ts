import { describe, expect, test } from "@rstest/core";

import {
  consumeSkillBuilderRunStream,
  createSkillBuilderRunStreamProjection,
  reduceSkillBuilderRunStreamFrame,
} from "@/core/skill-builder";

const RUN_ID = "44444444-4444-4444-8444-444444444444";

describe("Skill Builder durable Run stream projection", () => {
  test("projects arbitrary standard and Builder tools without retaining arguments or results", () => {
    const secret = "credential-must-not-enter-the-projection";
    let projection = createSkillBuilderRunStreamProjection(RUN_ID, "pending");

    projection = reduceSkillBuilderRunStreamFrame(projection, {
      id: "1",
      event: "values",
      data: {
        messages: [
          {
            id: `run-admission-${RUN_ID}`,
            type: "human",
            content: "private prompt",
            additional_kwargs: { run_id: RUN_ID },
          },
          {
            id: "assistant-tools",
            type: "ai",
            content: "",
            tool_calls: [
              {
                id: "call-read",
                name: "read_file",
                args: { path: `/skills/${secret}/SKILL.md` },
              },
              {
                id: "call-bash",
                name: "bash",
                args: { command: `printf ${secret}` },
              },
              {
                id: "call-builder",
                name: "skill_builder_replace_candidate_file",
                args: { content: secret },
              },
            ],
          },
        ],
      },
    });

    expect(projection.status).toBe("running");
    expect(projection.toolSteps).toEqual([
      { id: "call-read", toolName: "read_file", status: "running" },
      { id: "call-bash", toolName: "bash", status: "running" },
      {
        id: "call-builder",
        toolName: "skill_builder_replace_candidate_file",
        status: "running",
      },
    ]);
    expect(JSON.stringify(projection)).not.toContain(secret);

    projection = reduceSkillBuilderRunStreamFrame(projection, {
      id: "2",
      event: "values",
      data: {
        messages: [
          {
            id: `run-admission-${RUN_ID}`,
            type: "human",
            content: "private prompt",
            additional_kwargs: { run_id: RUN_ID },
          },
          {
            id: "assistant-tools",
            type: "ai",
            tool_calls: [
              { id: "call-read", name: "read_file", args: { secret } },
              { id: "call-bash", name: "bash", args: { secret } },
              {
                id: "call-builder",
                name: "skill_builder_replace_candidate_file",
                args: { secret },
              },
            ],
          },
          {
            id: "result-read",
            type: "tool",
            tool_call_id: "call-read",
            name: "read_file",
            status: "success",
            content: secret,
            additional_kwargs: { run_id: RUN_ID },
          },
          {
            id: "result-bash",
            type: "tool",
            tool_call_id: "call-bash",
            name: "bash",
            status: "error",
            content: secret,
            additional_kwargs: { run_id: RUN_ID },
          },
          {
            id: "result-builder",
            type: "tool",
            tool_call_id: "call-builder",
            name: "skill_builder_replace_candidate_file",
            status: "success",
            content: secret,
            additional_kwargs: { run_id: RUN_ID },
          },
        ],
      },
    });

    expect(
      projection.toolSteps.map(({ toolName, status }) => ({
        toolName,
        status,
      })),
    ).toEqual([
      { toolName: "read_file", status: "completed" },
      { toolName: "bash", status: "failed" },
      {
        toolName: "skill_builder_replace_candidate_file",
        status: "completed",
      },
    ]);
    expect(JSON.stringify(projection)).not.toContain(secret);
  });

  test("shows pending message chunks, then running and terminal states", () => {
    let projection = createSkillBuilderRunStreamProjection(RUN_ID, "pending");

    projection = reduceSkillBuilderRunStreamFrame(projection, {
      id: "1",
      event: "messages",
      data: [
        {
          type: "AIMessageChunk",
          tool_call_chunks: [
            {
              id: "call-web",
              name: "web_search",
              args: '{"query":"private"}',
            },
          ],
        },
        { langgraph_node: "model" },
      ],
    });
    expect(projection.toolSteps).toEqual([
      { id: "call-web", toolName: "web_search", status: "pending" },
    ]);

    projection = reduceSkillBuilderRunStreamFrame(projection, {
      id: "2",
      event: "updates",
      data: {
        model: {
          messages: [
            {
              type: "ai",
              tool_calls: [{ id: "call-web", name: "web_search", args: {} }],
            },
          ],
        },
      },
    });
    expect(projection.toolSteps[0]?.status).toBe("running");

    projection = reduceSkillBuilderRunStreamFrame(projection, {
      id: "3",
      event: "end",
      data: { status: "error", error: "private provider detail" },
    });
    expect(projection.status).toBe("error");
    expect(projection.toolSteps[0]?.status).toBe("failed");
    expect(JSON.stringify(projection)).not.toContain("private provider detail");
  });

  test("rebuilds the same safe projection from cursor-zero durable replay", () => {
    const frames = [
      {
        id: "10",
        event: "values",
        data: {
          messages: [
            {
              id: "old-ai",
              type: "ai",
              tool_calls: [{ id: "old-call", name: "old_turn_tool", args: {} }],
            },
            {
              id: `run-admission-${RUN_ID}`,
              type: "human",
              content: "current",
              additional_kwargs: { run_id: RUN_ID },
            },
            {
              id: "current-ai",
              type: "ai",
              tool_calls: [{ id: "current-call", name: "bash", args: {} }],
            },
            {
              id: "current-result",
              type: "tool",
              tool_call_id: "current-call",
              name: "bash",
              content: "private result",
            },
          ],
        },
      },
      { id: "11", event: "end", data: { status: "completed" } },
    ] as const;

    const replay = () =>
      frames.reduce(
        (projection, frame) =>
          reduceSkillBuilderRunStreamFrame(projection, frame),
        createSkillBuilderRunStreamProjection(RUN_ID, "pending"),
      );

    expect(replay()).toEqual(replay());
    expect(replay().toolSteps).toEqual([
      { id: "current-call", toolName: "bash", status: "completed" },
    ]);
    expect(JSON.stringify(replay())).not.toContain("private result");
    expect(JSON.stringify(replay())).not.toContain("old_turn_tool");
  });

  test("ignores delegated subgraph frames in the lead Builder activity", () => {
    const initial = createSkillBuilderRunStreamProjection(RUN_ID, "running");
    const projection = reduceSkillBuilderRunStreamFrame(initial, {
      id: "20",
      event: "values|task:delegated",
      data: {
        messages: [
          {
            type: "ai",
            tool_calls: [
              { id: "child-call", name: "child_private_tool", args: {} },
            ],
          },
        ],
      },
    });

    expect(projection).toBe(initial);
  });

  test("reconnects from durable origin and converges after a dropped live stream", async () => {
    const secret = "dropped-stream-private-result";
    let attempt = 0;
    const observed: ReturnType<typeof createSkillBuilderRunStreamProjection>[] =
      [];

    await consumeSkillBuilderRunStream({
      runId: RUN_ID,
      initialStatus: "pending",
      signal: new AbortController().signal,
      retryDelayMs: 0,
      open: () => {
        attempt += 1;
        if (attempt === 1) {
          return (async function* () {
            yield {
              id: "1",
              event: "messages",
              data: [
                {
                  type: "AIMessageChunk",
                  tool_call_chunks: [
                    {
                      id: "call-read",
                      name: "read_file",
                      args: secret,
                    },
                  ],
                },
                {},
              ],
            };
            throw new Error("connection dropped");
          })();
        }
        return (async function* () {
          yield {
            id: "1",
            event: "values",
            data: {
              messages: [
                {
                  type: "human",
                  additional_kwargs: { run_id: RUN_ID },
                },
                {
                  type: "ai",
                  tool_calls: [
                    { id: "call-read", name: "read_file", args: secret },
                  ],
                },
                {
                  type: "tool",
                  tool_call_id: "call-read",
                  name: "read_file",
                  content: secret,
                },
              ],
            },
          };
          yield { id: "2", event: "end", data: { status: "completed" } };
        })();
      },
      onProjection: (projection) => observed.push(projection),
    });

    expect(attempt).toBe(2);
    expect(observed.at(-1)).toEqual({
      runId: RUN_ID,
      status: "success",
      messages: [],
      toolSteps: [
        { id: "call-read", toolName: "read_file", status: "completed" },
      ],
      clarification: null,
    });
    expect(JSON.stringify(observed)).not.toContain(secret);
  });

  test("stops after the bounded number of reconnects", async () => {
    let attempts = 0;
    const controller = new AbortController();
    globalThis.setTimeout(() => controller.abort(), 25);

    await consumeSkillBuilderRunStream({
      runId: RUN_ID,
      initialStatus: "running",
      signal: controller.signal,
      retryDelayMs: 1,
      maxReconnectAttempts: 2,
      open: () => {
        attempts += 1;
        return (async function* () {
          throw new Error("not found");
        })();
      },
      onProjection: () => undefined,
    });

    expect(attempts).toBe(3);
    expect(controller.signal.aborted).toBe(false);
    controller.abort();
  });
});
