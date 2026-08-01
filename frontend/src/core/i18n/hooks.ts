"use client";

import { useI18nContext } from "./context";
import { translations } from "./translations";

import { DEFAULT_LOCALE, type Locale } from "./index";

export function useI18n() {
  const { locale, setLocale } = useI18nContext();

  const t = translations[locale] ?? translations[DEFAULT_LOCALE];

  const changeLocale = (newLocale: Locale) => {
    setLocale(newLocale);
  };

  return {
    locale,
    t,
    changeLocale,
  };
}
