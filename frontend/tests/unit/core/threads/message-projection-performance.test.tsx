import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";
import { useState } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  type ThreadMessageProjectionInput,
  useProjectedThreadMessages,
} from "@/core/threads/hooks";

const EMPTY_IDS = new Set<string>();

function messages(count: number): Message[] {
  return Array.from({ length: count }, (_, index) => ({
    type: index % 2 === 0 ? "human" : "ai",
    id: `message-${index}`,
    content: `message ${index}`,
    additional_kwargs: {},
  })) as Message[];
}

function inputFor(messageCount: number): ThreadMessageProjectionInput {
  const allMessages = messages(messageCount);
  const overlapStart = Math.floor(messageCount * 0.8);
  return {
    threadId: "thread-benchmark",
    visibleHistory: allMessages.slice(0, overlapStart),
    pendingArchivedMessages: [],
    pendingArchiveThreadId: null,
    renderMessages: allMessages.slice(overlapStart),
    activeRunId: null,
    runBaselineMessageIds: EMPTY_IDS,
    pendingSupersededRunIds: EMPTY_IDS,
    visibleOptimisticMessages: [],
    historyRuns: [],
  };
}

function MemoizedProjectionProbe({
  input,
  identities,
}: {
  input: ThreadMessageProjectionInput;
  identities: Message[][];
}) {
  const [pass, setPass] = useState(0);
  const projected = useProjectedThreadMessages(input);
  identities.push(projected);
  if (pass === 0) setPass(1);
  return <output>{projected.length}</output>;
}

function ChangingProjectionProbe({
  initial,
  next,
  identities,
}: {
  initial: ThreadMessageProjectionInput;
  next: ThreadMessageProjectionInput;
  identities: Message[][];
}) {
  const [pass, setPass] = useState(0);
  const projected = useProjectedThreadMessages(pass === 0 ? initial : next);
  identities.push(projected);
  if (pass === 0) setPass(1);
  return <output>{projected.length}</output>;
}

describe("long-session message projection baseline", () => {
  test("reuses the projection identity when semantic inputs are unchanged", () => {
    const identities: Message[][] = [];
    const html = renderToStaticMarkup(
      <MemoizedProjectionProbe input={inputFor(100)} identities={identities} />,
    );

    expect(html).toBe("<output>100</output>");
    expect(identities).toHaveLength(2);
    expect(identities[1]).toBe(identities[0]);
  });

  test("invalidates the projection when a semantic input identity changes", () => {
    const identities: Message[][] = [];
    const html = renderToStaticMarkup(
      <ChangingProjectionProbe
        initial={inputFor(100)}
        next={inputFor(1_000)}
        identities={identities}
      />,
    );

    expect(html).toBe("<output>1000</output>");
    expect(identities).toHaveLength(2);
    expect(identities[1]).not.toBe(identities[0]);
  });
});
