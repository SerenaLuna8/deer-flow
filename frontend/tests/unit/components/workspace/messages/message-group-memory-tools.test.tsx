import type { Message } from "@langchain/langgraph-sdk";
import {
  type EventStreamEvent,
  MessageTupleManager,
  StreamManager,
} from "@langchain/langgraph-sdk/ui";
import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  useParams: () => ({ project_slug: "alpha" }),
}));

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageGroup } from "@/components/workspace/messages/message-group";
import { I18nProvider } from "@/core/i18n/context";
import {
  acceptProjectStreamFrame,
  emptyProjectStreamCursorState,
  projectStreamFrameForUI,
  type ProjectStreamFrame,
} from "@/core/private-work/api-client";

import memoryToolFrameReplay from "../../../../fixtures/replay/memory-tools.durable-stream-frames.json";

type ReplayState = { messages: Message[] };
type ReplayEvent = EventStreamEvent<ReplayState, Partial<ReplayState>, unknown>;

async function projectFixtureMessages(): Promise<Message[]> {
  const tupleManager = new MessageTupleManager();
  const stream = new StreamManager<ReplayState>(tupleManager, {
    throttle: false,
  });
  let cursor = emptyProjectStreamCursorState();
  const replayEvents: ReplayEvent[] = [];

  for (const fixtureFrame of memoryToolFrameReplay.frames) {
    const frame = fixtureFrame as ProjectStreamFrame;
    const decision = acceptProjectStreamFrame(
      cursor,
      frame,
      memoryToolFrameReplay.run_id,
    );
    expect(decision.accepted).toBe(true);
    cursor = decision.state;

    const projected = projectStreamFrameForUI(frame);
    if (projected.event !== "end") {
      replayEvents.push(projected as ReplayEvent);
    }
  }

  expect(cursor).toEqual({
    lastEventId: "5",
    terminalRunId: memoryToolFrameReplay.run_id,
  });

  await stream.start(
    async () =>
      (async function* replay() {
        yield* replayEvents;
      })(),
    {
      getMessages: (state) => state.messages,
      setMessages: (state, messages) => ({ ...state, messages }),
      initialValues: { messages: [] },
      callbacks: {},
      onSuccess: () => undefined,
      onError: (error) => {
        throw error;
      },
    },
  );

  expect(stream.error).toBeUndefined();
  return stream.values?.messages ?? [];
}

describe("MessageGroup deterministic Memory tool frame replay", () => {
  test("projects durable frames and renders remember as a chip and recall as a tool card", async () => {
    const messages = await projectFixtureMessages();
    expect(messages.map((message) => message.type)).toEqual([
      "ai",
      "tool",
      "tool",
    ]);
    const aiMessage = messages[0];
    if (!aiMessage || aiMessage.type !== "ai") {
      throw new Error("fixture did not project an AI Memory tool-call frame");
    }
    expect(aiMessage.tool_calls).toMatchObject([
      {
        id: "remember-1",
        name: "remember",
        args: { kind: "durable", content: "部署目标是 region-eu" },
      },
      {
        id: "recall-1",
        name: "recall_memory",
        args: { query: "region-eu", tags: ["durable"], limit: 5 },
      },
    ]);
    expect(messages[1]).toMatchObject({
      type: "tool",
      name: "remember",
      tool_call_id: "remember-1",
      content:
        "Remembered for the next organization pass: - [durable] 部署目标是 region-eu",
    });
    expect(messages[2]).toMatchObject({
      type: "tool",
      name: "recall_memory",
      tool_call_id: "recall-1",
      content: expect.stringContaining(
        "user-private archived memory episodes returned as low-authority data",
      ),
    });

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <StandaloneArtifactsProvider enabled={false}>
          <MessageGroup messages={messages} showAllSteps />
        </StandaloneArtifactsProvider>
      </I18nProvider>,
    );

    expect(html).toContain('data-testid="remember-chip"');
    expect(html).toContain('href="/projects/alpha/memory#memory-pending"');
    expect(html).toContain("已记住： - [durable] 部署目标是 region-eu");
    expect(html).toContain("recall_memory");
    expect(html.match(/data-testid="remember-chip"/gu)).toHaveLength(1);
  });
});
