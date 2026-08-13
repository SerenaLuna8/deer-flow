import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";
import { useState } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  projectThreadMessages,
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

function median(samples: number[]) {
  const sorted = [...samples].sort((left, right) => left - right);
  return sorted[Math.floor(sorted.length / 2)] ?? 0;
}

function pureProjectionMedianMs(input: ThreadMessageProjectionInput) {
  projectThreadMessages(input);
  const samples = Array.from({ length: 5 }, () => {
    const startedAt = performance.now();
    const projected = projectThreadMessages(input);
    expect(projected.length).toBe(
      input.visibleHistory.length + input.renderMessages.length,
    );
    return performance.now() - startedAt;
  });
  return median(samples);
}

function MemoizedProjectionProbe({
  input,
  samples,
  identities,
}: {
  input: ThreadMessageProjectionInput;
  samples: number[];
  identities: Message[][];
}) {
  const [pass, setPass] = useState(0);
  const startedAt = performance.now();
  const projected = useProjectedThreadMessages(input);
  samples.push(performance.now() - startedAt);
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
  test("records 100/1k/10k pure profiles and skips unchanged React projections", () => {
    const profiles = [100, 1_000, 10_000].map((messageCount) => {
      const input = inputFor(messageCount);
      const reactSamples: number[] = [];
      const identities: Message[][] = [];
      const html = renderToStaticMarkup(
        <MemoizedProjectionProbe
          input={input}
          samples={reactSamples}
          identities={identities}
        />,
      );

      expect(html).toBe(`<output>${messageCount}</output>`);
      expect(reactSamples).toHaveLength(2);
      expect(identities).toHaveLength(2);
      expect(identities[1]).toBe(identities[0]);
      return {
        messageCount,
        pureMedianMs: pureProjectionMedianMs(input),
        reactInitialMs: reactSamples[0] ?? 0,
        reactUnrelatedRerenderMs: reactSamples[1] ?? 0,
      };
    });

    expect(profiles.every((profile) => profile.pureMedianMs >= 0)).toBe(true);
    console.info("THREAD_MESSAGE_PROJECTION_PROFILE", profiles);
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
