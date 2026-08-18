import { expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentArchivedAlert } from "@/components/workspace/agent-archived-alert";
import { I18nProvider } from "@/core/i18n/context";

test("explains that the Agent was deleted and starts a new chat switch flow", () => {
  const html = renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <AgentArchivedAlert onStartNewChat={() => undefined} />
    </I18nProvider>,
  );

  expect(html).toContain('data-testid="agent-archived-alert"');
  expect(html).toContain("Agent 已删除");
  expect(html).toContain(
    "该 Agent 已删除，当前对话不能继续。请选择其他 Agent 新建对话。",
  );
  expect(html).toContain("选择其他 Agent 新建对话");
  expect(html).not.toContain("Agent 模型不可用");
  expect(html).not.toContain("重试");
});
