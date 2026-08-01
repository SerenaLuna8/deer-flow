import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import { resolveRequestLocale } from "@/core/i18n/locale";

describe("locale negotiation", () => {
  test("uses a supported cookie before the browser language", () => {
    expect(resolveRequestLocale("en-US", "zh-CN,zh;q=0.9")).toBe("en-US");
    expect(resolveRequestLocale("zh-CN", "en-US,en;q=0.9")).toBe("zh-CN");
  });

  test("falls back from an absent or invalid cookie to Accept-Language", () => {
    expect(
      resolveRequestLocale(undefined, "zh-Hans-CN,zh;q=0.9,en;q=0.8"),
    ).toBe("zh-CN");
    expect(resolveRequestLocale("invalid", "zh;q=0.8,en-US;q=0.7")).toBe(
      "zh-CN",
    );
    expect(resolveRequestLocale(undefined, "zh;q=0,en-US;q=0.7")).toBe("en-US");
    expect(resolveRequestLocale(undefined, "fr-FR,fr;q=0.9")).toBe("en-US");
  });

  test("keeps locale side effects in the provider rather than every hook user", () => {
    const providerSource = readFileSync(
      resolve(process.cwd(), "src/core/i18n/context.tsx"),
      "utf8",
    );
    const hookSource = readFileSync(
      resolve(process.cwd(), "src/core/i18n/hooks.ts"),
      "utf8",
    );

    expect(providerSource).toContain("document.documentElement.lang");
    expect(providerSource).toContain("setLocaleInCookie");
    expect(hookSource).not.toContain("useEffect");
    expect(hookSource).not.toContain("setLocaleInCookie");
  });
});
