import { expect, test } from "@rstest/core";

import { buildPublicHeaderLinks } from "@/components/landing/header";

test("keeps the public header free of retired blog links", () => {
  expect(buildPublicHeaderLinks("zh", "文档")).toEqual([
    { href: "/zh/docs", label: "文档" },
  ]);
  expect(buildPublicHeaderLinks("en", "Docs")).toEqual([
    { href: "/en/docs", label: "Docs" },
  ]);
});
