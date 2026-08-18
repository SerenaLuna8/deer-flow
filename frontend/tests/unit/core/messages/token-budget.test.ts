import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  projectTokenBudgetMessages,
  readTokenBudgetNotice,
  TOKEN_BUDGET_STATUS_KEY,
} from "@/core/messages/token-budget";
import { getMessageGroups } from "@/core/messages/utils";

const internalControlText =
  "[TOKEN BUDGET EXCEEDED] The total token usage (10,197) has exceeded the safety limit (5,000). Producing final answer with results collected so far.";

test("reads a structured token-budget notice without changing answer text", () => {
  const message = {
    type: "ai",
    content: "BUDGET_OK",
    response_metadata: {
      [TOKEN_BUDGET_STATUS_KEY]: {
        version: 1,
        status: "exceeded",
        reason: "total",
      },
    },
  } as unknown as Message;

  expect(projectTokenBudgetMessages([message])).toEqual([message]);
  expect(readTokenBudgetNotice(message)).toEqual({ reason: "total" });
});

test("rejects token-budget metadata with unrecognized fields", () => {
  const message = {
    type: "ai",
    content: "ordinary answer",
    response_metadata: {
      [TOKEN_BUDGET_STATUS_KEY]: {
        version: 1,
        status: "exceeded",
        reason: "total",
        internalUsage: 10_197,
      },
    },
  } as unknown as Message;

  expect(readTokenBudgetNotice(message)).toBeNull();
});

test("removes the exact legacy control suffix and derives a notice", () => {
  const [projected] = projectTokenBudgetMessages([
    {
      type: "ai",
      content: `BUDGET_OK\n\n${internalControlText}`,
      response_metadata: {},
    } as unknown as Message,
  ]);

  expect(projected?.content).toBe("BUDGET_OK");
  expect(readTokenBudgetNotice(projected!)).toEqual({ reason: "total" });
  expect(JSON.stringify(projected?.content)).not.toContain("10,197");
});

test("removes a legacy control-only text block from mixed content", () => {
  const [projected] = projectTokenBudgetMessages([
    {
      type: "ai",
      content: [
        { type: "text", text: "BUDGET_OK" },
        { type: "text", text: `\n\n${internalControlText}` },
      ],
      response_metadata: {},
    } as unknown as Message,
  ]);

  expect(projected?.content).toEqual([{ type: "text", text: "BUDGET_OK" }]);
  expect(readTokenBudgetNotice(projected!)).toEqual({ reason: "total" });
});

test("does not rewrite similar ordinary assistant prose", () => {
  const message = {
    type: "ai",
    content: `${internalControlText} This is a quoted example.`,
    response_metadata: {},
  } as unknown as Message;

  expect(projectTokenBudgetMessages([message])).toEqual([message]);
  expect(readTokenBudgetNotice(message)).toBeNull();
});

test("does not strip a quoted control sentence without its original delimiter", () => {
  const message = {
    type: "ai",
    content: `Quoted example: ${internalControlText}`,
    response_metadata: {},
  } as unknown as Message;

  expect(projectTokenBudgetMessages([message])).toEqual([message]);
  expect(readTokenBudgetNotice(message)).toBeNull();
});

test("keeps a marker-only hard stop as an assistant display group", () => {
  const [projected] = projectTokenBudgetMessages([
    {
      type: "ai",
      content: "",
      response_metadata: {
        [TOKEN_BUDGET_STATUS_KEY]: {
          version: 1,
          status: "exceeded",
          reason: "output",
        },
      },
    } as unknown as Message,
  ]);

  expect(getMessageGroups([projected!])).toMatchObject([{ type: "assistant" }]);
});
