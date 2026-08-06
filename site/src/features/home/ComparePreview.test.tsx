import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import { ComparePreview } from "./ComparePreview";

class ImmediateIntersectionObserver implements IntersectionObserver {
  readonly root = null;
  readonly rootMargin = "0px";
  readonly thresholds = [0];

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }

  private readonly callback: IntersectionObserverCallback;
  disconnect = vi.fn();
  takeRecords = () => [];
  unobserve = vi.fn();
  observe = (target: Element) => {
    this.callback(
      [
        {
          boundingClientRect: target.getBoundingClientRect(),
          intersectionRatio: 1,
          intersectionRect: target.getBoundingClientRect(),
          isIntersecting: true,
          rootBounds: null,
          target,
          time: 0,
        },
      ],
      this,
    );
  };
}

describe("ComparePreview", () => {
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", ImmediateIntersectionObserver);
  });

  it("seeks and controls both preview videos together", async () => {
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);

    render(
      <AppProviders>
        <MemoryRouter>
          <ComparePreview />
        </MemoryRouter>
      </AppProviders>,
    );

    const videos = await screen.findAllByLabelText<HTMLVideoElement>(
      /Comparison [AB]/,
      {
        selector: "video",
      },
    );
    videos.forEach((video) => {
      Object.defineProperty(video, "duration", {
        configurable: true,
        value: 20,
      });
    });

    fireEvent.change(
      screen.getByRole("slider", { name: "Synchronized preview position" }),
      { target: { value: "25" } },
    );
    expect(videos[0].currentTime).toBe(5);
    expect(videos[1].currentTime).toBe(5);

    fireEvent.click(screen.getByRole("button", { name: "Play both previews" }));
    expect(play).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "Pause both previews" }));
    expect(pause).toHaveBeenCalledTimes(2);
  });

  it("applies a requested seek after each preview receives metadata", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    render(
      <AppProviders>
        <MemoryRouter>
          <ComparePreview />
        </MemoryRouter>
      </AppProviders>,
    );
    const videos = await screen.findAllByLabelText<HTMLVideoElement>(
      /Comparison [AB]/,
      { selector: "video" },
    );

    fireEvent.change(
      screen.getByRole("slider", { name: "Synchronized preview position" }),
      { target: { value: "25" } },
    );
    videos.forEach((video) => {
      Object.defineProperty(video, "duration", {
        configurable: true,
        value: 20,
      });
      fireEvent.loadedMetadata(video);
    });

    expect(videos[0].currentTime).toBe(5);
    expect(videos[1].currentTime).toBe(5);
  });

  it("rolls both previews back to paused when either play request fails", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    render(
      <AppProviders>
        <MemoryRouter>
          <ComparePreview />
        </MemoryRouter>
      </AppProviders>,
    );
    const videos = await screen.findAllByLabelText<HTMLVideoElement>(
      /Comparison [AB]/,
      { selector: "video" },
    );
    const firstPause = vi.fn();
    const secondPause = vi.fn();
    videos[0]!.play = vi.fn().mockResolvedValue(undefined);
    videos[0]!.pause = firstPause;
    videos[1]!.play = vi.fn().mockRejectedValue(new Error("blocked"));
    videos[1]!.pause = secondPause;

    fireEvent.click(screen.getByRole("button", { name: "Play both previews" }));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Play both previews" }),
      ).toBeVisible(),
    );
    expect(firstPause).toHaveBeenCalled();
    expect(secondPause).toHaveBeenCalled();
  });

  it("pauses manual comparison playback when the preview leaves the viewport", async () => {
    const callbacks: IntersectionObserverCallback[] = [];
    class ControlledObserver extends ImmediateIntersectionObserver {
      constructor(callback: IntersectionObserverCallback) {
        super(callback);
        callbacks.push(callback);
      }
    }
    vi.stubGlobal("IntersectionObserver", ControlledObserver);
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);

    render(
      <AppProviders>
        <MemoryRouter>
          <ComparePreview />
        </MemoryRouter>
      </AppProviders>,
    );
    const videos = await screen.findAllByLabelText<HTMLVideoElement>(
      /Comparison [AB]/,
      { selector: "video" },
    );
    fireEvent.click(screen.getByRole("button", { name: "Play both previews" }));
    act(() => {
      callbacks[0]?.(
        [
          {
            boundingClientRect: videos[0]!.getBoundingClientRect(),
            intersectionRatio: 0,
            intersectionRect: videos[0]!.getBoundingClientRect(),
            isIntersecting: false,
            rootBounds: null,
            target: videos[0]!,
            time: 0,
          },
        ],
        {} as IntersectionObserver,
      );
    });

    expect(
      screen.getByRole("button", { name: "Play both previews" }),
    ).toBeVisible();
    expect(pause).toHaveBeenCalled();
  });

  it("keeps the play action idle when reduced motion replaces videos with posters", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockReturnValue({
        addEventListener: vi.fn(),
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        removeEventListener: vi.fn(),
      }),
    );

    render(
      <AppProviders>
        <MemoryRouter>
          <ComparePreview />
        </MemoryRouter>
      </AppProviders>,
    );

    const play = screen.getByRole("button", { name: "Play both previews" });
    fireEvent.click(play);
    expect(play).toHaveAccessibleName("Play both previews");
  });
});
