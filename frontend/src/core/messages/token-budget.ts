import type { Message } from "@langchain/langgraph-sdk";

export const TOKEN_BUDGET_STATUS_KEY = "token_budget_status";

export type TokenBudgetNotice = {
  reason: "total" | "input" | "output";
};

const LEGACY_BUDGET_CONTROL_SUFFIX =
  /(?:^|\n\n)\[TOKEN BUDGET EXCEEDED\] The (total|input|output) token usage \([\d,]+\) has exceeded the safety limit \([\d,]+\)\. Producing final answer with results collected so far\.$/u;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseStructuredNotice(value: unknown): TokenBudgetNotice | null {
  if (!isRecord(value)) {
    return null;
  }
  const keys = Object.keys(value);
  if (
    keys.length !== 3 ||
    !keys.includes("version") ||
    !keys.includes("status") ||
    !keys.includes("reason") ||
    value.version !== 1 ||
    value.status !== "exceeded" ||
    (value.reason !== "total" &&
      value.reason !== "input" &&
      value.reason !== "output")
  ) {
    return null;
  }
  return { reason: value.reason };
}

export function readTokenBudgetNotice(
  message: Message,
): TokenBudgetNotice | null {
  if (message.type !== "ai") {
    return null;
  }
  return parseStructuredNotice(
    message.response_metadata?.[TOKEN_BUDGET_STATUS_KEY],
  );
}

function stripLegacyControlSuffix(value: string) {
  const match = LEGACY_BUDGET_CONTROL_SUFFIX.exec(value);
  if (!match || match.index + match[0].length !== value.length) {
    return null;
  }
  const reason = match[1];
  if (reason !== "total" && reason !== "input" && reason !== "output") {
    return null;
  }
  return {
    content: value.slice(0, match.index).trimEnd(),
    notice: { reason } satisfies TokenBudgetNotice,
  };
}

function cleanLegacyContent(content: Message["content"]): {
  content: Message["content"];
  notice: TokenBudgetNotice;
} | null {
  if (typeof content === "string") {
    return stripLegacyControlSuffix(content);
  }
  if (!Array.isArray(content) || content.length === 0) {
    return null;
  }

  const lastIndex = content.length - 1;
  const last = content[lastIndex];
  const text =
    typeof last === "string"
      ? last
      : isRecord(last) && last.type === "text" && typeof last.text === "string"
        ? last.text
        : null;
  if (text === null) {
    return null;
  }
  const cleaned = stripLegacyControlSuffix(text);
  if (!cleaned) {
    return null;
  }

  // Older persisted LangChain payloads may contain a bare string in a mixed
  // content array even though the current SDK type only models complex blocks.
  const next: unknown[] = content.slice(0, lastIndex);
  if (cleaned.content) {
    next.push(
      typeof last === "string"
        ? cleaned.content
        : { ...last, text: cleaned.content },
    );
  }
  return {
    content: next as Message["content"],
    notice: cleaned.notice,
  };
}

export function projectTokenBudgetMessages(messages: Message[]): Message[] {
  return messages.map((message) => {
    if (message.type !== "ai") {
      return message;
    }
    const cleaned = cleanLegacyContent(message.content);
    if (!cleaned) {
      return message;
    }
    const notice = readTokenBudgetNotice(message) ?? cleaned.notice;
    return {
      ...message,
      content: cleaned.content,
      response_metadata: {
        ...message.response_metadata,
        [TOKEN_BUDGET_STATUS_KEY]: {
          version: 1,
          status: "exceeded",
          reason: notice.reason,
        },
      },
    } as Message;
  });
}
