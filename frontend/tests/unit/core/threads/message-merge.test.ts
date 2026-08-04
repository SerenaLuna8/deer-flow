import type { Message, Run } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import { deriveHumanInputThreadState } from "@/core/messages/human-input";
import { buildRunMessagesUrl } from "@/core/threads/api";
import {
  attachRunIdToNewMessages,
  buildVisibleHistoryMessages,
  canStartPreparedReplay,
  classifyPreparedReplaySdkError,
  computeSummarizationMovedMessages,
  countHumanMessagesExcludingSuperseded,
  filterMessagesBySupersededRunIds,
  findLatestUnloadedRunIndex,
  getMessageMasksAfterPreparedReplayFailure,
  getNextRunMessagesBeforeSeq,
  getOldestRunMessageSeq,
  getPreparedReplayStopRollback,
  getRunMasksAfterPreparedReplayFailure,
  getSupersededRunIds,
  getSummarizationMiddlewareMessages,
  getVisibleOptimisticMessages,
  latestRunHasTerminalFailure,
  MAX_CONSECUTIVE_EMPTY_RUN_LOADS,
  mergeMessages,
  mergeRunMessageRows,
  overlayThreadProjection,
  type PendingPreparedReplayMask,
  rememberActiveRun,
  resolveActiveRunIdForMessages,
  pruneConfirmedArchivedMessages,
  resolvePreservedHistory,
  runMessagesPageHasMore,
  shouldAutoContinueOnEmptyRun,
} from "@/core/threads/hooks";
import type { RunMessage } from "@/core/threads/types";

function runMessage(seq?: string): RunMessage {
  return {
    run_id: "run-1",
    ...(seq === undefined ? {} : { seq }),
    content: {} as Message,
    metadata: { caller: "" },
    created_at: "2026-05-22T00:00:00Z",
  };
}

test("edit replay hides its optimistic copy after the replacement human persists", () => {
  const supersededHuman = {
    id: "human-1__user",
    type: "human",
    content: "introduce Li Bai",
  } as Message;
  const optimisticHuman = {
    id: "replacement-1",
    type: "human",
    content: "introduce Du Fu",
  } as Message;

  const baseline = countHumanMessagesExcludingSuperseded(
    [supersededHuman],
    ["human-1__user", "ai-1"],
  );

  expect(baseline).toBe(0);
  expect(getVisibleOptimisticMessages([optimisticHuman], baseline, 0)).toEqual([
    optimisticHuman,
  ]);
  expect(getVisibleOptimisticMessages([optimisticHuman], baseline, 1)).toEqual(
    [],
  );
});

test("prepared replay baseline keeps human turns it does not supersede", () => {
  const keptHuman = { id: "human-1", type: "human", content: "one" } as Message;
  const supersededHuman = {
    id: "human-2",
    type: "human",
    content: "two",
  } as Message;
  const ai = { id: "ai-1", type: "ai", content: "answer" } as Message;

  expect(
    countHumanMessagesExcludingSuperseded(
      [keptHuman, ai, supersededHuman],
      ["human-2", "ai-2"],
    ),
  ).toBe(1);
});

function orderedRunMessage(
  runId: string,
  seq: string,
  messageId: string,
  content: string,
): RunMessage {
  return {
    run_id: runId,
    seq,
    content: {
      id: messageId,
      type: "human",
      content,
    } as Message,
    metadata: { caller: "lead_agent" },
    created_at: "2026-07-23T00:00:00Z",
  };
}

function threadRun(runId: string, status: Run["status"] = "success"): Run {
  return {
    run_id: runId,
    thread_id: "thread-1",
    assistant_id: "lead_agent",
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:00:00Z",
    status,
    metadata: {},
    multitask_strategy: null,
  };
}

function clarificationToolMessage(
  requestId: string,
  toolCallId: string,
  runId?: string,
): Message {
  return {
    id: requestId,
    type: "tool",
    name: "ask_clarification",
    tool_call_id: toolCallId,
    content: "fallback",
    ...(runId ? { run_id: runId } : {}),
    artifact: {
      human_input: {
        version: 2,
        kind: "human_input_request",
        source: "ask_clarification",
        request_id: requestId,
        question: `Question for ${requestId}`,
        input_mode: "form",
        fields: [
          {
            name: "answer",
            label: "Answer",
            type: "text",
            required: true,
          },
        ],
      },
    },
  } as unknown as Message;
}

test("rememberActiveRun exposes a newly admitted run before the list refetch completes", () => {
  const previous = [threadRun("run-old")];

  expect(
    rememberActiveRun(previous, {
      threadId: "thread-1",
      runId: "run-current",
      createdAt: "2026-07-27T01:00:00Z",
    }),
  ).toEqual([
    {
      ...threadRun("run-current", "running"),
      created_at: "2026-07-27T01:00:00Z",
      updated_at: "2026-07-27T01:00:00Z",
    },
    threadRun("run-old"),
  ]);
});

test("rememberActiveRun keeps an authoritative existing run while moving it to the newest position", () => {
  const current = {
    ...threadRun("run-current", "pending"),
    metadata: { admitted_by: "gateway" },
  };

  expect(
    rememberActiveRun([threadRun("run-old"), current], {
      threadId: "thread-1",
      runId: "run-current",
      createdAt: "2026-07-27T01:00:00Z",
    }),
  ).toEqual([current, threadRun("run-old")]);
});

test("reconnect infers the active run from the latest admission Human message", () => {
  const checkpointMessages = [
    {
      id: "human-old",
      type: "human",
      content: "old prompt",
      additional_kwargs: { run_id: "run-old" },
    },
    {
      id: "ai-old",
      type: "ai",
      content: "old answer",
    },
    {
      id: "human-current",
      type: "human",
      content: "current prompt",
      additional_kwargs: { run_id: "run-current" },
    },
    {
      id: "ai-task-current",
      type: "ai",
      content: "",
      tool_calls: [
        {
          id: "task-current",
          name: "task",
          args: { description: "research" },
        },
      ],
    },
  ] as unknown as Message[];

  expect(resolveActiveRunIdForMessages(checkpointMessages, true, null)).toBe(
    "run-current",
  );
  expect(
    resolveActiveRunIdForMessages(checkpointMessages, false, null),
  ).toBeNull();
});

test("mergeMessages removes duplicate messages already present in history", () => {
  const human = {
    id: "human-1",
    type: "human",
    content: "Design an agent",
  } as Message;
  const ai = {
    id: "ai-1",
    type: "ai",
    content: "Let's design it.",
  } as Message;

  expect(mergeMessages([human, ai, human, ai], [], [])).toEqual([human, ai]);
});

test("mergeMessages lets live thread messages replace overlapping history", () => {
  const oldHuman = {
    id: "human-1",
    type: "human",
    content: "old",
  } as Message;
  const liveHuman = {
    id: "human-1",
    type: "human",
    content: "live",
  } as Message;
  const oldAi = {
    id: "ai-1",
    type: "ai",
    content: "old",
  } as Message;
  const liveAi = {
    id: "ai-1",
    type: "ai",
    content: "live",
  } as Message;

  expect(mergeMessages([oldHuman, oldAi], [liveHuman, liveAi], [])).toEqual([
    liveHuman,
    liveAi,
  ]);
});

test("mergeMessages preserves persisted reasoning timing over a stale live duplicate", () => {
  const historyAi = {
    id: "ai-reasoned",
    type: "ai",
    content: "answer",
    additional_kwargs: {
      reasoning_content: "analysis",
      reasoning_duration_ms: 19_000,
    },
  } as Message;
  const liveAi = {
    id: "ai-reasoned",
    type: "ai",
    content: "answer",
    additional_kwargs: {
      reasoning_content: "analysis",
    },
  } as Message;

  expect(
    mergeMessages([historyAi], [liveAi], [])[0]?.additional_kwargs
      ?.reasoning_duration_ms,
  ).toBe(19_000);
});

test("mergeMessages keeps a newer live reasoning timing over persisted history", () => {
  const historyAi = {
    id: "ai-reasoned-newer",
    type: "ai",
    content: "answer",
    additional_kwargs: { reasoning_duration_ms: 4_000 },
  } as Message;
  const liveAi = {
    id: "ai-reasoned-newer",
    type: "ai",
    content: "answer",
    additional_kwargs: { reasoning_duration_ms: 7_000 },
  } as Message;

  expect(
    mergeMessages([historyAi], [liveAi], [])[0]?.additional_kwargs
      ?.reasoning_duration_ms,
  ).toBe(7_000);
});

test("completed live Run messages expose exact run_id without a page refresh", () => {
  const priorAi = {
    id: "ai-prior",
    type: "ai",
    content: "prior answer",
  } as Message;
  const currentAi = {
    id: "ai-current",
    type: "ai",
    content: "current answer",
  } as Message;
  const scopedLive = attachRunIdToNewMessages(
    [priorAi, currentAi],
    "run-current",
    new Set(["message:ai-prior"]),
  );
  const merged = mergeMessages(
    [{ ...priorAi, run_id: "run-prior" } as unknown as Message],
    scopedLive,
    [],
  );

  expect(Reflect.get(merged[0]!, "run_id")).toBe("run-prior");
  expect(Reflect.get(merged[1]!, "run_id")).toBe("run-current");
});

test("live Run scoping preserves an authoritative existing run_id", () => {
  const alreadyScoped = {
    id: "ai-scoped",
    type: "ai",
    content: "persisted",
    run_id: "run-authoritative",
  } as Message;

  expect(
    Reflect.get(
      attachRunIdToNewMessages([alreadyScoped], "run-current", new Set())[0]!,
      "run_id",
    ),
  ).toBe("run-authoritative");
});

test("mergeMessages deduplicates tool messages by tool_call_id", () => {
  const oldTool = {
    id: "tool-message-old",
    type: "tool",
    tool_call_id: "call-1",
    content: "old",
  } as Message;
  const liveTool = {
    id: "tool-message-live",
    type: "tool",
    tool_call_id: "call-1",
    content: "live",
  } as Message;

  expect(mergeMessages([oldTool], [liveTool], [])).toEqual([liveTool]);
});

test("mergeMessages keeps repeated tool_call_id values from different runs", () => {
  const firstRunTool = {
    id: "tool-message-run-a",
    type: "tool",
    tool_call_id: "call-1",
    run_id: "run-a",
    content: "run-a output",
  } as unknown as Message;
  const secondRunTool = {
    id: "tool-message-run-b",
    type: "tool",
    tool_call_id: "call-1",
    additional_kwargs: { run_id: "run-b" },
    content: "run-b output",
  } as Message;

  expect(mergeMessages([firstRunTool, secondRunTool], [], [])).toEqual([
    firstRunTool,
    secondRunTool,
  ]);
});

test("history hydration does not pair the latest clarification with an older checkpoint human", () => {
  const historyLatest = [
    {
      id: "human-latest",
      type: "human",
      content: "ask the latest question",
      run_id: "run-latest",
    },
    {
      id: "ai-latest",
      type: "ai",
      content: "",
      run_id: "run-latest",
    },
    clarificationToolMessage(
      "clarification:latest",
      "call-latest",
      "run-latest",
    ),
  ] as unknown as Message[];
  const checkpoint = [
    {
      id: "human-first",
      type: "human",
      content: "the first old prompt",
      additional_kwargs: { run_id: "run-first" },
    },
    { id: "ai-first", type: "ai", content: "old answer" },
    {
      id: "human-older-clarification",
      type: "human",
      content: "ask the older question",
      additional_kwargs: { run_id: "run-older-clarification" },
    },
    { id: "ai-older-clarification", type: "ai", content: "" },
    clarificationToolMessage("clarification:older", "call-older"),
    {
      id: "human-reply",
      type: "human",
      content: "ordinary reply to the older question",
      additional_kwargs: { run_id: "run-reply" },
    },
    { id: "ai-reply", type: "ai", content: "continued" },
    {
      id: "human-latest",
      type: "human",
      content: "ask the latest question",
      additional_kwargs: { run_id: "run-latest" },
    },
    { id: "ai-latest", type: "ai", content: "" },
    clarificationToolMessage("clarification:latest", "call-latest"),
  ] as unknown as Message[];
  const runsNewestFirst = [
    threadRun("run-latest"),
    threadRun("run-reply"),
    threadRun("run-older-clarification"),
    threadRun("run-first"),
  ];

  const merged = mergeMessages(historyLatest, checkpoint, [], runsNewestFirst);
  const state = deriveHumanInputThreadState(merged);

  expect(
    merged.filter(
      (message) =>
        message.type === "tool" &&
        Reflect.get(message, "tool_call_id") === "call-latest",
    ),
  ).toHaveLength(1);
  expect(state.answeredResponses.get("clarification:older")?.value).toBe(
    "ordinary reply to the older question",
  );
  expect(state.answeredResponses.has("clarification:latest")).toBe(false);
  expect(state.latestOpenRequestId).toBe("clarification:latest");
});

test("a stale live duplicate keeps its canonical history position before a later clarification", () => {
  const historyHuman = {
    id: "human-first",
    type: "human",
    content: "the first old prompt",
    run_id: "run-first",
  } as unknown as Message;
  const staleLiveHuman = {
    id: "human-first",
    type: "human",
    content: "the first old prompt",
    additional_kwargs: { run_id: "run-first" },
  } as Message;
  const latestRequest = clarificationToolMessage(
    "clarification:latest",
    "call-latest",
    "run-latest",
  );

  const merged = mergeMessages(
    [historyHuman, latestRequest],
    [staleLiveHuman],
    [],
    [threadRun("run-latest"), threadRun("run-first")],
  );
  const state = deriveHumanInputThreadState(merged);

  expect(merged.map((message) => message.id)).toEqual([
    "human-first",
    "clarification:latest",
  ]);
  expect(state.answeredResponses.has("clarification:latest")).toBe(false);
  expect(state.latestOpenRequestId).toBe("clarification:latest");
});

test("compacted checkpoint tail keeps the Run admission human at the start of its history", () => {
  const historyMessages = [
    {
      id: "human-early",
      type: "human",
      content: "early sentinel",
    },
    {
      id: "ai-early",
      type: "ai",
      content: "early answer",
    },
    {
      id: "human-middle",
      type: "human",
      content: "middle sentinel",
    },
    {
      id: "ai-middle",
      type: "ai",
      content: "middle answer",
    },
    {
      id: "human-late",
      type: "human",
      content: "late sentinel",
    },
    {
      id: "ai-late",
      type: "ai",
      content: "persisted late answer",
    },
  ] as Message[];
  const history = buildVisibleHistoryMessages(
    historyMessages.map((content, index) => ({
      run_id: "run-history",
      seq: String(index + 1),
      content,
      metadata: {
        caller: "lead_agent",
        ...(index === 0 ? { source: "run_admission" as const } : {}),
      },
      created_at: `2026-07-23T00:00:0${index}Z`,
    })),
    new Set(),
    [],
  );
  const checkpointTail = [
    {
      ...historyMessages[4],
      content: "live late sentinel",
    },
    {
      ...historyMessages[5],
      content: "live late answer",
    },
  ] as Message[];
  const optimisticHuman = {
    id: "human-recall",
    type: "human",
    content: "recall every sentinel",
  } as Message;

  const merged = mergeMessages(
    history,
    checkpointTail,
    [optimisticHuman],
    [threadRun("run-history")],
  );

  expect(merged.map((message) => message.id)).toEqual([
    "human-early",
    "ai-early",
    "human-middle",
    "ai-middle",
    "human-late",
    "ai-late",
    "human-recall",
  ]);
  expect(merged[4]?.content).toBe("live late sentinel");
  expect(merged[5]?.content).toBe("live late answer");
});

test("mergeMessages keeps a visible history message when a hidden live message reuses its id", () => {
  const historyHuman = {
    id: "human-1",
    type: "human",
    content: "visible user prompt",
  } as Message;
  const hiddenReminder = {
    id: "human-1",
    type: "human",
    content: "<system-reminder>hidden</system-reminder>",
    additional_kwargs: { hide_from_ui: true },
  } as Message;
  const liveAi = {
    id: "ai-1",
    type: "ai",
    content: "live answer",
  } as Message;

  expect(mergeMessages([historyHuman], [hiddenReminder, liveAi], [])).toEqual([
    historyHuman,
    liveAi,
  ]);
});

test("mergeMessages lets a visible live message replace overlapping hidden history", () => {
  const hiddenHistoryHuman = {
    id: "human-1",
    type: "human",
    content: "<system-reminder>hidden</system-reminder>",
    additional_kwargs: { hide_from_ui: true },
  } as Message;
  const liveHuman = {
    id: "human-1",
    type: "human",
    content: "visible user prompt",
  } as Message;

  expect(mergeMessages([hiddenHistoryHuman], [liveHuman], [])).toEqual([
    liveHuman,
  ]);
});

test("getSummarizationMiddlewareMessages matches ActWeave summarization update keys", () => {
  const removeAll = {
    id: "__remove_all__",
    type: "remove",
    content: "",
  } as Message;
  const summary = {
    id: "summary-1",
    type: "human",
    name: "summary",
    content: "summary",
  } as Message;

  expect(
    getSummarizationMiddlewareMessages({
      "DeerFlowSummarizationMiddleware.before_model": {
        messages: [removeAll, summary],
      },
    }),
  ).toEqual([removeAll, summary]);
});

test("getSummarizationMiddlewareMessages matches base LangChain summarization update keys", () => {
  const summary = {
    id: "summary-1",
    type: "human",
    name: "summary",
    content: "summary",
  } as Message;

  expect(
    getSummarizationMiddlewareMessages({
      "SummarizationMiddleware.before_model": {
        messages: [summary],
      },
    }),
  ).toEqual([summary]);
});

test("getSummarizationMiddlewareMessages ignores unrelated suffix-sharing update keys", () => {
  const summary = {
    id: "summary-1",
    type: "human",
    name: "summary",
    content: "summary",
  } as Message;

  expect(
    getSummarizationMiddlewareMessages({
      "OtherSummarizationMiddleware.before_model": {
        messages: [summary],
      },
    }),
  ).toBeUndefined();
});

test("getVisibleOptimisticMessages hides optimistic user input after server human arrives", () => {
  const optimisticHuman = {
    id: "opt-human-1",
    type: "human",
    content: "hello",
  } as Message;

  expect(getVisibleOptimisticMessages([optimisticHuman], 0, 1)).toEqual([]);
});

test("mergeMessages shows server human instead of optimistic duplicate after first response", () => {
  const serverHuman = {
    id: "server-human-1",
    type: "human",
    content: "hello",
  } as Message;
  const optimisticHuman = {
    id: "opt-human-1",
    type: "human",
    content: "hello",
  } as Message;
  const visibleOptimistic = getVisibleOptimisticMessages(
    [optimisticHuman],
    0,
    1,
  );

  expect(mergeMessages([], [serverHuman], visibleOptimistic)).toEqual([
    serverHuman,
  ]);
});

test("mergeMessages deduplicates a failed admission prompt against its live state copy by stable id", () => {
  const priorHuman = {
    id: "human-1",
    type: "human",
    content: "first question",
  } as Message;
  const priorAi = {
    id: "ai-1",
    type: "ai",
    content: "first answer",
  } as Message;
  const failedAdmission = {
    id: "human-2",
    type: "human",
    content: "second question",
    run_id: "run-2",
  } as unknown as Message;
  const failedLive = {
    id: "human-2",
    type: "human",
    content: "second question",
  } as Message;

  expect(
    mergeMessages(
      [priorHuman, priorAi, failedAdmission],
      [priorHuman, priorAi, failedLive],
      [],
    ).map((message) => message.id),
  ).toEqual(["human-1", "ai-1", "human-2"]);
});

test("mergeMessages removes a legacy failed admission fallback when live state owns the same run", () => {
  const priorHuman = {
    id: "human-1",
    type: "human",
    content: "first question",
  } as Message;
  const priorAi = {
    id: "ai-1",
    type: "ai",
    content: "first answer",
  } as Message;
  const failedAdmission = {
    id: "run-admission-run-2",
    type: "human",
    content: "second question",
    run_id: "run-2",
  } as unknown as Message;
  const failedLive = {
    id: "generated-live-id",
    type: "human",
    content: "second question",
    additional_kwargs: { run_id: "run-2" },
  } as Message;

  expect(
    mergeMessages(
      [priorHuman, priorAi, failedAdmission],
      [priorHuman, priorAi, failedLive],
      [],
    ).map((message) => message.id),
  ).toEqual(["human-1", "ai-1", "generated-live-id"]);
});

test("mergeMessages appends the newest failed admission after an older live checkpoint", () => {
  const priorHuman = {
    id: "human-1",
    type: "human",
    content: "first question",
  } as Message;
  const priorAi = {
    id: "ai-1",
    type: "ai",
    content: "first answer",
  } as Message;
  const failedAdmission = {
    id: "human-2",
    type: "human",
    content: "second question",
    run_id: "run-2",
    run_message_source: "run_admission",
  } as unknown as Message;
  const runs = [{ run_id: "run-2" }] as unknown as Run[];

  expect(
    mergeMessages([failedAdmission], [priorHuman, priorAi], [], runs).map(
      (message) => message.id,
    ),
  ).toEqual(["human-1", "ai-1", "human-2"]);
});

test("mergeMessages preserves intentionally repeated prompts with different ids", () => {
  const first = {
    id: "human-repeat-1",
    type: "human",
    content: "repeat this",
  } as Message;
  const second = {
    id: "human-repeat-2",
    type: "human",
    content: "repeat this",
  } as Message;

  expect(mergeMessages([first, second], [], [])).toEqual([first, second]);
});

test("getVisibleOptimisticMessages keeps optimistic user input until server human arrives", () => {
  const optimisticHuman = {
    id: "opt-human-1",
    type: "human",
    content: "hello",
  } as Message;

  expect(getVisibleOptimisticMessages([optimisticHuman], 0, 0)).toEqual([
    optimisticHuman,
  ]);
});

test("getVisibleOptimisticMessages keeps non-human optimistic status messages", () => {
  const optimisticAi = {
    id: "opt-ai-1",
    type: "ai",
    content: "Uploading files...",
  } as Message;

  expect(getVisibleOptimisticMessages([optimisticAi], 0, 1)).toEqual([
    optimisticAi,
  ]);
});

test("getVisibleOptimisticMessages hides the upload optimistic pair after server human arrives", () => {
  const optimisticHuman = {
    id: "opt-human-1",
    type: "human",
    content: "upload this",
  } as Message;
  const optimisticUploadingAi = {
    id: "opt-ai-uploading",
    type: "ai",
    content: "Uploading files...",
  } as Message;

  expect(
    getVisibleOptimisticMessages(
      [optimisticHuman, optimisticUploadingAi],
      0,
      1,
    ),
  ).toEqual([]);
});

test("getVisibleOptimisticMessages hides optimistic user input after later server turns", () => {
  const optimisticHuman = {
    id: "opt-human-2",
    type: "human",
    content: "follow up",
  } as Message;

  expect(getVisibleOptimisticMessages([optimisticHuman], 3, 4)).toEqual([]);
  expect(getVisibleOptimisticMessages([optimisticHuman], 3, 3)).toEqual([
    optimisticHuman,
  ]);
});

test("runMessagesPageHasMore reads backend snake_case pagination field", () => {
  expect(runMessagesPageHasMore({ data: [], has_more: true })).toBe(true);
  expect(runMessagesPageHasMore({ data: [], has_more: false })).toBe(false);
});

test("getOldestRunMessageSeq returns the cursor for the next older run page", () => {
  expect(
    getOldestRunMessageSeq([
      runMessage("9007199254740993"),
      runMessage("9007199254740994"),
      runMessage("9007199254740995"),
    ]),
  ).toBe("9007199254740993");
});

test("getOldestRunMessageSeq ignores rows without seq", () => {
  expect(getOldestRunMessageSeq([runMessage()])).toBeNull();
});

test("getNextRunMessagesBeforeSeq keeps runs pending when has_more lacks seq", () => {
  expect(
    getNextRunMessagesBeforeSeq({ data: [runMessage()], has_more: true }),
  ).toBeUndefined();
});

test("getNextRunMessagesBeforeSeq marks runs loaded when no more pages exist", () => {
  expect(
    getNextRunMessagesBeforeSeq({ data: [runMessage()], has_more: false }),
  ).toBeNull();
});

test("buildRunMessagesUrl encodes path segments and optional before_seq", () => {
  expect(
    buildRunMessagesUrl(
      "https://api.example.test/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/",
      "thread/with space",
      "run?one",
      "18",
    ),
  ).toBe(
    "https://api.example.test/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/threads/thread%2Fwith%20space/runs/run%3Fone/messages?before_seq=18",
  );
});

test("buildRunMessagesUrl omits before_seq when loading the latest page", () => {
  expect(
    buildRunMessagesUrl(
      "https://api.example.test/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work",
      "thread-1",
      "run-1",
    ),
  ).toBe(
    "https://api.example.test/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/threads/thread-1/runs/run-1/messages",
  );
});

test("buildRunMessagesUrl rejects a non-project API root", () => {
  // @ts-expect-error The runtime guard must reject legacy numeric cursors.
  expect(() => buildRunMessagesUrl("/api", "thread-1", "run-1", 42)).toThrow();
});

test("findLatestUnloadedRunIndex loads the newest run first from a newest-first list", () => {
  const runs = [
    { run_id: "R6" },
    { run_id: "R5" },
    { run_id: "R4" },
    { run_id: "R3" },
    { run_id: "R2" },
    { run_id: "R1" },
  ] as unknown as Run[];
  expect(findLatestUnloadedRunIndex(runs, new Set())).toBe(0);
});

test("findLatestUnloadedRunIndex skips already-loaded runs and returns the next newest unloaded run", () => {
  const runs = [
    { run_id: "R6" },
    { run_id: "R5" },
    { run_id: "R4" },
  ] as unknown as Run[];
  expect(findLatestUnloadedRunIndex(runs, new Set(["R6"]))).toBe(1);
});

test("findLatestUnloadedRunIndex returns -1 when every run is already loaded", () => {
  const runs = [{ run_id: "R2" }, { run_id: "R1" }] as unknown as Run[];
  expect(findLatestUnloadedRunIndex(runs, new Set(["R1", "R2"]))).toBe(-1);
});

test("mergeRunMessageRows keeps a newly discovered latest run after older loaded runs", () => {
  const runs = [
    { run_id: "R3" },
    { run_id: "R2" },
    { run_id: "R1" },
  ] as unknown as Run[];
  const previous = [
    orderedRunMessage("R1", "10", "R1-human", "first"),
    orderedRunMessage("R2", "20", "R2-human", "second"),
  ];
  const incoming = [orderedRunMessage("R3", "30", "R3-human", "third")];

  expect(
    mergeRunMessageRows(previous, incoming, runs).map(
      (message) => message.run_id,
    ),
  ).toEqual(["R1", "R2", "R3"]);
});

test("mergeRunMessageRows orders older pages by seq inside the same run", () => {
  const runs = [{ run_id: "R1" }] as unknown as Run[];
  const previous = [orderedRunMessage("R1", "30", "R1-30", "latest page")];
  const incoming = [
    orderedRunMessage("R1", "10", "R1-10", "oldest page"),
    orderedRunMessage("R1", "20", "R1-20", "middle page"),
  ];

  expect(
    mergeRunMessageRows(previous, incoming, runs).map((message) => message.seq),
  ).toEqual(["10", "20", "30"]);
});

test("mergeRunMessageRows keeps adjacent sequences above Number.MAX_SAFE_INTEGER distinct", () => {
  const runs = [{ run_id: "R1" }] as unknown as Run[];
  const previous = [
    orderedRunMessage("R1", "9007199254740994", "R1-newer", "newer"),
  ];
  const incoming = [
    orderedRunMessage("R1", "9007199254740993", "R1-older", "older"),
  ];

  expect(
    mergeRunMessageRows(previous, incoming, runs).map((message) => message.seq),
  ).toEqual(["9007199254740993", "9007199254740994"]);
});

test("getSupersededRunIds combines completed regenerate metadata with pending ids", () => {
  const runs = [
    {
      run_id: "run-new",
      status: "success",
      metadata: { regenerate_from_run_id: "run-old" },
    },
    {
      run_id: "run-normal",
      status: "success",
      metadata: {},
    },
  ] as unknown as Run[];

  expect(getSupersededRunIds(runs, new Set(["run-pending"]))).toEqual(
    new Set(["run-old", "run-pending"]),
  );
});

test("getSupersededRunIds ignores failed regenerate runs but keeps pending ids", () => {
  const runs = [
    {
      run_id: "run-error",
      status: "error",
      metadata: { regenerate_from_run_id: "run-old" },
    },
    {
      run_id: "run-interrupted",
      status: "interrupted",
      metadata: { regenerate_from_run_id: "run-older" },
    },
  ] as unknown as Run[];

  expect(getSupersededRunIds(runs, new Set(["run-pending"]))).toEqual(
    new Set(["run-pending"]),
  );
});

test("latestRunHasTerminalFailure restores only the newest failed Run state", () => {
  for (const status of ["error", "failed", "timeout"]) {
    expect(latestRunHasTerminalFailure([{ status }] as unknown as Run[])).toBe(
      true,
    );
  }
  for (const status of ["success", "interrupted", "running", "pending"]) {
    expect(latestRunHasTerminalFailure([{ status }] as unknown as Run[])).toBe(
      false,
    );
  }
  expect(latestRunHasTerminalFailure(undefined)).toBe(false);
  expect(
    latestRunHasTerminalFailure([
      { status: "success" },
      { status: "error" },
    ] as unknown as Run[]),
  ).toBe(false);
});

test("replay rollback removes only the failed replay and preserves earlier successful masks", () => {
  const replay: PendingPreparedReplayMask = {
    kind: "edit",
    targetRunId: "run-old",
    supersededMessageIds: ["message-old"],
    replacementHumanMessageId: "replacement-failed",
  };

  expect(
    getRunMasksAfterPreparedReplayFailure(
      new Set(["run-old", "run-other"]),
      replay,
      "run-failed",
    ),
  ).toEqual(new Set(["run-other", "run-failed"]));
  expect(
    getMessageMasksAfterPreparedReplayFailure(
      new Set(["message-old", "message-other", "replacement-other"]),
      replay,
    ),
  ).toEqual(
    new Set(["message-other", "replacement-other", "replacement-failed"]),
  );
});

test("prepared replay waits for initial SDK thread state before opening its error-attribution window", () => {
  expect(
    canStartPreparedReplay({
      threadId: "thread-1",
      sendInFlight: false,
      isThreadLoading: false,
    }),
  ).toBe(true);
  expect(
    canStartPreparedReplay({
      threadId: "thread-1",
      sendInFlight: false,
      isThreadLoading: true,
    }),
  ).toBe(false);
  expect(
    canStartPreparedReplay({
      threadId: "thread-1",
      sendInFlight: true,
      isThreadLoading: false,
    }),
  ).toBe(false);
  expect(
    canStartPreparedReplay({
      threadId: null,
      sendInFlight: false,
      isThreadLoading: false,
    }),
  ).toBe(false);
});

test("stopping a created replay rolls back with its actual Run ID", () => {
  const replay: PendingPreparedReplayMask = {
    kind: "edit",
    targetRunId: "run-old",
    supersededMessageIds: ["human-old", "ai-old"],
    replacementHumanMessageId: "human-replacement",
  };

  expect(
    getPreparedReplayStopRollback({
      replay,
      createdRunId: "run-created-before-stop",
      status: "submitting",
    }),
  ).toEqual({
    replay,
    failedRunId: "run-created-before-stop",
  });
  expect(
    getPreparedReplayStopRollback({
      replay,
      createdRunId: "run-created-before-stop",
      status: "succeeded",
    }),
  ).toBeUndefined();
  expect(getPreparedReplayStopRollback(null)).toBeUndefined();
});

test("failed or stopped replay hides only live messages owned by its Run", () => {
  const directRunMessage = {
    id: "ai-failed",
    type: "ai",
    content: "partial failed answer",
    run_id: "run-failed",
  } as Message;
  const additionalRunMessage = {
    id: "tool-stopped",
    type: "tool",
    content: "partial stopped tool output",
    tool_call_id: "call-stopped",
    additional_kwargs: { run_id: "run-stopped" },
  } as Message;
  const unrelatedMessage = {
    id: "ai-kept",
    type: "ai",
    content: "kept answer",
    run_id: "run-kept",
  } as Message;
  const unscopedMessage = {
    id: "human-kept",
    type: "human",
    content: "kept question",
  } as Message;

  expect(
    filterMessagesBySupersededRunIds(
      [
        directRunMessage,
        additionalRunMessage,
        unrelatedMessage,
        unscopedMessage,
      ],
      new Set(["run-failed", "run-stopped"]),
    ),
  ).toEqual([unrelatedMessage, unscopedMessage]);
});

test("prepared replay SDK errors distinguish pre-create, terminal, and post-success history failures", () => {
  const preCreateError = new Error("run admission unavailable");
  const preCreateDecision = classifyPreparedReplaySdkError({
    createdRunId: null,
    callbackRunId: null,
    error: preCreateError,
    historyRefetchError: null,
  });
  expect(preCreateDecision).toEqual({
    kind: "rollback",
    failedRunId: null,
  });
  expect(preCreateDecision).not.toEqual({
    kind: "rollback",
    failedRunId: "run-stale-from-an-earlier-submit",
  });

  const terminalError = new Error("run failed");
  expect(
    classifyPreparedReplaySdkError({
      createdRunId: "run-replay",
      callbackRunId: "run-replay",
      error: terminalError,
      historyRefetchError: null,
    }),
  ).toEqual({
    kind: "rollback",
    failedRunId: "run-replay",
  });

  const historyRefetchError = new Error("state refetch failed");
  expect(
    classifyPreparedReplaySdkError({
      createdRunId: "run-replay",
      callbackRunId: null,
      error: historyRefetchError,
      historyRefetchError: null,
    }),
  ).toEqual({
    kind: "history-refetch-failure",
    error: historyRefetchError,
  });
  expect(
    classifyPreparedReplaySdkError({
      createdRunId: "run-replay",
      callbackRunId: "run-replay",
      error: historyRefetchError,
      historyRefetchError,
    }),
  ).toEqual({
    kind: "ignore-history-refetch-duplicate",
  });
});

test("buildVisibleHistoryMessages filters superseded runs but keeps regenerated run", () => {
  const oldHuman = {
    id: "human-1",
    type: "human",
    content: "question",
  } as Message;
  const oldAi = {
    id: "ai-old",
    type: "ai",
    content: "old answer",
  } as Message;
  const newHuman = {
    id: "human-1",
    type: "human",
    content: "question",
  } as Message;
  const newAi = {
    id: "ai-new",
    type: "ai",
    content: "new answer",
  } as Message;
  const rows: RunMessage[] = [
    {
      run_id: "run-old",
      content: oldHuman,
      metadata: { caller: "lead_agent" },
      created_at: "2026-06-18T00:00:00Z",
    },
    {
      run_id: "run-old",
      content: oldAi,
      metadata: { caller: "lead_agent" },
      created_at: "2026-06-18T00:00:01Z",
    },
    {
      run_id: "run-new",
      content: newHuman,
      metadata: { caller: "lead_agent" },
      created_at: "2026-06-18T00:00:02Z",
    },
    {
      run_id: "run-new",
      content: newAi,
      metadata: { caller: "lead_agent" },
      created_at: "2026-06-18T00:00:03Z",
    },
  ];

  // run_id is carried onto each content message (#3779) so historical subtask
  // cards can fetch their persisted step history on expand.
  expect(buildVisibleHistoryMessages(rows, new Set(["run-old"]), [])).toEqual([
    { ...newHuman, run_id: "run-new" },
    { ...newAi, run_id: "run-new" },
  ]);
});

test("buildVisibleHistoryMessages attaches run_id to each content message (#3779)", () => {
  const rows: RunMessage[] = [
    {
      run_id: "run-1",
      content: { id: "ai-1", type: "ai", content: "answer" } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-06-26T00:00:00Z",
    },
  ];

  const result = buildVisibleHistoryMessages(rows, new Set(), []);

  expect((result[0] as { run_id?: string }).run_id).toBe("run-1");
});

test("buildVisibleHistoryMessages hides subagent and middleware AI messages with their tool results", () => {
  const rows: RunMessage[] = [
    {
      run_id: "run-1",
      seq: "1",
      content: {
        id: "human-1",
        type: "human",
        content: "research agents",
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:00Z",
    },
    {
      run_id: "run-1",
      seq: "2",
      content: {
        id: "lead-ai",
        type: "ai",
        content: "",
        tool_calls: [{ id: "call-task", name: "task", args: {} }],
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:01Z",
    },
    {
      run_id: "run-1",
      seq: "3",
      content: {
        id: "subagent-ai",
        type: "ai",
        content: "private subagent reasoning",
        tool_calls: [{ id: "call-search", name: "web_search", args: {} }],
      } as Message,
      metadata: { caller: "subagent:research" },
      created_at: "2026-07-26T00:00:02Z",
    },
    {
      run_id: "run-1",
      seq: "4",
      content: {
        id: "subagent-tool",
        type: "tool",
        tool_call_id: "call-search",
        content: "large raw search result",
      } as Message,
      metadata: { caller: "subagent:research" },
      created_at: "2026-07-26T00:00:03Z",
    },
    {
      run_id: "run-1",
      seq: "5",
      content: {
        id: "middleware-ai",
        type: "ai",
        content: "middleware detail",
        additional_kwargs: {
          tool_calls: [{ id: "call-middleware", type: "function" }],
        },
      } as Message,
      metadata: { caller: "middleware:summarization" },
      created_at: "2026-07-26T00:00:04Z",
    },
    {
      run_id: "run-1",
      seq: "6",
      content: {
        id: "middleware-tool",
        type: "tool",
        tool_call_id: "call-middleware",
        content: "middleware tool result",
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:05Z",
    },
    {
      run_id: "run-1",
      seq: "7",
      content: {
        id: "lead-tool",
        type: "tool",
        tool_call_id: "call-task",
        content: "subtask completed",
      } as Message,
      metadata: { caller: "subagent:research" },
      created_at: "2026-07-26T00:00:06Z",
    },
    {
      run_id: "run-1",
      seq: "8",
      content: {
        id: "lead-final",
        type: "ai",
        content: "final answer",
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:07Z",
    },
  ];

  expect(
    buildVisibleHistoryMessages(rows, new Set(), []).map(
      (message) => message.id,
    ),
  ).toEqual(["human-1", "lead-ai", "lead-tool", "lead-final"]);
});

test("buildVisibleHistoryMessages keeps human run admission messages regardless of caller", () => {
  const rows: RunMessage[] = [
    {
      run_id: "run-1",
      seq: "1",
      content: {
        id: "run-admission-run-1",
        type: "human",
        content: "user prompt",
      } as Message,
      metadata: {
        caller: "middleware:run-admission",
        source: "run_admission",
      },
      created_at: "2026-07-26T00:00:00Z",
    },
  ];

  expect(buildVisibleHistoryMessages(rows, new Set(), [])).toEqual([
    {
      ...rows[0]!.content,
      run_id: "run-1",
      run_message_source: "run_admission",
    },
  ]);
});

test("buildVisibleHistoryMessages hides internal human messages except run admission", () => {
  const rows: RunMessage[] = [
    {
      run_id: "run-1",
      seq: "1",
      content: {
        id: "lead-human",
        type: "human",
        content: "visible user prompt",
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:00Z",
    },
    {
      run_id: "run-1",
      seq: "2",
      content: {
        id: "subagent-human",
        type: "human",
        content: "private subagent input",
      } as Message,
      metadata: { caller: "subagent:research" },
      created_at: "2026-07-26T00:00:01Z",
    },
    {
      run_id: "run-1",
      seq: "3",
      content: {
        id: "middleware-human",
        type: "human",
        content: "private middleware input",
      } as Message,
      metadata: { caller: "middleware:summarization" },
      created_at: "2026-07-26T00:00:02Z",
    },
    {
      run_id: "run-1",
      seq: "4",
      content: {
        id: "run-admission-run-1",
        type: "human",
        content: "admitted user prompt",
      } as Message,
      metadata: {
        caller: "middleware:run-admission",
        source: "run_admission",
      },
      created_at: "2026-07-26T00:00:03Z",
    },
  ];

  expect(
    buildVisibleHistoryMessages(rows, new Set(), []).map(
      (message) => message.id,
    ),
  ).toEqual(["lead-human", "run-admission-run-1"]);
});

test("buildVisibleHistoryMessages defers an orphan tool result until its lead parent page loads", () => {
  const toolResult: RunMessage = {
    run_id: "run-1",
    seq: "20",
    content: {
      id: "tool-1",
      type: "tool",
      tool_call_id: "call-1",
      content: "lead tool result",
    } as Message,
    metadata: { caller: "lead_agent" },
    created_at: "2026-07-26T00:00:01Z",
  };
  const leadParent: RunMessage = {
    run_id: "run-1",
    seq: "10",
    content: {
      id: "ai-1",
      type: "ai",
      content: "",
      tool_calls: [{ id: "call-1", name: "task", args: {} }],
    } as Message,
    metadata: { caller: "lead_agent" },
    created_at: "2026-07-26T00:00:00Z",
  };

  expect(buildVisibleHistoryMessages([toolResult], new Set(), [])).toEqual([]);
  expect(
    buildVisibleHistoryMessages([leadParent, toolResult], new Set(), []).map(
      (message) => message.id,
    ),
  ).toEqual(["ai-1", "tool-1"]);
});

test("buildVisibleHistoryMessages scopes tool-call ownership to its run", () => {
  const rows: RunMessage[] = [
    {
      run_id: "run-subagent",
      seq: "1",
      content: {
        id: "subagent-ai",
        type: "ai",
        content: "",
        tool_calls: [{ id: "shared-call", name: "search", args: {} }],
      } as Message,
      metadata: { caller: "subagent:research" },
      created_at: "2026-07-26T00:00:00Z",
    },
    {
      run_id: "run-subagent",
      seq: "2",
      content: {
        id: "subagent-tool",
        type: "tool",
        tool_call_id: "shared-call",
        content: "hidden",
      } as Message,
      metadata: { caller: "subagent:research" },
      created_at: "2026-07-26T00:00:01Z",
    },
    {
      run_id: "run-lead",
      seq: "1",
      content: {
        id: "lead-ai",
        type: "ai",
        content: "",
        tool_calls: [{ id: "shared-call", name: "task", args: {} }],
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:02Z",
    },
    {
      run_id: "run-lead",
      seq: "2",
      content: {
        id: "lead-tool",
        type: "tool",
        tool_call_id: "shared-call",
        content: "visible",
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-07-26T00:00:03Z",
    },
  ];

  expect(
    buildVisibleHistoryMessages(rows, new Set(), []).map(
      (message) => message.id,
    ),
  ).toEqual(["lead-ai", "lead-tool"]);
});

test("loading runs in newest-first order and prepending pages yields chronological messages (regression for #3352)", () => {
  // Simulate backend list_by_thread returning newest first.
  const runs = [
    { run_id: "R6" },
    { run_id: "R5" },
    { run_id: "R4" },
    { run_id: "R3" },
    { run_id: "R2" },
    { run_id: "R1" },
  ] as unknown as Run[];
  const runIdToContent: Record<string, string> = {
    R1: "A",
    R2: "B",
    R3: "C",
    R4: "D",
    R5: "E",
    R6: "F",
  };

  const loaded = new Set<string>();
  let messages: Message[] = [];

  while (true) {
    const index = findLatestUnloadedRunIndex(runs, loaded);
    if (index === -1) break;
    const run = runs[index]!;
    const pageMessages = [
      {
        id: run.run_id,
        type: "human",
        content: runIdToContent[run.run_id],
      } as Message,
    ];
    // Mirror loadMessages: prepend new page to existing messages.
    messages = [...pageMessages, ...messages];
    loaded.add(run.run_id);
  }

  expect(messages.map((m) => m.content)).toEqual([
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
  ]);
});

test("shouldAutoContinueOnEmptyRun does not continue when the run produced messages", () => {
  expect(shouldAutoContinueOnEmptyRun(3, 0)).toBe(false);
  expect(shouldAutoContinueOnEmptyRun(1, 4)).toBe(false);
});

test("shouldAutoContinueOnEmptyRun continues when an empty run is below the safety cap", () => {
  expect(shouldAutoContinueOnEmptyRun(0, 0)).toBe(true);
  expect(
    shouldAutoContinueOnEmptyRun(0, MAX_CONSECUTIVE_EMPTY_RUN_LOADS - 1),
  ).toBe(true);
});

test("shouldAutoContinueOnEmptyRun stops once consecutive empty loads reach the cap", () => {
  expect(shouldAutoContinueOnEmptyRun(0, MAX_CONSECUTIVE_EMPTY_RUN_LOADS)).toBe(
    false,
  );
  expect(
    shouldAutoContinueOnEmptyRun(0, MAX_CONSECUTIVE_EMPTY_RUN_LOADS + 1),
  ).toBe(false);
});

test("shouldAutoContinueOnEmptyRun honors a custom safety cap when provided", () => {
  expect(shouldAutoContinueOnEmptyRun(0, 0, 1)).toBe(true);
  expect(shouldAutoContinueOnEmptyRun(0, 1, 1)).toBe(false);
});

test("simulating auto-continue across empty runs skips empty contributions and lands on the next run with content (issue #3352 follow-up)", () => {
  const runs = [
    { run_id: "R6" },
    { run_id: "R5" },
    { run_id: "R4" },
    { run_id: "R3" },
    { run_id: "R2" },
    { run_id: "R1" },
  ] as unknown as Run[];
  const runIdToMessages: Record<string, Message[]> = {
    R6: [{ id: "R6", type: "human", content: "F" } as Message],
    R5: [{ id: "R5", type: "human", content: "E" } as Message],
    R4: [],
    R3: [],
    R2: [],
    R1: [{ id: "R1", type: "human", content: "A" } as Message],
  };

  const loaded = new Set<string>();
  let messages: Message[] = [];

  loaded.add("R6");
  loaded.add("R5");
  messages = [...runIdToMessages.R5!, ...runIdToMessages.R6!];

  let consecutiveEmptyLoads = 0;
  let visited = 0;
  const visitedRunIds: string[] = [];
  while (true) {
    const index = findLatestUnloadedRunIndex(runs, loaded);
    if (index === -1) break;
    const run = runs[index]!;
    visited += 1;
    visitedRunIds.push(run.run_id);
    const pageMessages = runIdToMessages[run.run_id] ?? [];
    messages = [...pageMessages, ...messages];
    loaded.add(run.run_id);
    if (
      !shouldAutoContinueOnEmptyRun(pageMessages.length, consecutiveEmptyLoads)
    ) {
      consecutiveEmptyLoads = 0;
      break;
    }
    consecutiveEmptyLoads += 1;
  }

  expect(visitedRunIds).toEqual(["R4", "R3", "R2", "R1"]);
  expect(visited).toBe(4);
  expect(messages.map((m) => m.content)).toEqual(["A", "E", "F"]);
});

test("shouldAutoContinueOnEmptyRun input must use the post-filter visible count, not the raw page size (middleware-only runs should still trigger auto-continue)", () => {
  const filteredVisibleCount = 0;
  const rawPageSize = 3; // pretend the raw page had 3 middleware-only entries
  expect(shouldAutoContinueOnEmptyRun(filteredVisibleCount, 0)).toBe(true);
  expect(shouldAutoContinueOnEmptyRun(rawPageSize, 0)).toBe(false);
});

// Regression coverage for #3825: after context summarization the backend emits
// RemoveMessage(ALL) + summary + retained, and onUpdateEvent rescues the removed
// messages into history via an async setState. The live thread.messages (an
// external store) and the archived history (React state) update through two
// independent scheduling channels, so a render can observe the post-summary
// (shrunk) thread while the rescued messages have NOT yet landed in
// visibleHistory. resolvePreservedHistory overlays a synchronous archive buffer
// so the merge never loses those messages regardless of the interleaving.

const summarizationHuman1 = {
  id: "human-1",
  type: "human",
  content: "round 1 question",
} as Message;
const summarizationAi1 = {
  id: "ai-1",
  type: "ai",
  content: "round 1 answer",
} as Message;
const summarizationHuman2 = {
  id: "human-2",
  type: "human",
  content: "round 2 question",
} as Message;
const summarizationAi2 = {
  id: "ai-2",
  type: "ai",
  content: "round 2 answer (retained)",
} as Message;
const summarizationMovedMessages = [
  summarizationHuman1,
  summarizationAi1,
  summarizationHuman2,
];

test("resolvePreservedHistory keeps rescued messages while history state is still stale (regression for #3825)", () => {
  // visibleHistory has not yet absorbed the rescued messages (async setState
  // from appendMessages is still pending in this render).
  const staleHistory: Message[] = [];

  expect(
    resolvePreservedHistory(staleHistory, summarizationMovedMessages),
  ).toEqual(summarizationMovedMessages);
});

test("resolvePreservedHistory appends rescued messages after already-loaded history", () => {
  const olderLoadedHuman = {
    id: "older-human",
    type: "human",
    content: "older loaded turn",
  } as Message;

  expect(
    resolvePreservedHistory([olderLoadedHuman], summarizationMovedMessages),
  ).toEqual([olderLoadedHuman, ...summarizationMovedMessages]);
});

test("resolvePreservedHistory does not duplicate or reorder once history state catches up", () => {
  // visibleHistory now contains the rescued messages (appendMessages committed),
  // but the synchronous buffer still holds them this render.
  expect(
    resolvePreservedHistory(
      summarizationMovedMessages,
      summarizationMovedMessages,
    ),
  ).toEqual(summarizationMovedMessages);
});

test("resolvePreservedHistory returns history unchanged when nothing is pending archival", () => {
  const history = [summarizationHuman1, summarizationAi1];
  expect(resolvePreservedHistory(history, [])).toBe(history);
});

test("merge keeps the full conversation across summarization even when visibleHistory lags (regression for #3825)", () => {
  // Hidden summary (name === "summary") + the retained latest answer is all the
  // live thread carries after RemoveMessage(ALL).
  const hiddenSummary = {
    id: "summary-1",
    type: "human",
    name: "summary",
    content: "conversation summary",
  } as Message;
  const postSummaryThread = [hiddenSummary, summarizationAi2];

  // The bad render: visibleHistory is still empty, so without the buffer the
  // rescued round-1/2 messages exist in neither merge input and are lost.
  const effectiveHistory = resolvePreservedHistory(
    [],
    summarizationMovedMessages,
  );
  const merged = mergeMessages(effectiveHistory, postSummaryThread, []);

  expect(merged.map((m) => m.id)).toEqual([
    "human-1",
    "ai-1",
    "human-2",
    "summary-1",
    "ai-2",
  ]);
});

test("pruneConfirmedArchivedMessages drops messages history has absorbed but keeps the rest", () => {
  // History has caught up on the first two rescued messages only.
  expect(
    pruneConfirmedArchivedMessages(summarizationMovedMessages, [
      summarizationHuman1,
      summarizationAi1,
    ]),
  ).toEqual([summarizationHuman2]);
});

test("pruneConfirmedArchivedMessages keeps every pending message while history has not caught up", () => {
  expect(
    pruneConfirmedArchivedMessages(summarizationMovedMessages, []),
  ).toEqual(summarizationMovedMessages);
});

test("resolvePreservedHistory prefers the live history copy over a stale buffered duplicate (#3825 review #3)", () => {
  // Same identity, but the buffered copy is an older snapshot. The live history
  // copy (e.g. the finalized answer) must win — the buffer only fills gaps, it
  // must never overwrite a message history already shows.
  const staleBuffered = {
    id: "ai-1",
    type: "ai",
    content: "streaming partial",
  } as Message;
  const liveFinal = {
    id: "ai-1",
    type: "ai",
    content: "finalized answer",
  } as Message;

  expect(resolvePreservedHistory([liveFinal], [staleBuffered])).toEqual([
    liveFinal,
  ]);
});

test("computeSummarizationMovedMessages returns the live turns dropped before the retained boundary (regression for #3825)", () => {
  const removeAll = {
    id: "__remove_all__",
    type: "remove",
    content: "",
  } as Message;
  const hiddenSummary = {
    id: "summary-1",
    type: "human",
    name: "summary",
    content: "conversation summary",
  } as Message;
  const liveThreadBeforeSummary = [
    summarizationHuman1,
    summarizationAi1,
    summarizationHuman2,
    summarizationAi2,
  ];
  // Summarization emits RemoveMessage(ALL) + hidden summary + retained answer.
  const summarizationMessages = [removeAll, hiddenSummary, summarizationAi2];

  expect(
    computeSummarizationMovedMessages(
      liveThreadBeforeSummary,
      summarizationMessages,
      new Set([hiddenSummary.id!]),
    ),
  ).toEqual([summarizationHuman1, summarizationAi1, summarizationHuman2]);
});

test("computeSummarizationMovedMessages excludes already-summarized control messages", () => {
  const priorSummary = {
    id: "summary-0",
    type: "human",
    name: "summary",
    content: "earlier summary",
  } as Message;
  const liveThreadBeforeSummary = [
    priorSummary,
    summarizationHuman1,
    summarizationAi1,
    summarizationAi2,
  ];
  const summarizationMessages = [
    { id: "__remove_all__", type: "remove", content: "" } as Message,
    {
      id: "summary-1",
      type: "human",
      name: "summary",
      content: "new summary",
    } as Message,
    summarizationAi2,
  ];

  // priorSummary is in the summarized set, so it must not be re-archived.
  expect(
    computeSummarizationMovedMessages(
      liveThreadBeforeSummary,
      summarizationMessages,
      new Set([priorSummary.id!, "summary-1"]),
    ),
  ).toEqual([summarizationHuman1, summarizationAi1]);
});

test("full summarization rescue pipeline keeps the conversation when history state lags (regression for #3825)", () => {
  // Exercises the whole rescue algorithm the hook runs: derive the moved
  // messages, buffer them, then merge against the post-summary thread while the
  // archived-history React state is still stale (empty).
  const removeAll = {
    id: "__remove_all__",
    type: "remove",
    content: "",
  } as Message;
  const hiddenSummary = {
    id: "summary-1",
    type: "human",
    name: "summary",
    content: "conversation summary",
  } as Message;
  const liveThreadBeforeSummary = [
    summarizationHuman1,
    summarizationAi1,
    summarizationHuman2,
    summarizationAi2,
  ];
  const summarizationMessages = [removeAll, hiddenSummary, summarizationAi2];

  const moved = computeSummarizationMovedMessages(
    liveThreadBeforeSummary,
    summarizationMessages,
    new Set([hiddenSummary.id!]),
  );
  const staleHistory: Message[] = [];
  const postSummaryThread = [hiddenSummary, summarizationAi2];

  const merged = mergeMessages(
    resolvePreservedHistory(staleHistory, moved),
    postSummaryThread,
    [],
  );

  expect(merged.map((m) => m.id)).toEqual([
    "human-1",
    "ai-1",
    "human-2",
    "summary-1",
    "ai-2",
  ]);
});

test("thread projection does not eagerly invoke SDK getters during sparse compaction state", () => {
  let toolCallsGetterCount = 0;
  const sdkThread = {
    messages: new Array<Message>(3),
    values: { messages: new Array<Message>(3) },
    stop: () => undefined,
    get toolCalls() {
      toolCallsGetterCount += 1;
      throw new TypeError(
        "Cannot read properties of undefined (reading 'type')",
      );
    },
  };
  const projectedMessages = [
    { id: "safe-ai", type: "ai", content: "complete" } as Message,
  ];
  const projectedValues = { messages: projectedMessages };
  const projectedStop = () => "stopped";

  const projection = overlayThreadProjection(sdkThread, {
    messages: projectedMessages,
    values: projectedValues,
    stop: projectedStop,
  });

  expect(toolCallsGetterCount).toBe(0);
  expect(projection.messages).toBe(projectedMessages);
  expect(projection.messages).toHaveLength(1);
  expect(projection.values).toBe(projectedValues);
  expect(projection.stop).toBe(projectedStop);
  const toolCallsDescriptor = Object.getOwnPropertyDescriptor(
    projection,
    "toolCalls",
  );
  expect(toolCallsDescriptor).toBeDefined();
  expect("get" in toolCallsDescriptor!).toBe(true);
  expect("value" in toolCallsDescriptor!).toBe(false);
  expect(toolCallsGetterCount).toBe(0);
});
