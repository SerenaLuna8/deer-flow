import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  canRetryModelOutputLimit,
  RunFailureAlert,
} from "@/components/workspace/run-failure-alert";
import { I18nProvider } from "@/core/i18n/context";
import {
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  type ProjectRunFailureCode,
} from "@/core/private-work/api-client";

function render(
  failureCode: ProjectRunFailureCode | null,
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

  test("renders the dedicated Chinese output-delivery warning without a replay action", () => {
    const html = render(OUTPUT_DELIVERY_INCOMPLETE, "zh-CN");

    expect(html).toContain(
      `data-run-failure-code="${OUTPUT_DELIVERY_INCOMPLETE}"`,
    );
    expect(html).toContain("结果文件未完成交付");
    expect(html).toContain("重新发送可能重复执行已经完成的命令");
    expect(html).not.toContain("检查所选模型、依赖资产和凭据");
    expect(html).not.toContain("关闭深度思考后重试");
  });

  test("renders the dedicated English output-delivery warning", () => {
    const html = render(OUTPUT_DELIVERY_INCOMPLETE, "en-US");

    expect(html).toContain("Output file was not delivered");
    expect(html).toContain("Resending may repeat an already completed command");
    expect(html).not.toContain("Check the selected model");
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
