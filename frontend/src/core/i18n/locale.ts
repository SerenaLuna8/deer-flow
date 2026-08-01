export const SUPPORTED_LOCALES = ["en-US", "zh-CN"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en-US";

export function isLocale(value: string): value is Locale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

export function getLocaleByLang(lang: string): Locale {
  const normalizedLang = lang.toLowerCase();
  for (const locale of SUPPORTED_LOCALES) {
    if (locale.startsWith(normalizedLang)) {
      return locale;
    }
  }
  return DEFAULT_LOCALE;
}

export function getLangByLocale(locale: Locale): string {
  const parts = locale.split("-");
  if (parts.length > 0 && typeof parts[0] === "string") {
    return parts[0];
  }
  return locale;
}

export function normalizeLocale(locale: string | null | undefined): Locale {
  if (!locale) {
    return DEFAULT_LOCALE;
  }

  if (isLocale(locale)) {
    return locale;
  }

  if (locale.toLowerCase().startsWith("zh")) {
    return "zh-CN";
  }

  return DEFAULT_LOCALE;
}

function parseSupportedLocale(value: string | null | undefined): Locale | null {
  if (!value) {
    return null;
  }

  const normalized = value.trim().toLowerCase();
  if (normalized === "en" || normalized.startsWith("en-")) {
    return "en-US";
  }
  if (normalized === "zh" || normalized.startsWith("zh-")) {
    return "zh-CN";
  }
  return null;
}

/**
 * Resolve the application locale in request priority order:
 * an explicit supported cookie, the browser's weighted language list,
 * and finally the product default.
 */
export function resolveRequestLocale(
  cookieLocale: string | null | undefined,
  acceptLanguage: string | null | undefined,
): Locale {
  const cookieMatch = parseSupportedLocale(cookieLocale);
  if (cookieMatch) {
    return cookieMatch;
  }

  const accepted = (acceptLanguage ?? "")
    .split(",")
    .map((entry, index) => {
      const [language = "", ...parameters] = entry.trim().split(";");
      const qualityParameter = parameters.find((parameter) =>
        parameter.trim().startsWith("q="),
      );
      const quality = qualityParameter
        ? Number.parseFloat(qualityParameter.trim().slice(2))
        : 1;
      return {
        language,
        quality: Number.isFinite(quality) ? quality : 0,
        index,
      };
    })
    .sort(
      (left, right) => right.quality - left.quality || left.index - right.index,
    );

  for (const preference of accepted) {
    if (preference.quality <= 0) {
      continue;
    }
    const match = parseSupportedLocale(preference.language);
    if (match) {
      return match;
    }
  }

  return DEFAULT_LOCALE;
}

// Helper function to detect browser locale
export function detectLocale(): Locale {
  if (typeof window === "undefined") {
    return DEFAULT_LOCALE;
  }

  const browserLang =
    navigator.language ||
    (navigator as unknown as { userLanguage: string }).userLanguage;

  return normalizeLocale(browserLang);
}
