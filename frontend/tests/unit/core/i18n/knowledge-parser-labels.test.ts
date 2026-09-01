import { expect, test } from "@rstest/core";

import { enUS, zhCN } from "@/core/i18n/locales";

test("admin knowledge parser has product-owned labels", () => {
  expect(enUS.adminKnowledgeSettings.builtInParser).toBe("Built-in parser");
  expect(zhCN.adminKnowledgeSettings.builtInParser).toBe("内置解析器");
});
