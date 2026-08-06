import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  buildVisibleHistoryMessages,
  mergeMessages,
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
