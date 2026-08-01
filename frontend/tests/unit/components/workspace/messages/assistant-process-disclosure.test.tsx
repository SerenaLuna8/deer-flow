import { expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AssistantProcessDisclosure } from "@/components/workspace/messages/assistant-process-disclosure";
import { I18nContext } from "@/core/i18n/context";

test("renders a compact completed execution disclosure in Chinese", () => {
  const html = renderDisclosure("zh-CN", 8);

  expect(html).toContain('data-testid="assistant-process-disclosure"');
  expect(html).toContain("执行过程");
  expect(html).toContain("8 个步骤");
  expect(html).toContain('data-state="closed"');
  const controlledContentId = /aria-controls="([^"]+)"/.exec(html)?.[1];
  expect(controlledContentId).toBeTruthy();
  expect(html).toContain(`id="${controlledContentId}"`);
});

test("renders the execution step count in English", () => {
  const html = renderDisclosure("en-US", 2);

  expect(html).toContain("Execution details");
  expect(html).toContain("2 steps");
});

function renderDisclosure(locale: "en-US" | "zh-CN", stepCount: number) {
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
        AssistantProcessDisclosure,
        { stepCount },
        createElement("p", null, "Earlier reasoning"),
      ),
    ),
  );
}
