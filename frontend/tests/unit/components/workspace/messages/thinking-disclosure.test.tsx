import { expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ThinkingDisclosure } from "@/components/workspace/messages/thinking-disclosure";
import { I18nContext } from "@/core/i18n/context";

test("renders the completed thinking duration in Chinese without the old English label", () => {
  const html = renderThinkingDisclosure("zh-CN");

  expect(html).toContain("思考了 12 秒");
  expect(html).not.toContain("Thinking");
  expect(html).not.toContain("Thought for");
  expect(html).toContain('data-testid="thinking-disclosure"');
});

test("renders the completed thinking duration in English", () => {
  const html = renderThinkingDisclosure("en-US");

  expect(html).toContain("Thought for 12 seconds");
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
