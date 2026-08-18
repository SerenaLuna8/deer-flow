import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  buildVisibleHistoryMessages,
  mergeMessages,
  resolveFailedRunComposerInput,
  retainOptimisticHumanMessagesAfterFailure,
  retainUnacknowledgedOptimisticHumanMessages,
} from "@/core/threads/hooks";
import type { RunMessage } from "@/core/threads/types";

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
