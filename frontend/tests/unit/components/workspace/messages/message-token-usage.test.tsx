import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { MessageTokenUsageList } from "@/components/workspace/messages/message-token-usage";
import { I18nContext } from "@/core/i18n/context";

const messages = [
  {
    id: "ai-1",
    type: "ai",
    content: "Answer",
    usage_metadata: {
      input_tokens: 10,
      output_tokens: 5,
      total_tokens: 15,
    },
  },
] as Message[];

test("does not show token usage while the assistant turn is loading", () => {
  expect(renderUsage(true)).toBe("");
});

test("shows token usage after the assistant turn completes", () => {
  const html = renderUsage(false);

  expect(html).toContain("Tokens");
  expect(html).toContain("15");
});

function renderUsage(isLoading: boolean) {
  return renderToStaticMarkup(
    createElement(
      I18nContext.Provider,
      {
        value: {
          locale: "en-US",
          setLocale: () => undefined,
        },
      },
      createElement(MessageTokenUsageList, {
        enabled: true,
        isLoading,
        messages,
      }),
    ),
  );
}
