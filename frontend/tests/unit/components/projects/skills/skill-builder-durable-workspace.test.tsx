import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillBuilderRunActivity } from "@/components/projects/skills/skill-builder-run-activity";
import { I18nProvider } from "@/core/i18n/context";

function renderUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

const RUN_ID = "44444444-4444-4444-8444-444444444444";
const NEXT_RUN_ID = "55555555-5555-4555-8555-555555555555";

describe("SkillBuilderRunActivity", () => {
  test("shows an explicit empty state before tool events are available", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        activeRun={{
          runId: RUN_ID,
          status: "running",
          streamUrl: "/api/stream",
        }}
      />,
    );

    expect(html).toContain("正在执行");
    expect(html).toContain("尚无工具步骤");
    expect(html).not.toContain("当前后端仅提供了可靠的 Run 状态");
  });

  test("renders only projected tool names and statuses", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        projection={{
          runId: RUN_ID,
          status: "running",
          messages: [],
          toolSteps: [
            {
              id: "step-1",
              toolName: "project_asset_catalog",
              status: "completed",
            },
          ],
          clarification: null,
        }}
      />,
    );

    expect(html).toContain("project_asset_catalog");
    expect(html).toContain("已完成");
  });

  test("keeps the terminal outcome visible after activeRun is removed", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        presentation={{ runId: RUN_ID, status: "cancelled" }}
      />,
    );

    expect(html).toContain("本轮执行已取消");
  });

  test("prefers the durable terminal outcome while retaining replayed tool steps", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        projection={{
          runId: RUN_ID,
          status: "running",
          messages: [],
          toolSteps: [
            { id: "call-read", toolName: "read_file", status: "running" },
          ],
          clarification: null,
        }}
        presentation={{ runId: RUN_ID, status: "success" }}
      />,
    );

    expect(html).toContain("本轮已完成");
    expect(html).not.toContain("正在执行");
    expect(html).toContain("read_file");
    expect(html).toContain("已完成");
    expect(html).not.toContain("调用中");
  });

  test("does not let an old terminal presentation cover a newly active Run", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        activeRun={{
          runId: NEXT_RUN_ID,
          status: "pending",
          streamUrl: "/api/next-run/stream",
        }}
        presentation={{ runId: RUN_ID, status: "success" }}
      />,
    );

    expect(html).toContain("等待执行");
    expect(html).not.toContain("本轮已完成");
  });

  test("settles unfinished tool steps as failed for a non-success terminal outcome", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        projection={{
          runId: RUN_ID,
          status: "running",
          messages: [],
          toolSteps: [{ id: "call-bash", toolName: "bash", status: "pending" }],
          clarification: null,
        }}
        presentation={{ runId: RUN_ID, status: "error" }}
      />,
    );

    expect(html).toContain("本轮执行失败");
    expect(html).toContain("调用失败");
    expect(html).not.toContain("等待调用");
  });

  test("explains how to resume after the model output limit", () => {
    const html = renderUi(
      <SkillBuilderRunActivity
        presentation={{ runId: RUN_ID, status: "error" }}
        failureCode="MODEL_OUTPUT_LIMIT"
      />,
    );

    expect(html).toContain("本轮达到模型输出上限");
    expect(html).toContain("已成功写入的候选文件仍然保留");
    expect(html).toContain("基于现有候选文件继续完成");
  });
});
