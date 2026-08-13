import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  canRetryModelOutputLimit,
  RunFailureAlert,
} from "@/components/workspace/run-failure-alert";
import { I18nProvider } from "@/core/i18n/context";
import { MODEL_OUTPUT_LIMIT } from "@/core/private-work/api-client";

function render(
  failureCode: typeof MODEL_OUTPUT_LIMIT | null,
  locale: "zh-CN" | "en-US",
  retryDisabled = false,
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <RunFailureAlert
        failureCode={failureCode}
        retryDisabled={retryDisabled}
        onRetryWithoutThinking={() => true}
      />
    </I18nProvider>,
  );
}

describe("RunFailureAlert", () => {
  test("renders the dedicated Chinese output-limit recovery state", () => {
    const html = render(MODEL_OUTPUT_LIMIT, "zh-CN");

    expect(html).toContain(`data-run-failure-code="${MODEL_OUTPUT_LIMIT}"`);
    expect(html).toContain("模型达到单次输出上限，当前回复未完成。");
    expect(html).toContain("关闭深度思考后重试");
  });

  test("renders the dedicated English output-limit recovery state", () => {
    const html = render(MODEL_OUTPUT_LIMIT, "en-US");

    expect(html).toContain("Model output limit reached");
    expect(html).toContain(
      "The model reached its per-request output limit, so this response is incomplete.",
    );
    expect(html).toContain("Retry without deep thinking");
  });

  test("keeps unknown failures on the existing generic path", () => {
    const html = render(null, "en-US");

    expect(html).toContain('data-run-failure-code="generic"');
    expect(html).toContain("Run did not finish");
    expect(html).not.toContain("Retry without deep thinking");
  });

  test("disables retry without Run authority or while a Run is active", () => {
    expect(
      canRetryModelOutputLimit({
        canRun: false,
        isRunLoading: false,
        hasRegenerationTarget: true,
        retrySurfaceAvailable: true,
      }),
    ).toBe(false);
    expect(
      canRetryModelOutputLimit({
        canRun: true,
        isRunLoading: true,
        hasRegenerationTarget: true,
        retrySurfaceAvailable: true,
      }),
    ).toBe(false);

    const html = render(MODEL_OUTPUT_LIMIT, "zh-CN", true);
    expect(html).toMatch(/<button[^>]*disabled=""/u);
    expect(html).toContain("关闭深度思考后重试");
  });
});
