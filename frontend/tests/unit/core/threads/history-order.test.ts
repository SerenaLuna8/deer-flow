import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  buildVisibleHistoryMessages,
  mergeMessages,
  mergeRunMessageRows,
  resolveFailedRunComposerInput,
  retainOptimisticHumanMessagesAfterFailure,
  retainUnacknowledgedOptimisticHumanMessages,
} from "@/core/threads/hooks";
import {
  captureTerminalRunMessages,
  projectThreadMessages,
  pruneConfirmedArchivedMessages,
} from "@/core/threads/message-projection";
import type { RunMessage } from "@/core/threads/types";

test("keeps the newer reconciled payload when an older duplicate arrives on a later page", () => {
  const row = (seq: string, reconciled: boolean): RunMessage => ({
    run_id: "run-reconciled",
    seq,
    content: {
      type: "ai",
      id: "assistant-reconciled",
      content: "Recovered answer",
      additional_kwargs: reconciled
        ? {
            deerflow_recovered_llm_failures: {
              schema_version: 1,
              failures: [
                {
                  attempt: 1,
                  max_attempts: 3,
                  error_code: "LLM_PROVIDER_UNAVAILABLE",
                  reason: "transient",
                  disposition: "recovered",
                },
              ],
            },
          }
        : {},
    } as Message,
    metadata: { caller: "lead_agent" },
    created_at: "2026-08-22T00:00:00Z",
  });

  const merged = mergeRunMessageRows(
    [row("100", true)],
    [row("49", false)],
    [],
  );

  expect(merged).toHaveLength(1);
  expect(
    merged[0]?.content.additional_kwargs?.deerflow_recovered_llm_failures,
  ).toBeDefined();
  expect(merged[0]?.seq).toBe("100");
});

test("keeps a sanitized run input before its output after a late dynamic-context injection", () => {
  const runId = "run-1";
  const originalUserContent = "deep-research 研究Agent发展史";
  const historyRows: RunMessage[] = [
    {
      run_id: runId,
      seq: "100",
      content: {
        type: "human",
        id: "human-1",
        content: [
          {
            type: "text",
            text: `--- BEGIN USER INPUT ---\n${originalUserContent}\n--- END USER INPUT ---`,
          },
        ],
        additional_kwargs: {
          run_id: runId,
          original_user_content: originalUserContent,
        },
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-08-06T03:04:39Z",
    },
    {
      run_id: runId,
      seq: "101",
      content: {
        type: "ai",
        id: "assistant-1",
        content: "已完成第一批检索。",
        additional_kwargs: {},
      } as Message,
      metadata: { caller: "lead_agent" },
      created_at: "2026-08-06T03:04:40Z",
    },
  ];
  const checkpointMessages = [
    {
      type: "system",
      id: "human-1",
      content: "<system-reminder />",
      additional_kwargs: {
        hide_from_ui: true,
        dynamic_context_reminder: true,
      },
    },
    historyRows[1]!.content,
    {
      type: "human",
      id: "human-1__user",
      content: [{ type: "text", text: originalUserContent }],
      additional_kwargs: { run_id: runId },
    },
  ] as Message[];

  const merged = mergeMessages(
    buildVisibleHistoryMessages(historyRows, new Set(), []),
    checkpointMessages,
    [],
  );
  const visibleHumans = merged.filter(
    (message) =>
      message.type === "human" &&
      message.additional_kwargs?.hide_from_ui !== true,
  );

  expect(visibleHumans).toHaveLength(1);
  expect(visibleHumans[0]?.id).toBe("human-1__user");
  expect(visibleHumans[0]?.content).toEqual([
    { type: "text", text: originalUserContent },
  ]);
  expect(merged.indexOf(visibleHumans[0]!)).toBeLessThan(
    merged.findIndex((message) => message.id === "assistant-1"),
  );
});

test("repairs a late injected user before run history has loaded", () => {
  const merged = mergeMessages(
    [],
    [
      {
        type: "system",
        id: "human-2",
        content: "<system-reminder />",
        additional_kwargs: {
          hide_from_ui: true,
          dynamic_context_reminder: true,
        },
      },
      {
        type: "ai",
        id: "assistant-2",
        content: "先出现的执行输出",
        additional_kwargs: {},
      },
      {
        type: "human",
        id: "human-2__user",
        content: "实际用户输入",
        additional_kwargs: { run_id: "run-2" },
      },
    ] as Message[],
    [],
  );

  expect(merged.findIndex((message) => message.id === "human-2__user")).toBe(1);
  expect(merged.findIndex((message) => message.id === "assistant-2")).toBe(2);
});

test("replaces an optimistic user with its dynamic-context canonical projection", () => {
  const merged = mergeMessages(
    [],
    [
      {
        type: "system",
        id: "human-optimistic-1",
        content: "<system-reminder />",
        additional_kwargs: {
          hide_from_ui: true,
          dynamic_context_reminder: true,
        },
      },
      {
        type: "human",
        id: "human-optimistic-1__user",
        content: "hello",
        additional_kwargs: { run_id: "run-optimistic-1" },
      },
    ] as Message[],
    [
      {
        type: "human",
        id: "human-optimistic-1",
        content: "hello",
        additional_kwargs: {},
      } as Message,
    ],
  );
  const visibleHumans = merged.filter(
    (message) =>
      message.type === "human" &&
      message.additional_kwargs?.hide_from_ui !== true,
  );

  expect(visibleHumans).toHaveLength(1);
  expect(visibleHumans[0]?.id).toBe("human-optimistic-1__user");
});

test("keeps a genuinely different optimistic user with the same text", () => {
  const merged = mergeMessages(
    [],
    [
      {
        type: "human",
        id: "human-canonical-1",
        content: "hello",
        additional_kwargs: { run_id: "run-canonical-1" },
      } as Message,
    ],
    [
      {
        type: "human",
        id: "human-optimistic-2",
        content: "hello",
        additional_kwargs: {},
      } as Message,
    ],
  );
  const visibleHumans = merged.filter(
    (message) =>
      message.type === "human" &&
      message.additional_kwargs?.hide_from_ui !== true,
  );

  expect(visibleHumans.map((message) => message.id)).toEqual([
    "human-canonical-1",
    "human-optimistic-2",
  ]);
});

test("retains only the submitted optimistic user when its Run fails", () => {
  const optimistic = [
    {
      type: "human",
      id: "human-failed",
      content: "原始输入",
    },
    {
      type: "ai",
      id: "ai-partial",
      content: "partial",
    },
    {
      type: "tool",
      id: "tool-partial",
      content: "partial tool result",
      tool_call_id: "call-1",
    },
  ] as Message[];

  expect(
    retainOptimisticHumanMessagesAfterFailure(optimistic, "run-failed"),
  ).toEqual([{ ...optimistic[0], run_id: "run-failed" }]);
});

test("captures only stable visible messages from the terminal Run", () => {
  const previous = {
    type: "ai",
    id: "ai-previous",
    content: "previous answer",
    run_id: "run-previous",
  } as Message;
  const currentHuman = {
    type: "human",
    id: "human-current",
    content: "current question",
  } as Message;
  const currentAssistant = {
    type: "ai",
    id: "ai-current",
    content: "partial answer",
    additional_kwargs: {},
  } as Message;
  const hiddenControl = {
    type: "system",
    id: "control-current",
    content: "hidden",
    additional_kwargs: { hide_from_ui: true },
  } as Message;
  const identityLess = {
    type: "ai",
    content: "cannot be durably acknowledged",
  } as Message;

  expect(
    captureTerminalRunMessages(
      [previous, currentHuman, currentAssistant, hiddenControl, identityLess],
      "run-current",
      new Set(["message:ai-previous"]),
    ),
  ).toEqual([
    { ...currentHuman, run_id: "run-current" },
    { ...currentAssistant, run_id: "run-current" },
  ]);
});

test("keeps terminal live messages visible until canonical history absorbs them", () => {
  const terminalMessages = [
    {
      type: "human",
      id: "human-terminal",
      content: "question",
      run_id: "run-terminal",
    },
    {
      type: "ai",
      id: "ai-terminal",
      content: "partial execution details",
      run_id: "run-terminal",
    },
  ] as unknown as Message[];
  const input = {
    threadId: "thread-a",
    visibleHistory: [] as Message[],
    pendingArchivedMessages: terminalMessages,
    pendingArchiveThreadId: "thread-a",
    renderMessages: [] as Message[],
    activeRunId: null,
    runBaselineMessageIds: new Set<string>(),
    pendingSupersededRunIds: new Set<string>(),
    visibleOptimisticMessages: [] as Message[],
  };

  expect(projectThreadMessages(input).map((message) => message.id)).toEqual([
    "human-terminal",
    "ai-terminal",
  ]);

  const canonicalHistory = [
    { ...terminalMessages[0], content: "question (persisted)" },
    {
      ...terminalMessages[1],
      content: "partial execution details (persisted)",
    },
  ] as unknown as Message[];
  const remaining = pruneConfirmedArchivedMessages(
    terminalMessages,
    canonicalHistory,
  );
  expect(remaining).toEqual([]);
  expect(
    projectThreadMessages({
      ...input,
      visibleHistory: canonicalHistory,
      pendingArchivedMessages: remaining,
    }).map((message) => [message.id, message.content]),
  ).toEqual([
    ["human-terminal", "question (persisted)"],
    ["ai-terminal", "partial execution details (persisted)"],
  ]);
});

test("never overlays a terminal handoff onto another thread", () => {
  expect(
    projectThreadMessages({
      threadId: "thread-b",
      visibleHistory: [],
      pendingArchivedMessages: [
        {
          type: "ai",
          id: "ai-thread-a",
          content: "must stay in thread A",
          run_id: "run-a",
        } as Message,
      ],
      pendingArchiveThreadId: "thread-a",
      renderMessages: [],
      activeRunId: null,
      runBaselineMessageIds: new Set(),
      pendingSupersededRunIds: new Set(),
      visibleOptimisticMessages: [],
    }),
  ).toEqual([]);
});

test("removes only the failed optimistic user acknowledged by canonical history", () => {
  const failed = [
    {
      type: "human",
      id: "human-failed-1",
      content: "same text",
      additional_kwargs: { run_id: "run-failed-1" },
    },
    {
      type: "human",
      id: "human-failed-2",
      content: "same text",
      additional_kwargs: { run_id: "run-failed-2" },
    },
  ] as Message[];

  expect(
    retainUnacknowledgedOptimisticHumanMessages(failed, [
      {
        type: "human",
        id: "human-failed-1__user",
        content: "same text",
        additional_kwargs: { run_id: "run-failed-1" },
      } as Message,
    ]),
  ).toEqual([failed[1]]);

  const merged = mergeMessages(
    [],
    [
      {
        type: "human",
        id: "human-failed-1__user",
        content: "same text",
        additional_kwargs: { run_id: "run-failed-1" },
      } as Message,
    ],
    failed,
  );
  expect(
    merged
      .filter((message) => message.type === "human")
      .map((message) => message.id),
  ).toEqual(["human-failed-1__user", "human-failed-2"]);
});

test("restores text and ready opaque attachments from the exact failed Run", () => {
  const restored = resolveFailedRunComposerInput(
    [
      {
        type: "human",
        id: "human-old",
        content: "older input",
        additional_kwargs: { run_id: "run-old" },
      },
      {
        type: "human",
        id: "human-failed",
        content: [{ type: "text", text: "describe this" }],
        additional_kwargs: {
          run_id: "run-failed",
          files: [
            {
              file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
              filename: "clipboard.png",
              size: 445_553,
              path: "/mnt/user-data/uploads/clipboard.png",
              status: "uploaded",
            },
            {
              file_id: "not-a-uuid",
              filename: "forged.png",
              size: 12,
              status: "uploaded",
            },
          ],
        },
      },
    ] as Message[],
    "run-failed",
  );

  expect(restored).toEqual({
    runId: "run-failed",
    messageId: "human-failed",
    text: "describe this",
    files: [
      {
        file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
        filename: "clipboard.png",
        size: 445_553,
        path: "/mnt/user-data/uploads/clipboard.png",
        status: "uploaded",
      },
    ],
  });
  expect(
    resolveFailedRunComposerInput(
      [
        {
          type: "human",
          id: "human-old",
          content: "older input",
          additional_kwargs: { run_id: "run-old" },
        } as Message,
      ],
      "run-missing",
    ),
  ).toBeNull();
});

test("restores an attachment-only failed Run", () => {
  expect(
    resolveFailedRunComposerInput(
      [
        {
          type: "human",
          id: "human-image-only",
          content: [{ type: "text", text: "" }],
          additional_kwargs: {
            run_id: "run-image-only",
            files: [
              {
                file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af",
                filename: "clipboard.png",
                size: 445_553,
                status: "uploaded",
              },
            ],
          },
        } as Message,
      ],
      "run-image-only",
    ),
  ).toMatchObject({
    runId: "run-image-only",
    messageId: "human-image-only",
    text: "",
    files: [{ file_id: "8f31eef3-0662-42c5-809c-3bbbe2c663af" }],
  });
});

test("shows original user text when only the sanitized journal row remains", () => {
  const [message] = mergeMessages(
    buildVisibleHistoryMessages(
      [
        {
          run_id: "run-3",
          seq: "300",
          content: {
            type: "human",
            id: "human-3",
            content:
              "--- BEGIN USER INPUT ---\n原始用户输入\n--- END USER INPUT ---",
            additional_kwargs: {
              original_user_content: "原始用户输入",
            },
          } as Message,
          metadata: { caller: "lead_agent" },
          created_at: "2026-08-06T03:04:39Z",
        },
      ],
      new Set(),
      [],
    ),
    [],
    [],
  );

  expect(message?.content).toEqual([{ type: "text", text: "原始用户输入" }]);
});
