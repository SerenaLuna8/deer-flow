import { describe, expect, test } from "@rstest/core";

import { localizeDocsHref } from "@/components/docs/localized-links";

describe("localized docs links", () => {
  test("localizes absolute and root-relative docs links for every supported route locale", () => {
    expect(localizeDocsHref("/docs/introduction", "en")).toBe(
      "/en/docs/introduction",
    );
    expect(localizeDocsHref("./docs/introduction", "zh")).toBe(
      "/zh/docs/introduction",
    );
    expect(localizeDocsHref("/docs/introduction", "en-US")).toBe(
      "/en-US/docs/introduction",
    );
    expect(localizeDocsHref("./docs/introduction", "zh-CN")).toBe(
      "/zh-CN/docs/introduction",
    );
  });

  test("leaves anchors and external links unchanged", () => {
    expect(localizeDocsHref("#start", "en")).toBe("#start");
    expect(localizeDocsHref("https://example.com/docs", "zh-CN")).toBe(
      "https://example.com/docs",
    );
  });
});
