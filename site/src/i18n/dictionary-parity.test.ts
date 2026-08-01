import { fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { beforeEach, describe, expect, it } from "vitest";

import { en } from "./en";
import { I18nProvider, useI18n } from "./I18nProvider";
import { zhCN } from "./zh-CN";

function flattenKeys(value: object, prefix = ""): string[] {
  return Object.entries(value)
    .flatMap(([key, nestedValue]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      return typeof nestedValue === "object" && nestedValue !== null
        ? flattenKeys(nestedValue, path)
        : [path];
    })
    .sort();
}

function LocaleProbe() {
  const { locale, setLocale } = useI18n();

  return createElement(
    "div",
    null,
    createElement("output", { "aria-label": "active locale" }, locale),
    createElement(
      "button",
      { type: "button", onClick: () => setLocale("zh-CN") },
      "Switch locale",
    ),
  );
}

describe("locale dictionaries", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("keeps Simplified Chinese keys in parity with English", () => {
    expect(flattenKeys(zhCN)).toEqual(flattenKeys(en));
  });

  it("persists an explicit locale choice across provider remounts", () => {
    const firstMount = render(
      createElement(
        I18nProvider,
        { initialLocale: "en" },
        createElement(LocaleProbe),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Switch locale" }));

    expect(screen.getByLabelText("active locale")).toHaveTextContent("zh-CN");
    expect(document.documentElement).toHaveAttribute("lang", "zh-CN");

    firstMount.unmount();
    render(
      createElement(I18nProvider, null, createElement(LocaleProbe)),
    );

    expect(screen.getByLabelText("active locale")).toHaveTextContent("zh-CN");
  });

  it("keeps the creator mark language-independent", () => {
    expect(en.brand.creator).toBe("what912");
    expect(zhCN.brand.creator).toBe("what912");
    expect(zhCN.brand.creator).toBe(en.brand.creator);
  });
});
