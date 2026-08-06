import { expect, test } from "@rstest/core";

import { enUS } from "@/core/i18n/locales/en-US";
import { zhCN } from "@/core/i18n/locales/zh-CN";

test("labels vision as a model capability rather than claiming image input", () => {
  expect(
    zhCN.conversation.runExecutionProfile("gpt-5.6-luna", "Pro", true),
  ).toBe("实际执行：gpt-5.6-luna · Pro · 支持视觉");
  expect(
    enUS.conversation.runExecutionProfile("gpt-5.6-luna", "Pro", true),
  ).toBe("Effective run: gpt-5.6-luna · Pro · vision-capable");
});
