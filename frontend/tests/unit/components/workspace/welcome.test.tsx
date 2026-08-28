import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

let searchParams = new URLSearchParams();

rs.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

import { Welcome } from "@/components/workspace/welcome";
import { I18nProvider } from "@/core/i18n/context";

describe("Welcome", () => {
  beforeEach(() => {
    searchParams = new URLSearchParams();
  });

  test("omits the product description from a new conversation", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <Welcome />
      </I18nProvider>,
    );

    expect(html).toContain("你好，欢迎回来！");
    expect(html).not.toContain("欢迎使用 🦌 Fluva");
  });

  test("keeps the dedicated Skill creation description", () => {
    searchParams = new URLSearchParams("mode=skill");

    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <Welcome />
      </I18nProvider>,
    );

    expect(html).toContain("创建你自己的 Agent SKill");
    expect(html).toContain("创建你的 Agent Skill 来释放 Fluva 的潜力");
  });
});
