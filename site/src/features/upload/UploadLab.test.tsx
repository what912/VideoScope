import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import { I18nProvider } from "../../i18n/I18nProvider";
import type { BrowserAnalysisService } from "../../services/browser-analysis";
import { BrowserAnalysisError } from "../../services/browser-analysis/errors";
import { DirectMediaImportError } from "../../services/browser-analysis/url-import";
import type { ReportStore } from "../../services/report-store/report-store";
import { UploadLab } from "./UploadLab";

const neverCompletes: BrowserAnalysisService = {
  analyzeLocalVideo: vi.fn(
    (_file, _options, signal, onProgress) =>
      new Promise<never>((_resolve, reject) => {
        onProgress({ stage: "sampling_frames", progress: 0.35 });
        signal.addEventListener("abort", () =>
          reject(new DOMException("cancelled", "AbortError")),
        );
      }),
  ),
};

const store: ReportStore = {
  put: vi.fn(),
  get: vi.fn(),
  list: vi.fn(),
  delete: vi.fn(),
  clear: vi.fn(),
  usage: vi.fn(),
};

function renderLab(locale: "en" | "zh-CN" = "en") {
  return render(
    <MemoryRouter>
      <I18nProvider initialLocale={locale}>
        <UploadLab
          analysisService={neverCompletes}
          reportStore={store}
          navigate={vi.fn()}
          createObjectURL={() => "blob:upload"}
          revokeObjectURL={vi.fn()}
          importUrl={vi.fn()}
          loadSample={vi.fn()}
        />
      </I18nProvider>
    </MemoryRouter>,
  );
}

describe("UploadLab", () => {
  it.each([
    [
      "metadata_unavailable",
      "The browser could not read video metadata.",
    ],
    [
      "duration_unavailable",
      "The browser could not determine the video duration.",
    ],
    ["decode_failed", "The browser could not decode this video."],
    [
      "canvas_unavailable",
      "Canvas frame analysis is unavailable in this browser.",
    ],
    [
      "memory_pressure",
      "Browser memory was insufficient for this scan. Try Quick Scan or the desktop CLI.",
    ],
  ] as const)(
    "renders the actionable %s browser failure in the Upload Lab",
    async (code, message) => {
      const analysisService: BrowserAnalysisService = {
        analyzeLocalVideo: vi
          .fn()
          .mockRejectedValue(new BrowserAnalysisError(code, "private detail")),
      };
      render(
        <MemoryRouter>
          <I18nProvider initialLocale="en">
            <UploadLab
              analysisService={analysisService}
              reportStore={store}
              navigate={vi.fn()}
              createObjectURL={() => "blob:upload"}
              revokeObjectURL={vi.fn()}
              importUrl={vi.fn()}
              loadSample={vi.fn()}
            />
          </I18nProvider>
        </MemoryRouter>,
      );

      fireEvent.change(screen.getByLabelText("Choose a local video"), {
        target: {
          files: [new File(["video"], "clip.mp4", { type: "video/mp4" })],
        },
      });
      fireEvent.click(screen.getByRole("button", { name: "Start analysis" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(screen.queryByText("private detail")).not.toBeInTheDocument();
    },
  );

  it("announces drag state and shows a localized unsupported-type error", () => {
    renderLab();
    const dropzone = screen.getByTestId("upload-dropzone");
    expect(screen.getByLabelText("Choose a local video")).toHaveAttribute(
      "accept",
      expect.stringContaining(".mkv"),
    );

    fireEvent.dragEnter(dropzone);
    expect(dropzone).toHaveAttribute("data-dragging", "true");
    fireEvent.drop(dropzone, {
      dataTransfer: {
        files: [new File(["text"], "notes.txt", { type: "text/plain" })],
      },
    });

    expect(
      screen.getByText("Choose an MP4, WebM, MOV, or MKV video."),
    ).toBeInTheDocument();
  });

  it("keeps direct URL fetching behind explicit consent", async () => {
    const importUrl = vi.fn().mockResolvedValue(
      new File(["video"], "remote.mp4", { type: "video/mp4" }),
    );
    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <UploadLab
            analysisService={neverCompletes}
            reportStore={store}
            navigate={vi.fn()}
            createObjectURL={() => "blob:upload"}
            revokeObjectURL={vi.fn()}
            importUrl={importUrl}
            loadSample={vi.fn()}
          />
        </I18nProvider>
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Direct video URL"), {
      target: { value: "https://media.example/video.mp4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import URL" }));
    expect(importUrl).not.toHaveBeenCalled();
    expect(
      screen.getByText("Confirm the network notice before importing."),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /source host may observe my IP address/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Import URL" }));

    await waitFor(() =>
      expect(importUrl).toHaveBeenCalledWith(
        "https://media.example/video.mp4",
        expect.objectContaining({ consent: true }),
      ),
    );
  });

  it("explains that a reachable network error may mean a redirect was refused", async () => {
    const importUrl = vi
      .fn()
      .mockRejectedValue(new DirectMediaImportError("cors_or_network"));
    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <UploadLab
            analysisService={neverCompletes}
            reportStore={store}
            navigate={vi.fn()}
            createObjectURL={() => "blob:upload"}
            revokeObjectURL={vi.fn()}
            importUrl={importUrl}
            loadSample={vi.fn()}
          />
        </I18nProvider>
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Direct video URL"), {
      target: { value: "https://media.example/redirecting-video" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /source host may observe my IP address/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Import URL" }));

    expect(
      await screen.findByText(/attempted a redirect.*refuses/i),
    ).toBeInTheDocument();
  });

  it("shows truthful compare and batch behaviors", () => {
    renderLab();

    expect(
      screen.getByRole("link", { name: /Compare videos/i }),
    ).toHaveAttribute("href", "/compare");
    expect(
      screen.getByRole("button", { name: /Batch evaluate/i }),
    ).toBeDisabled();
    expect(
      screen.getByText("Batch evaluation is available in the desktop CLI."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /desktop CLI documentation/i }),
    ).toHaveAttribute("href", "/docs#batch-analysis");
  });

  it("renders named analysis progress and allows cancellation", async () => {
    renderLab();
    const input = screen.getByLabelText("Choose a local video");
    fireEvent.change(input, {
      target: {
        files: [new File(["video"], "clip.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start analysis" }));

    expect(
      await screen.findByText("Sampling local frames"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel analysis" }));
    expect(await screen.findByText("Analysis cancelled")).toBeInTheDocument();
  });

  it("aborts a direct URL import when the Upload Lab unmounts", async () => {
    let importSignal: AbortSignal | undefined;
    const importUrl = vi.fn(
      (_input, dependencies) =>
        new Promise<File>(() => {
          importSignal = dependencies.signal;
        }),
    );
    const view = render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <UploadLab
            analysisService={neverCompletes}
            reportStore={store}
            navigate={vi.fn()}
            createObjectURL={() => "blob:upload"}
            revokeObjectURL={vi.fn()}
            importUrl={importUrl}
            loadSample={vi.fn()}
          />
        </I18nProvider>
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Direct video URL"), {
      target: { value: "https://media.example/video.mp4" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /source host may observe my IP address/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Import URL" }));
    await waitFor(() => expect(importSignal).toBeDefined());

    view.unmount();

    expect(importSignal?.aborted).toBe(true);
  });

  it("ignores a stale sample failure after a newer URL import succeeds", async () => {
    let rejectSample: ((reason?: unknown) => void) | undefined;
    const loadSample = vi.fn(
      () =>
        new Promise<File>((_resolve, reject) => {
          rejectSample = reject;
        }),
    );
    const importUrl = vi.fn().mockResolvedValue(
      new File(["remote"], "remote.mp4", { type: "video/mp4" }),
    );
    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <UploadLab
            analysisService={neverCompletes}
            reportStore={store}
            navigate={vi.fn()}
            createObjectURL={() => "blob:upload"}
            revokeObjectURL={vi.fn()}
            importUrl={importUrl}
            loadSample={loadSample}
          />
        </I18nProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Use bundled sample" }));
    fireEvent.change(screen.getByLabelText("Direct video URL"), {
      target: { value: "https://media.example/video.mp4" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /source host may observe my IP address/i,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Import URL" }));
    expect(await screen.findByText("remote.mp4")).toBeInTheDocument();

    await act(async () => {
      rejectSample?.(new Error("late sample failure"));
      await Promise.resolve();
    });

    expect(screen.getByText("remote.mp4")).toBeInTheDocument();
    expect(
      screen.queryByText("Local analysis stopped because of an internal error."),
    ).not.toBeInTheDocument();
  });

  it("clears a completed job state when a new file is selected", async () => {
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn().mockRejectedValue(new Error("failed")),
    };
    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <UploadLab
            analysisService={analysisService}
            reportStore={store}
            navigate={vi.fn()}
            createObjectURL={() => "blob:upload"}
            revokeObjectURL={vi.fn()}
            importUrl={vi.fn()}
            loadSample={vi.fn()}
          />
        </I18nProvider>
      </MemoryRouter>,
    );

    const input = screen.getByLabelText("Choose a local video");
    fireEvent.change(input, {
      target: {
        files: [new File(["first"], "first.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start analysis" }));
    expect(
      await screen.findByText(
        "Local analysis stopped because of an internal error.",
      ),
    ).toBeInTheDocument();

    fireEvent.change(input, {
      target: {
        files: [new File(["second"], "second.mp4", { type: "video/mp4" })],
      },
    });

    expect(
      screen.queryByText(
        "Local analysis stopped because of an internal error.",
      ),
    ).not.toBeInTheDocument();
    expect(screen.getByText("second.mp4")).toBeInTheDocument();
  });

  it("records the current reduced-motion preference in analysis options", async () => {
    const originalMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    });
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn(
        (_file, _options, signal) =>
          new Promise<never>((_resolve, reject) => {
            signal.addEventListener("abort", () =>
              reject(new DOMException("cancelled", "AbortError")),
            );
          }),
      ),
    };
    const view = render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <UploadLab
            analysisService={analysisService}
            reportStore={store}
            navigate={vi.fn()}
            createObjectURL={() => "blob:upload"}
            revokeObjectURL={vi.fn()}
            importUrl={vi.fn()}
            loadSample={vi.fn()}
          />
        </I18nProvider>
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByLabelText("Choose a local video"), {
      target: {
        files: [new File(["video"], "clip.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start analysis" }));

    await waitFor(() =>
      expect(analysisService.analyzeLocalVideo).toHaveBeenCalledWith(
        expect.any(File),
        expect.objectContaining({ reduced_motion: true }),
        expect.any(AbortSignal),
        expect.any(Function),
      ),
    );

    view.unmount();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: originalMatchMedia,
    });
  });

  it("localizes upload validation errors in Simplified Chinese", () => {
    renderLab("zh-CN");
    fireEvent.click(screen.getByRole("button", { name: "开始分析" }));

    expect(screen.getByText("请选择一个本地视频。")).toBeInTheDocument();
  });
});
