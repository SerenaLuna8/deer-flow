import { describe, expect, test } from "@rstest/core";

import { buildDocsPageMap, resolveDocsLanguage } from "@/core/docs/routing";

describe("docs routing", () => {
  test("maps application locale tags to Nextra content locales", () => {
    expect(resolveDocsLanguage("en")).toMatchObject({
      contentLang: "en",
      locale: "en-US",
    });
    expect(resolveDocsLanguage("en-US")).toMatchObject({
      contentLang: "en",
      locale: "en-US",
    });
    expect(resolveDocsLanguage("zh")).toMatchObject({
      contentLang: "zh",
      locale: "zh-CN",
    });
    expect(resolveDocsLanguage("zh-CN")).toMatchObject({
      contentLang: "zh",
      locale: "zh-CN",
    });
    expect(resolveDocsLanguage("fr")).toBeNull();
  });

  test("keeps only documentation roots and never exposes dynamic app routes", () => {
    const source = [
      {
        name: "introduction",
        route: "/introduction",
        children: [
          { name: "index", route: "/introduction" },
          { name: "concepts", route: "/introduction/core-concepts" },
        ],
      },
      {
        name: "admin",
        route: "/admin",
        children: [
          {
            name: "projects",
            route: "/admin/projects/[project_id]/assets",
          },
        ],
      },
    ];

    expect(buildDocsPageMap("/en/docs", source)).toEqual([
      {
        name: "introduction",
        route: "/en/docs/introduction",
        children: [
          { name: "index", route: "/en/docs/introduction" },
          {
            name: "concepts",
            route: "/en/docs/introduction/core-concepts",
          },
        ],
      },
    ]);
    expect(source[0]?.route).toBe("/introduction");
  });
});
