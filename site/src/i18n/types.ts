import type { en } from "./en";

export type Locale = "en" | "zh-CN";
export type Dictionary = typeof en;

export interface I18nValue {
  locale: Locale;
  t: Dictionary;
  setLocale(locale: Locale): void;
}
