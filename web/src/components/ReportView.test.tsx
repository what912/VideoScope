import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import mockReportData from "../mocks/mock-report.json";
import type { AnalysisReport } from "../types";
import { ReportView } from "./ReportView";

const mockReport = mockReportData as AnalysisReport;

describe("report view", () => {
  it("renders an explicit empty state for a report with no findings", () => {
    const empty: AnalysisReport = {
      ...mockReport,
      findings: [],
      detector_executions: mockReport.detector_executions.map((execution) => ({
        ...execution,
        findings_count: 0,
        status: "ok",
        error_type: null,
        error_message: null,
      })),
    };
    render(
      <ReportView
        jobId="mock"
        report={empty}
        videoSource={null}
        mockMode
        onNewAnalysis={vi.fn()}
      />,
    );
    expect(screen.getByText("No findings match these filters.")).toBeInTheDocument();
    expect(screen.getByText("00")).toBeInTheDocument();
  });

  it("shows detector errors separately and keeps successful findings", () => {
    render(
      <ReportView
        jobId="mock"
        report={mockReport}
        videoSource={null}
        mockMode
        onNewAnalysis={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("1 detector");
    expect(screen.getAllByText("Near-black interval detected")).not.toHaveLength(0);
    expect(screen.getByText("FrameSequenceError", { exact: false })).toBeInTheDocument();
  });

  it("selecting a finding exposes its evidence and limitations", () => {
    render(
      <ReportView
        jobId="mock"
        report={mockReport}
        videoSource={null}
        mockMode
        onNewAnalysis={vi.fn()}
      />,
    );
    const findingButton = screen
      .getAllByRole("button", {
        name: /Possible frozen or repeated frames/,
      })
      .find((button) => button.classList.contains("finding-row"));
    expect(findingButton).toBeDefined();
    fireEvent.click(findingButton!);
    expect(screen.getByText("Repeated-frame midpoint")).toBeInTheDocument();
    expect(screen.getByText(/deliberately static shot/)).toBeInTheDocument();
  });
});
