import { readFileSync } from "node:fs";
import path from "node:path";

import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import { AppProviders } from "../../app/AppProviders";
import { TestApp } from "../../app/router";
import { homepageMedia } from "../../data/media-manifest";
import { HomePage } from "./HomePage";

const tokensCss = readFileSync(
  path.resolve(process.cwd(), "src/styles/tokens.css"),
  "utf8",
);
const homeCss = readFileSync(
  path.resolve(process.cwd(), "src/features/home/home.css"),
  "utf8",
);

function renderHome() {
  return render(
    <AppProviders>
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>
    </AppProviders>,
  );
}

const forbiddenUniversalScoreClaims = [
  ["Overall", "Score"].join(" "),
  ["综合", "评分"].join(""),
  ["总质量", "分"].join(""),
];

describe("Video Observatory homepage", () => {
  let styles: HTMLStyleElement;

  beforeAll(() => {
    styles = document.createElement("style");
    styles.textContent = `${tokensCss}\n${homeCss}`;
    document.head.append(styles);
  });

  afterAll(() => styles.remove());

  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });

  it("explains interval-level diagnosis in the first section without a universal score", () => {
    renderHome();

    const hero = screen.getByTestId("home-hero");
    expect(
      within(hero).getByRole("heading", {
        name: "See what your video hides.",
      }),
    ).toBeVisible();
    expect(hero).toHaveTextContent(
      "Find the exact intervals that need review, inspect frame evidence, and understand each detector’s limitations.",
    );
    expect(hero).toHaveTextContent("5 review intervals");
    forbiddenUniversalScoreClaims.forEach((claim) => {
      expect(screen.queryByText(new RegExp(claim, "i"))).toBeNull();
    });
  });

  it("moves focus to the upload lab from the primary call to action", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    renderHome();

    fireEvent.click(
      screen.getAllByRole("button", { name: "Analyze a video" })[0],
    );

    expect(screen.getByTestId("home-upload-lab")).toHaveFocus();
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "start",
    });
  });

  it("keeps the centralized interactive demo synchronized", () => {
    renderHome();

    fireEvent.click(
      screen.getByRole("button", { name: "View interactive demo" }),
    );
    expect(screen.getAllByText("Temporal Flicker").length).toBeGreaterThan(0);
    expect(screen.getByTestId("home-demo-time")).toHaveTextContent("00:03.2");

    fireEvent.click(
      screen.getAllByRole("button", {
        name: /View details: Text Instability/,
      })[0],
    );

    expect(screen.getByTestId("home-demo-time")).toHaveTextContent("00:12.1");
    expect(screen.getAllByTestId("diagnostic-overlay")[0]).toHaveTextContent(
      "Text Instability",
    );
    expect(screen.getAllByTestId("finding-detail")[0]).toHaveTextContent(
      "demo_optional_text",
    );
  });

  it("labels every demo surface and every optional AI or OCR topic", () => {
    renderHome();

    const demoHeadings = [
      "See what your video hides.",
      "One timeline. Every observable interval.",
      "Follow the signal to the frame.",
      "Detector-local values, never a universal score.",
      "Editorial evidence atlas",
      "Compare observations, detector by detector.",
    ];
    demoHeadings.forEach((heading) => {
      const surface = screen.getByText(heading).closest("section");
      expect(surface).not.toBeNull();
      expect(
        within(surface as HTMLElement).getAllByText("INTERACTIVE DEMO").length,
      ).toBeGreaterThan(0);
    });

    screen.getAllByTestId("optional-demo-topic").forEach((topic) => {
      expect(topic).toHaveTextContent(/OPTIONAL|DEMO/);
    });
  });

  it("localizes the centralized demo findings, limitations, and metric labels", async () => {
    render(<TestApp initialEntries={["/"]} />);

    fireEvent.change(
      await screen.findByRole("combobox", { name: "Language" }),
      { target: { value: "zh-CN" } },
    );

    expect(await screen.findAllByText("时间闪烁")).not.toHaveLength(0);
    expect(screen.getAllByText("亮度稳定性")).not.toHaveLength(0);
    expect(
      screen.getAllByText("快速光照变化和刻意频闪可能呈现相似信号。"),
    ).not.toHaveLength(0);
    expect(screen.queryByText("Temporal Flicker")).not.toBeInTheDocument();
    expect(screen.queryByText("Luminance stability")).not.toBeInTheDocument();
  });

  it("uses a distinct local asset for every approved homepage media role", () => {
    renderHome();

    const rendered = screen.getAllByTestId("home-media-role");
    expect(rendered).toHaveLength(homepageMedia.length);
    const assetNames = rendered.map((node) => node.getAttribute("data-asset"));
    expect(new Set(assetNames).size).toBe(homepageMedia.length);
    expect(
      assetNames.every(
        (asset) =>
          asset?.startsWith("/VideoScope/media/") ||
          asset?.startsWith("/media/"),
      ),
    ).toBe(true);
  });

  it("keeps the hero heading legible when the surrounding interface uses the light theme", () => {
    document.documentElement.dataset.theme = "light";
    renderHome();

    const heading = within(screen.getByTestId("home-hero")).getByRole("heading");
    expect(getComputedStyle(heading).color).toBe("var(--color-soft-ivory)");
    expect(
      getComputedStyle(document.documentElement).getPropertyValue(
        "--color-soft-ivory",
      ),
    ).toBe("#f1f3ee");
  });

  it("labels every homepage media role as its current procedural scene", () => {
    renderHome();

    const videoLabels = [
      "Procedural optical aperture observatory scene",
      "Procedural night observation grid",
      "Procedural fluid spectrum",
      "Procedural diagnostic mesh",
      "Comparison A · procedural cool topography",
      "Comparison B · procedural dawn spectrum",
    ];
    const imageLabels = [
      "Procedural cyan caustic evidence study",
      "Procedural violet lattice evidence study",
      "Procedural amber contour evidence study",
    ];
    videoLabels.forEach((label) => {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    });
    imageLabels.forEach((label) => {
      expect(screen.getByAltText(label)).toBeInTheDocument();
    });
  });

  it("localizes every homepage media role as its current procedural scene", async () => {
    render(<TestApp initialEntries={["/"]} />);
    fireEvent.change(
      await screen.findByRole("combobox", { name: "Language" }),
      { target: { value: "zh-CN" } },
    );

    const videoLabels = [
      "程序化光学孔径观测场景",
      "程序化夜间观测网格",
      "程序化流体光谱",
      "程序化诊断网格",
      "对比 A · 程序化冷色地形",
      "对比 B · 程序化黎明光谱",
    ];
    const imageLabels = [
      "程序化青色焦散证据图",
      "程序化紫色晶格证据图",
      "程序化琥珀等高线证据图",
    ];
    videoLabels.forEach((label) => {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    });
    imageLabels.forEach((label) => {
      expect(screen.getByAltText(label)).toBeInTheDocument();
    });
  });

  it("keeps the creator mark literal after switching to Simplified Chinese", async () => {
    render(<TestApp initialEntries={["/"]} />);
    fireEvent.change(
      await screen.findByRole("combobox", { name: "Language" }),
      { target: { value: "zh-CN" } },
    );

    expect(
      await screen.findByRole("heading", { name: "看见视频隐藏的细节。" }),
    ).toBeVisible();
    expect(screen.getByText("what912")).toBeVisible();
    forbiddenUniversalScoreClaims.forEach((claim) => {
      expect(screen.queryByText(new RegExp(claim, "i"))).toBeNull();
    });
  });

  it("uses instant CTA scrolling when reduced motion is requested", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    renderHome();

    fireEvent.click(
      screen.getAllByRole("button", { name: "Analyze a video" })[0],
    );

    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "auto",
      block: "start",
    });
  });

  it("copies the open detector protocol example", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderHome();

    fireEvent.click(screen.getByRole("button", { name: "Copy JSON example" }));

    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('"detector_id": "global_flicker"'),
    );
    expect(await screen.findByText("Copied")).toHaveAttribute("role", "status");
  });
});
