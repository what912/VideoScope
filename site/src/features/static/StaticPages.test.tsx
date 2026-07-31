import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../../app/router";
import { createDemoReport } from "../../data/demo-report";
import { I18nProvider } from "../../i18n/I18nProvider";
import { MemoryReportStore } from "../../services/report-store/memory-report-store";
import { PrivacyPage } from "./PrivacyPage";
import { PUBLIC_RECOVERY_STATE_IDS } from "./recovery-states";

describe("public static pages", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("explains every local, optional-network, storage, and sharing boundary", async () => {
    render(<TestApp initialEntries={["/privacy"]} />);

    expect(
      await screen.findByRole("heading", { name: "Privacy by default" }),
    ).toBeVisible();
    expect(screen.getByText(/decoded in this browser/i)).toBeVisible();
    expect(screen.getByText(/IndexedDB/i)).toBeVisible();
    expect(screen.getByText(/source host can observe/i)).toBeVisible();
    expect(screen.getByText(/optional account/i)).toBeVisible();
    expect(screen.getByText(/sanitized report/i)).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Open local data controls" }),
    ).toHaveAttribute("href", "/workspace");
  });

  it("shows actual local usage and clears it without requiring a selected report", async () => {
    const store = new MemoryReportStore();
    await store.put(createDemoReport("en"));
    window.localStorage.setItem(
      "videoscope.share-records.v1.owner-a",
      JSON.stringify([
        {
          publicId: "public-a",
          createdAt: "2026-07-30T08:00:00.000Z",
          reportId: "demo-report",
          title: "Team review",
        },
      ]),
    );
    window.localStorage.setItem("another.application.preference", "keep-me");
    const clearSession = vi.fn();
    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <PrivacyPage clearSession={clearSession} reportStore={store} />
        </I18nProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("1 saved report")).toBeVisible();
    expect(screen.getByText("1 saved share link")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Delete all local data" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm deletion" }),
    );

    expect(
      await screen.findByRole("status", {
        name: "Local browser data deleted.",
      }),
    ).toBeVisible();
    await waitFor(async () =>
      expect((await store.usage()).report_count).toBe(0),
    );
    expect(
      window.localStorage.getItem("videoscope.share-records.v1.owner-a"),
    ).toBeNull();
    expect(
      window.localStorage.getItem("another.application.preference"),
    ).toBe("keep-me");
    expect(clearSession).toHaveBeenCalledOnce();
  });

  it("attempts every local clear operation and reports a partial failure", async () => {
    const store = new MemoryReportStore();
    await store.put(createDemoReport("en"));
    const storage = new Map<string, string>([
      [
        "videoscope.share-records.v1.owner-a",
        JSON.stringify([
          {
            publicId: "public-a",
            createdAt: "2026-07-30T08:00:00.000Z",
            reportId: "demo-report",
            title: "Team review",
          },
        ]),
      ],
    ]);
    const shareStorage: Storage = {
      get length() {
        return storage.size;
      },
      clear: () => storage.clear(),
      getItem: (key) => storage.get(key) ?? null,
      key: (index) => [...storage.keys()][index] ?? null,
      removeItem: () => {
        throw new Error("blocked");
      },
      setItem: (key, value) => storage.set(key, value),
    };
    const clearSession = vi.fn();
    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <PrivacyPage
            clearSession={clearSession}
            reportStore={store}
            shareStorage={shareStorage}
          />
        </I18nProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText("1 saved share link")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Delete all local data" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm deletion" }),
    );

    expect(
      await screen.findByRole("alert", {
        name: "Some local data could not be deleted.",
      }),
    ).toBeVisible();
    await waitFor(async () =>
      expect((await store.usage()).report_count).toBe(0),
    );
    expect(clearSession).toHaveBeenCalledOnce();
    expect(screen.getByText("1 saved share link")).toBeVisible();
  });

  it("discloses the local share index in both supported languages", async () => {
    render(<TestApp initialEntries={["/privacy"]} />);

    expect(
      await screen.findByText(/localStorage keeps a minimal share-link index/i),
    ).toBeVisible();
    fireEvent.change(screen.getByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });

    expect(
      screen.getByText(
        /\u672c\u5730\u5b58\u50a8\u8fd8\u4f1a\u4fdd\u7559\u6700\u5c0f\u5316\u7684\u5206\u4eab\u94fe\u63a5\u7d22\u5f15/u,
      ),
    ).toBeVisible();
  });

  it("discloses the production account-deletion blocker in both languages", async () => {
    render(<TestApp initialEntries={["/privacy"]} />);

    expect(
      await screen.findByText(/account deletion is not available/i),
    ).toBeVisible();
    fireEvent.change(screen.getByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });

    expect(
      screen.getByText(/暂不提供账户删除/u),
    ).toBeVisible();
  });

  it("distinguishes browser preview from every desktop-only capability", async () => {
    render(<TestApp initialEntries={["/docs"]} />);

    expect(
      await screen.findByRole("heading", { name: "Choose the right VideoScope workflow" }),
    ).toBeVisible();
    const matrix = screen.getByRole("table", { name: "Capability matrix" });
    for (const capability of [
      "FFmpeg probing",
      "Benchmark",
      "AI providers",
      "OCR",
      "Web API",
    ]) {
      expect(within(matrix).getByText(capability)).toBeVisible();
    }
    expect(within(matrix).getAllByText("Desktop").length).toBeGreaterThan(0);
    expect(screen.getByText(/four bounded CPU heuristics/i)).toBeVisible();
  });

  it("publishes an actionable recovery entry for every required state", async () => {
    render(<TestApp initialEntries={["/docs"]} />);

    await screen.findByRole("heading", { name: "Troubleshooting" });
    for (const stateId of PUBLIC_RECOVERY_STATE_IDS) {
      expect(screen.getByTestId(`recovery-${stateId}`)).toBeVisible();
    }
  });

  it("switches static-page copy to Simplified Chinese while preserving what912", async () => {
    render(<TestApp initialEntries={["/privacy"]} />);

    fireEvent.change(await screen.findByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });

    expect(
      screen.getByRole("heading", { name: "默认保护隐私" }),
    ).toBeVisible();
    expect(screen.getByText("what912")).toBeVisible();
  });

  it("localizes the docs capability matrix in Simplified Chinese", async () => {
    render(<TestApp initialEntries={["/docs"]} />);

    fireEvent.change(await screen.findByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });

    const matrix = screen.getByRole("table", { name: "能力对照表" });
    expect(within(matrix).getByText("四个浏览器 CPU 检测器")).toBeVisible();
    expect(within(matrix).getByText("FFmpeg 探测")).toBeVisible();
  });

  it("renders an actionable localized not-found route instead of a raw router error", async () => {
    render(<TestApp initialEntries={["/missing-route"]} />);

    expect(
      await screen.findByRole("heading", { name: "This route is outside the observatory" }),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Return home" })).toHaveAttribute(
      "href",
      "/",
    );
  });
});
