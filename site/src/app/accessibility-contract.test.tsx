import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { MemoryRouter } from "react-router";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { createDemoReport } from "../data/demo-report";
import { ComparePage } from "../features/compare/ComparePage";
import { WorkspacePage } from "../features/workspace/WorkspacePage";
import { MemoryReportStore } from "../services/report-store/memory-report-store";
import { auditAccessibility } from "../test/accessibility-audit";
import { AppProviders } from "./AppProviders";
import { TestApp } from "./router";

beforeAll(async () => {
  await Promise.all([
    import("../features/auth/AuthPage"),
    import("../features/auth/AuthCallbackPage"),
    import("../features/compare/ComparePage"),
    import("../features/report/ReportPage"),
    import("../features/static/DocsPage"),
    import("../features/static/NotFoundPage"),
    import("../features/static/PrivacyPage"),
    import("../features/workspace/WorkspacePage"),
  ]);
});

function expectNoAuditIssues(
  container: HTMLElement,
  options?: Parameters<typeof auditAccessibility>[1],
) {
  expect(auditAccessibility(container, options)).toEqual([]);
}

describe("public route accessibility contract", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it.each([
    "/",
    "/workspace",
    "/compare",
    "/report/demo",
    "/auth",
    "/privacy",
    "/docs",
    "/outside",
  ])("audits the complete %s route shell", async (route) => {
    const view = render(<TestApp initialEntries={[route]} />);

    await screen.findByRole(
      "heading",
      { level: 1 },
      { timeout: 3_000 },
    );
    expectNoAuditIssues(view.container, { requireSiteLandmarks: true });
  });

  it("audits a full-data Workspace and its destructive confirmation dialog", async () => {
    const report = createDemoReport("en");
    const store = new MemoryReportStore();
    await store.put(report);
    const view = render(
      <AppProviders initialLocale="en">
        <MemoryRouter
          initialEntries={[`/workspace?report=${encodeURIComponent(report.id)}`]}
        >
          <WorkspacePage
            getSessionVideo={() => null}
            isMobile={false}
            reportStore={store}
          />
        </MemoryRouter>
      </AppProviders>,
    );

    await screen.findByRole("heading", { name: report.title });
    expectNoAuditIssues(view.container);
    fireEvent.click(screen.getByRole("button", { name: "Clear local data" }));
    expect(await screen.findByRole("dialog")).toHaveAttribute(
      "aria-modal",
      "true",
    );
    expectNoAuditIssues(document.body);
  });

  it("audits a full-data synchronized comparison", async () => {
    const reportA = createDemoReport("en");
    const reportB = {
      ...createDemoReport("en"),
      analysis_id: "comparison-analysis-b",
      id: "comparison-report-b",
      title: "Comparison B",
    };
    const view = render(
      <AppProviders initialLocale="en">
        <MemoryRouter>
          <ComparePage
            initialA={{ mediaUrl: "blob:comparison-a", report: reportA }}
            initialB={{ mediaUrl: "blob:comparison-b", report: reportB }}
          />
        </MemoryRouter>
      </AppProviders>,
    );

    await screen.findByRole("heading", { name: "Compare videos" });
    expectNoAuditIssues(view.container);
  });

  it("audits the report share dialog and mobile navigation dialog", async () => {
    const reportView = render(<TestApp initialEntries={["/report/demo"]} />);
    await screen.findByRole("heading", {
      name: "Video Observatory interactive demonstration",
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Share sanitized report" }),
    );
    expect(await screen.findByRole("dialog")).toHaveAttribute(
      "aria-modal",
      "true",
    );
    expectNoAuditIssues(document.body);
    reportView.unmount();

    render(<TestApp initialEntries={["/"]} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Open navigation menu" }),
    );
    expect(await screen.findByRole("dialog")).toHaveAttribute(
      "aria-modal",
      "true",
    );
    expectNoAuditIssues(document.body);
  });
});
