import type { PageMapItem } from "nextra";

import type { Locale } from "@/core/i18n/locale";

type DocsContentLanguage = "en" | "zh";

export type DocsLanguage = {
  contentLang: DocsContentLanguage;
  locale: Locale;
  localeOptions: Array<{ locale: string; name: string }>;
};

const DOCS_ROOT_ROUTES = new Set([
  "/",
  "/application",
  "/harness",
  "/introduction",
  "/reference",
  "/tutorials",
]);

export function resolveDocsLanguage(lang: string): DocsLanguage | null {
  const normalized = lang.trim().toLowerCase();
  const usesApplicationLocale = normalized.includes("-");

  if (normalized === "en" || normalized === "en-us") {
    return {
      contentLang: "en",
      locale: "en-US",
      localeOptions: usesApplicationLocale
        ? [
            { locale: "en-US", name: "English" },
            { locale: "zh-CN", name: "中文" },
          ]
        : [
            { locale: "en", name: "English" },
            { locale: "zh", name: "中文" },
          ],
    };
  }

  if (normalized === "zh" || normalized === "zh-cn") {
    return {
      contentLang: "zh",
      locale: "zh-CN",
      localeOptions: usesApplicationLocale
        ? [
            { locale: "en-US", name: "English" },
            { locale: "zh-CN", name: "中文" },
          ]
        : [
            { locale: "en", name: "English" },
            { locale: "zh", name: "中文" },
          ],
    };
  }

  return null;
}

function prefixPageRoute(base: string, item: PageMapItem): PageMapItem {
  if (!("route" in item)) {
    return { ...item };
  }

  const route =
    item.route === "/"
      ? base
      : item.route.startsWith(base)
        ? item.route
        : `${base}${item.route}`;

  if (!("children" in item)) {
    return { ...item, route };
  }

  return {
    ...item,
    route,
    children: item.children.map((child) => prefixPageRoute(base, child)),
  };
}

export function buildDocsPageMap(
  base: string,
  items: PageMapItem[],
): PageMapItem[] {
  return items
    .filter(
      (item): item is PageMapItem & { route: string } =>
        "route" in item && DOCS_ROOT_ROUTES.has(item.route),
    )
    .map((item) => prefixPageRoute(base, item));
}
