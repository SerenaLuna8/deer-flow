import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  canReplayRunFailure,
  canRetryModelOutputLimit,
  RunFailureAlert,
  shouldShowRunFailureAlert,
} from "@/components/workspace/run-failure-alert";
import { I18nProvider } from "@/core/i18n/context";
import {
  CONTEXT_CAPACITY_EXCEEDED,
  CONTEXT_PROVIDER_CALL_AMBIGUOUS,
  CURRENT_UPLOAD_UNAVAILABLE,
  GRAPH_RECURSION_LIMIT,
  LLM_AUTHENTICATION_FAILED,
  LLM_CIRCUIT_OPEN,
  LLM_PROVIDER_UNAVAILABLE,
  LLM_QUOTA_EXCEEDED,
  LOOP_FINALIZATION_FAILED,
  LOOP_SAFETY_LIMIT,
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  RUN_POLICY_STALE,
  SIDE_EFFECT_STATE_UNKNOWN,
  TOOL_CALL_CONTROL_STATE_INVALID,
  TOOL_EXECUTION_FAILED,
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

  test("warns that an unknown side effect must not be replayed or restored", () => {
    const chinese = render(SIDE_EFFECT_STATE_UNKNOWN, "zh-CN", false, true);
    const english = render(SIDE_EFFECT_STATE_UNKNOWN, "en-US", false, true);

    expect(chinese).toContain(
      `data-run-failure-code="${SIDE_EFFECT_STATE_UNKNOWN}"`,
    );
    expect(chinese).toContain("运行状态无法确认");
    expect(chinese).toContain("部分操作可能已经执行");
    expect(chinese).toContain("请勿直接重新发送");
    expect(chinese).not.toContain("检查所选模型、依赖资产和凭据");
    expect(chinese).not.toContain("恢复到输入框");
    expect(english).toContain("Run state could not be confirmed");
    expect(english).toContain("Some operations may already have completed");
    expect(english).toContain("do not resend this message directly");
    expect(english).not.toContain("Restore to composer");
    expect(english).not.toContain("Retry without deep thinking");
  });

  test("explains the graph step limit without offering replay or input restore", () => {
    const failureCode = GRAPH_RECURSION_LIMIT;
    const chinese = render(failureCode, "zh-CN", false, true);
    const english = render(failureCode, "en-US", false, true);

    expect(chinese).toContain('data-run-failure-code="GRAPH_RECURSION_LIMIT"');
    expect(chinese).toContain("已达到图执行步数上限");
    expect(chinese).toContain("Agent 已停止，已有回答或文件可能不完整");
    expect(chinese).toContain("请勿直接重新发送");
    expect(chinese).not.toContain("Worker 无法确认最终状态");
    expect(chinese).not.toContain("恢复到输入框");
    expect(english).toContain("Graph execution step limit reached");
    expect(english).toContain("The Agent has stopped");
    expect(english).toContain("Existing answers or files may be incomplete");
    expect(english).toContain("do not resend this message directly");
    expect(english).not.toContain("Restore to composer");
    expect(english).not.toContain("Retry without deep thinking");
    expect(canReplayRunFailure(failureCode)).toBe(false);
  });

  test("blocks message replay when durable side effects may already exist", () => {
    expect(canReplayRunFailure(SIDE_EFFECT_STATE_UNKNOWN)).toBe(false);
    expect(canReplayRunFailure(OUTPUT_DELIVERY_INCOMPLETE)).toBe(false);
    expect(canReplayRunFailure(CONTEXT_PROVIDER_CALL_AMBIGUOUS)).toBe(false);
    expect(canReplayRunFailure(MODEL_OUTPUT_LIMIT)).toBe(true);
    expect(canReplayRunFailure(null)).toBe(true);
  });

  test("renders explicit Context capacity and Provider ambiguity failures", () => {
    const capacity = render(CONTEXT_CAPACITY_EXCEEDED, "zh-CN", false, true);
    const ambiguous = render(
      CONTEXT_PROVIDER_CALL_AMBIGUOUS,
      "en-US",
      false,
      true,
    );

    expect(capacity).toContain("上下文超过模型容量");
    expect(capacity).toContain("自动压缩后仍无法容纳");
    expect(capacity).toContain("恢复到输入框");
    expect(ambiguous).toContain("Provider call outcome could not be confirmed");
    expect(ambiguous).toContain("must not be replayed automatically");
    expect(ambiguous).not.toContain("Restore to composer");
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

  test("renders a provider-specific terminal failure without a console-error surface", () => {
    const chinese = render(LLM_PROVIDER_UNAVAILABLE, "zh-CN", false, true);
    const english = render(LLM_PROVIDER_UNAVAILABLE, "en-US", false, true);

    expect(chinese).toContain(
      `data-run-failure-code="${LLM_PROVIDER_UNAVAILABLE}"`,
    );
    expect(chinese).toContain("模型服务暂时不可用");
    expect(chinese).toContain("检查 Worker 网络或代理配置");
    expect(english).toContain("Model provider temporarily unavailable");
    expect(english).toContain(
      "Check the Worker network or proxy configuration",
    );
  });

  test("renders the loop safety limit as a partial-result failure and keeps input restore", () => {
    const chinese = render(LOOP_SAFETY_LIMIT, "zh-CN", false, true);
    const english = render(LOOP_SAFETY_LIMIT, "en-US", false, true);

    expect(chinese).toContain(`data-run-failure-code="${LOOP_SAFETY_LIMIT}"`);
    expect(chinese).toContain("重复操作触发安全上限");
    expect(chinese).toContain("本次运行已停止");
    expect(chinese).toContain("已有回答或文件属于部分结果");
    expect(chinese).toContain("本次失败原因是循环安全限制");
    expect(chinese).toContain("恢复到输入框");
    expect(chinese).not.toContain("Agent 未能生成回复");
    expect(english).toContain("Repeated operations triggered the safety limit");
    expect(english).toContain("This Run has stopped");
    expect(english).toContain(
      "Any existing answer or files are partial results",
    );
    expect(english).toContain("the failure reason is the loop safety limit");
    expect(english).toContain("Restore to composer");
    expect(english).not.toContain("could not produce a response");
  });

  test("renders stable model, tool, policy, and control-state direct causes", () => {
    expect(render(LOOP_FINALIZATION_FAILED, "zh-CN")).toContain(
      "模型仍尝试调用工具或没有返回可见的无工具最终回答",
    );
    expect(render(LLM_QUOTA_EXCEEDED, "en-US")).toContain(
      "The configured model quota was exhausted",
    );
    expect(render(LLM_AUTHENTICATION_FAILED, "zh-CN")).toContain(
      "所选模型的认证被服务商拒绝",
    );
    expect(render(LLM_CIRCUIT_OPEN, "en-US")).toContain(
      "model request circuit breaker was open",
    );
    expect(render(TOOL_EXECUTION_FAILED, "zh-CN")).toContain(
      "工具执行返回了明确失败",
    );
    expect(render(RUN_POLICY_STALE, "en-US")).toContain(
      "frozen runtime policy could not be materialized",
    );
    expect(render(TOOL_CALL_CONTROL_STATE_INVALID, "zh-CN")).toContain(
      "工具调用控制状态与冻结策略或执行作用域不一致",
    );
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
