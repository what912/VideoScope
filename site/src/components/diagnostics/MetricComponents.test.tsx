import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { I18nProvider } from "../../i18n/I18nProvider";
import type { QualityMetric } from "../../types/analysis";
import { MetricBar } from "./MetricBar";
import { MetricChart } from "./MetricChart";

function metric(
  overrides: Partial<QualityMetric> = {},
): QualityMetric {
  return {
    id: "metric",
    label: "Observed events",
    value: 250,
    kind: "browser_cpu",
    detector_id: "near_black",
    unit: "count",
    description: "A detector-local raw count.",
    ...overrides,
  } as QualityMetric;
}

function renderLocalized(
  ui: React.ReactNode,
  locale: "en" | "zh-CN" = "en",
) {
  return render(
    <I18nProvider initialLocale={locale}>{ui}</I18nProvider>,
  );
}

describe("MetricBar", () => {
  it("renders ratio metrics against the truthful fixed 0–1 domain", () => {
    renderLocalized(<MetricBar metric={metric({ unit: "ratio", value: 0.75 })} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuemax", "1");
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0.75");
    expect(screen.getByTestId("metric-bar-fill")).toHaveStyle({ width: "75%" });
  });

  it.each([
    ["count", 250],
    ["seconds", -4.5],
  ] as const)(
    "renders %s without inventing a percentage when no domain is supplied",
    (unit, value) => {
      renderLocalized(<MetricBar metric={metric({ unit, value })} />);
      const localizedUnit = unit === "count" ? "events" : "seconds";
      expect(
        screen.getByText(new RegExp(`^${value} ${localizedUnit}$`)),
      ).toBeVisible();
      if (localizedUnit !== unit) {
        expect(
          screen.queryByText(new RegExp(` ${unit}$`)),
        ).not.toBeInTheDocument();
      }
      expect(screen.queryByRole("meter")).not.toBeInTheDocument();
    },
  );

  it("uses an explicit finite scalar domain without silently clamping its meaning", () => {
    renderLocalized(
      <MetricBar
        metric={metric({
          unit: "seconds",
          value: 150,
          domain: { min: 100, max: 200 },
        })}
      />,
    );
    const meter = screen.getByRole("meter");
    expect(meter).toHaveAttribute("aria-valuemin", "100");
    expect(meter).toHaveAttribute("aria-valuemax", "200");
    expect(meter).toHaveAttribute("aria-valuenow", "150");
    expect(screen.getByTestId("metric-bar-fill")).toHaveStyle({ width: "50%" });
  });

  it("renders raw values for zero-width or non-finite domains", () => {
    renderLocalized(
      <MetricBar
        metric={metric({ domain: { min: 2, max: 2 }, value: 2 })}
      />,
    );
    expect(screen.queryByRole("meter")).not.toBeInTheDocument();
  });

  it("uses centralized Simplified Chinese unit labels", () => {
    renderLocalized(
      <MetricBar metric={metric({ unit: "count", value: 3 })} />,
      "zh-CN",
    );
    expect(screen.getByText("3 次")).toBeVisible();
    expect(screen.queryByText(/^3 count$/)).not.toBeInTheDocument();
  });
});

describe("MetricChart", () => {
  it("derives a finite data domain for count samples including negatives and large values", () => {
    renderLocalized(
      <MetricChart
        currentTime={1}
        duration={2}
        metric={metric()}
        samples={[
          { time: 0, value: -50 },
          { time: 1, value: 250 },
          { time: 2, value: 950 },
        ]}
      />,
    );
    expect(screen.getByTestId("metric-chart-domain")).toHaveTextContent(
      "-50–950",
    );
    expect(screen.getByTestId("metric-chart-line")).toHaveAttribute(
      "points",
      "0,100 50,70 100,0",
    );
  });

  it("renders a constant seconds series on the midpoint of a truthful zero-width data domain", () => {
    renderLocalized(
      <MetricChart
        currentTime={1}
        duration={2}
        metric={metric({ unit: "seconds", value: 5 })}
        samples={[
          { time: 0, value: 5 },
          { time: 2, value: 5 },
        ]}
      />,
    );
    expect(screen.getByTestId("metric-chart-domain")).toHaveTextContent("5");
    expect(screen.getByTestId("metric-chart-line")).toHaveAttribute(
      "points",
      "0,50 100,50",
    );
  });

  it("filters non-finite sample times before creating SVG points", () => {
    renderLocalized(
      <MetricChart
        currentTime={1}
        duration={2}
        metric={metric({ unit: "seconds", value: 5 })}
        samples={[
          { time: Number.NaN, value: 1 },
          { time: Number.POSITIVE_INFINITY, value: 2 },
          { time: 1, value: 3 },
        ]}
      />,
    );
    const line = screen.getByTestId("metric-chart-line");
    expect(line).toHaveAttribute("points", "50,50");
    expect(line.getAttribute("points")).not.toMatch(/NaN|Infinity/);
  });

  it("localizes chart domains without exposing raw unit enums", () => {
    renderLocalized(
      <MetricChart
        currentTime={1}
        duration={2}
        metric={metric({ unit: "seconds", value: 5 })}
        samples={[
          { time: 0, value: 1 },
          { time: 2, value: 5 },
        ]}
      />,
      "zh-CN",
    );
    expect(screen.getByTestId("metric-chart-domain")).toHaveTextContent(
      "1–5 秒",
    );
    expect(screen.queryByText(/seconds/)).not.toBeInTheDocument();
  });
});
