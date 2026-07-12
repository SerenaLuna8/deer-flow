import { describe, expect, test } from "@rstest/core";

import { safeInternalNextPath } from "@/core/auth/next-path";

describe("safeInternalNextPath", () => {
  test.each([
    [null, "/workspace"],
    ["", "/workspace"],
    ["/invite", "/invite"],
    [
      "/projects/research-lab?tab=members#active",
      "/projects/research-lab?tab=members#active",
    ],
  ])("maps %j to %s", (input, expected) => {
    expect(safeInternalNextPath(input)).toBe(expected);
  });

  test.each([
    "//evil.example",
    "/\\evil.example",
    "/%5Cevil.example",
    "https://evil.example",
    "https:",
    "/javascript:alert(1)",
    "/line\nbreak",
    "/nul\u0000byte",
  ])("rejects an escaping next value %j", (input) => {
    expect(safeInternalNextPath(input)).toBe("/workspace");
  });
});
