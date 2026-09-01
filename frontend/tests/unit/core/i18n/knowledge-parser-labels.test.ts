import { expect, test } from "@rstest/core";

import { enUS, zhCN } from "@/core/i18n/locales";

test("built-in document parsing has product-owned labels", () => {
  expect(enUS.adminKnowledgeSettings.builtInParser).toBe("Built-in parser");
  expect(zhCN.adminKnowledgeSettings.builtInParser).toBe("内置解析器");
  expect(
    enUS.knowledge.documents.parserProfile("builtin", "builtin.csv", "1"),
  ).toBe("Built-in parser · builtin.csv · 1");
  expect(
    zhCN.knowledge.documents.parserProfile("builtin", "builtin.csv", "1"),
  ).toBe("内置解析器 · builtin.csv · 1");
});
