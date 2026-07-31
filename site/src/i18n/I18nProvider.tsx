import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { en } from "./en";
import type { Dictionary, I18nValue, Locale } from "./types";
import { zhCN } from "./zh-CN";

const LOCALE_STORAGE_KEY = "videoscope.locale";

const dictionaries: Record<Locale, Dictionary> = {
  en,
  "zh-CN": zhCN,
};

const I18nContext = createContext<I18nValue | undefined>(undefined);

function isLocale(value: unknown): value is Locale {
  return value === "en" || value === "zh-CN";
}

function readSavedLocale(): Locale | undefined {
  try {
    const savedLocale = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return isLocale(savedLocale) ? savedLocale : undefined;
  } catch {
    return undefined;
  }
}

function browserLocale(): Locale {
  const browserLanguage =
    navigator.languages.find((language) => language.length > 0) ??
    navigator.language;
  return browserLanguage.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

export function resolveInitialLocale(explicitLocale?: Locale): Locale {
  return explicitLocale ?? readSavedLocale() ?? browserLocale() ?? "en";
}

type I18nProviderProps = PropsWithChildren<{
  initialLocale?: Locale;
}>;

export function I18nProvider({
  children,
  initialLocale,
}: I18nProviderProps) {
  const [locale, updateLocale] = useState<Locale>(() =>
    resolveInitialLocale(initialLocale),
  );

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((nextLocale: Locale) => {
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, nextLocale);
    } catch {
      // A locale choice still applies for this session if storage is blocked.
    }
    updateLocale(nextLocale);
  }, []);

  const value = useMemo<I18nValue>(
    () => ({
      locale,
      setLocale,
      t: dictionaries[locale],
    }),
    [locale, setLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) {
    throw new Error("useI18n must be used within I18nProvider");
  }
  return value;
}
