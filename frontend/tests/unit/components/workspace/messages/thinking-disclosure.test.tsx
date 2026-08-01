import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ThinkingDisclosure } from "@/components/workspace/messages/thinking-disclosure";
import { I18nContext } from "@/core/i18n/context";

test("renders the completed thinking duration in Chinese without the old English label", () => {
  const html = renderThinkingDisclosure("zh-CN");

  expect(html).toContain("已思考（用时 12 秒）");
  expect(html).not.toContain("Thinking");
  expect(html).not.toContain("Thought for");
  expect(html).not.toContain("思考了几秒");
  expect(html).toContain('data-testid="thinking-disclosure"');
});

test("renders the completed thinking duration in English", () => {
  const html = renderThinkingDisclosure("en-US");

  expect(html).toContain("Thought (12 seconds)");
});

test("matches the compact inline reference treatment", () => {
  const html = renderThinkingDisclosure("zh-CN");

  expect(html).toContain("text-[#3964fe]");
  expect(html).toContain("w-fit");
  expect(html).not.toContain("rounded-xl");
  expect(html).not.toContain("border-border/70");
});

test("renders a localized live thinking status", () => {
  const html = renderToStaticMarkup(
    createElement(
      I18nContext.Provider,
      {
        value: {
          locale: "zh-CN",
          setLocale: () => undefined,
        },
      },
      createElement(ThinkingDisclosure, {
        isStreaming: true,
        startTimeProp: Date.now() - 3000,
      }),
    ),
  );

  expect(html).toContain("思考中…（3 秒）");
  expect(html).not.toContain("Thinking");
});

test("renders a neutral legacy label without inventing a duration", () => {
  const html = renderToStaticMarkup(
    createElement(
      I18nContext.Provider,
      {
        value: {
          locale: "zh-CN",
          setLocale: () => undefined,
        },
      },
      createElement(
        ThinkingDisclosure,
        {
          defaultOpen: false,
          isStreaming: false,
        },
        createElement("p", null, "Reasoning details"),
      ),
    ),
  );

  expect(html).toContain("思考过程");
  expect(html).not.toContain("15 秒");
  expect(html).not.toContain("思考了几秒");
});

test("places completed thinking before the final answer", () => {
  const source = readFileSync(
    resolve(
      process.cwd(),
      "src/components/workspace/messages/message-list-item.tsx",
    ),
    "utf8",
  );
  const completedAssistantStart = source.lastIndexOf(
    "<AIElementMessageContent className={className}>",
  );
  const completedAssistantSource = source.slice(completedAssistantStart);
  const thinkingIndex = completedAssistantSource.indexOf(
    "{showReasoning && reasoningContent && (",
  );
  const answerIndex =
    completedAssistantSource.indexOf("<MarkdownContent");

  expect(thinkingIndex).toBeGreaterThan(-1);
  expect(answerIndex).toBeGreaterThan(thinkingIndex);
});

function renderThinkingDisclosure(locale: "en-US" | "zh-CN") {
  return renderToStaticMarkup(
    createElement(
      I18nContext.Provider,
      {
        value: {
          locale,
          setLocale: () => undefined,
        },
      },
      createElement(
        ThinkingDisclosure,
        {
          duration: 12,
          defaultOpen: false,
          isStreaming: false,
        },
        createElement("p", null, "Reasoning details"),
      ),
    ),
  );
}
