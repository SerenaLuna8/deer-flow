import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  canRetryModelOutputLimit,
  RunFailureAlert,
  shouldShowRunFailureAlert,
} from "@/components/workspace/run-failure-alert";
import { I18nProvider } from "@/core/i18n/context";
import {
  CURRENT_UPLOAD_UNAVAILABLE,
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  type ProjectRunFailureCode,
} from "@/core/private-work/api-client";

function render(
  failureCode: ProjectRunFailureCode | null,
  locale: "zh-CN" | "en-US",
  retryDisabled = false,
  restoreAvailable = false,
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <RunFailureAlert
        failureCode={failureCode}
        retryDisabled={retryDisabled}
        onRetryWithoutThinking={() => true}
        onRestoreInput={restoreAvailable ? () => undefined : undefined}
      />
    </I18nProvider>,
  );
}

describe("RunFailureAlert", () => {
  test("does not classify a pre-admission stream error as a terminal Run failure", () => {
    expect(
      shouldShowRunFailureAlert({
        hasTerminalRunFailure: false,
        streamError: new Error("Run admission conflict"),
      }),
    ).toBe(false);
    expect(
      shouldShowRunFailureAlert({
        hasTerminalRunFailure: true,
        streamError: undefined,
      }),
    ).toBe(true);
  });

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

  test("offers a non-submitting input restore action for recoverable generic failures", () => {
    const chinese = render(null, "zh-CN", false, true);
    const english = render(null, "en-US", false, true);

    expect(chinese).toContain("恢复到输入框");
    expect(english).toContain("Restore to composer");
    expect(chinese).not.toContain("关闭深度思考后重试");
  });

  test("does not offer input restore when no failed user input is available", () => {
    expect(render(null, "zh-CN")).not.toContain("恢复到输入框");
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

  test("renders a diagnosable current-upload failure with input recovery", () => {
    const chinese = render(CURRENT_UPLOAD_UNAVAILABLE, "zh-CN", false, true);
    const english = render(CURRENT_UPLOAD_UNAVAILABLE, "en-US", false, true);

    expect(chinese).toContain(
      `data-run-failure-code="${CURRENT_UPLOAD_UNAVAILABLE}"`,
    );
    expect(chinese).toContain("当前图片附件不可用");
    expect(chinese).toContain("恢复原输入并重试");
    expect(chinese).toContain("恢复到输入框");
    expect(english).toContain("Image attachment could not be read");
    expect(english).toContain("Restore the original input and retry");
    expect(english).toContain("Restore to composer");
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
