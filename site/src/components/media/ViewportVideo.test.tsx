import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { homepageMedia } from "../../data/media-manifest";
import { ViewportVideo } from "./ViewportVideo";

const HERO_VIDEO = "/VideoScope/media/hero-optical.mp4";
const HERO_POSTER = "/VideoScope/media/hero-optical.webp";

let observerCallback: IntersectionObserverCallback;
const observe = vi.fn();
const disconnect = vi.fn();
const observerConstructed = vi.fn();
const reducedMotionListeners = new Set<
  (event: MediaQueryListEvent) => void
>();
let reducedMotionMatches = false;

class FakeIntersectionObserver implements IntersectionObserver {
  readonly root = null;
  readonly rootMargin = "0px";
  readonly thresholds = [0];

  constructor(callback: IntersectionObserverCallback) {
    observerCallback = callback;
    observerConstructed();
  }

  disconnect = disconnect;
  observe = observe;
  takeRecords = () => [];
  unobserve = vi.fn();
}

function setReducedMotion(matches: boolean) {
  reducedMotionMatches = matches;
  reducedMotionListeners.clear();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string): MediaQueryList => {
      const mediaQueryList = {
        get matches() {
          return reducedMotionMatches;
        },
        media: query,
        onchange: null,
        addEventListener: (
          type: string,
          listener: (event: MediaQueryListEvent) => void,
        ) => {
          if (type === "change") {
            reducedMotionListeners.add(listener);
          }
        },
        removeEventListener: (
          type: string,
          listener: (event: MediaQueryListEvent) => void,
        ) => {
          if (type === "change") {
            reducedMotionListeners.delete(listener);
          }
        },
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      };
      return mediaQueryList as unknown as MediaQueryList;
    }),
  });
}

function sendReducedMotion(matches: boolean) {
  reducedMotionMatches = matches;
  const event = {
    matches,
    media: "(prefers-reduced-motion: reduce)",
  } as MediaQueryListEvent;

  act(() => {
    for (const listener of reducedMotionListeners) {
      listener(event);
    }
  });
}

function sendIntersection(target: Element, isIntersecting: boolean) {
  const entry = {
    boundingClientRect: target.getBoundingClientRect(),
    intersectionRatio: isIntersecting ? 1 : 0,
    intersectionRect: target.getBoundingClientRect(),
    isIntersecting,
    rootBounds: null,
    target,
    time: 0,
  } satisfies IntersectionObserverEntry;

  act(() => {
    observerCallback([entry], {} as IntersectionObserver);
  });
}

describe("ViewportVideo", () => {
  beforeEach(() => {
    setReducedMotion(false);
    observe.mockClear();
    disconnect.mockClear();
    observerConstructed.mockClear();
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  });

  afterEach(() => {
    Reflect.deleteProperty(navigator, "connection");
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps playback muted, inline, and user-agent idle before intersection", () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    const { unmount } = render(
      <ViewportVideo
        label="Abstract blue and green light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    const video = screen.getByLabelText("Abstract blue and green light");
    expect(video).toHaveAttribute("muted");
    expect(video).toHaveAttribute("playsinline");
    expect(video).not.toHaveAttribute("autoplay");
    expect(video).toHaveAttribute("poster", HERO_POSTER);
    expect(video).not.toHaveAttribute("src");
    expect(video).toHaveAttribute("preload", "none");
    unmount();
  });

  it("loads metadata eagerly only when explicitly requested", () => {
    render(
      <ViewportVideo
        eager
        label="Hero optical light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    const video = screen.getByLabelText("Hero optical light");
    expect(video).toHaveAttribute("src", HERO_VIDEO);
    expect(video).toHaveAttribute("preload", "metadata");
  });

  it("plays only while intersecting and pauses after leaving the viewport", async () => {
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue();
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);

    const { unmount } = render(
      <ViewportVideo
        label="Abstract blue and green light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    const video = screen.getByLabelText("Abstract blue and green light");
    expect(observe).toHaveBeenCalledWith(video);
    expect(video).not.toHaveAttribute("src");
    expect(play).not.toHaveBeenCalled();

    sendIntersection(video, true);
    expect(video).toHaveAttribute("src", HERO_VIDEO);
    expect(play).toHaveBeenCalledOnce();

    sendIntersection(video, false);
    expect(pause).toHaveBeenCalledOnce();
    unmount();
  });

  it("uses only the poster when data saver is enabled", () => {
    Object.defineProperty(navigator, "connection", {
      configurable: true,
      value: { saveData: true },
    });

    render(
      <ViewportVideo
        eager
        label="Data saver optical light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    const poster = screen.getByRole("img", {
      name: "Data saver optical light",
    });
    expect(poster).toHaveAttribute("src", HERO_POSTER);
    expect(poster).toHaveAttribute("loading", "eager");
    expect(
      screen.queryByLabelText("Data saver optical light", {
        selector: "video",
      }),
    ).not.toBeInTheDocument();
  });

  it("renders the poster without observing or playing under reduced motion", () => {
    setReducedMotion(true);
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue();

    render(
      <ViewportVideo
        label="Abstract blue and green light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    const poster = screen.getByRole("img", {
      name: "Abstract blue and green light",
    });
    expect(poster).toHaveAttribute("src", HERO_POSTER);
    expect(poster).toHaveAttribute("loading", "lazy");
    expect(screen.queryByLabelText("Abstract blue and green light", {
      selector: "video",
    })).not.toBeInTheDocument();
    expect(observerConstructed).not.toHaveBeenCalled();
    expect(play).not.toHaveBeenCalled();
  });

  it("pauses and switches to the poster when reduced motion becomes enabled", () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);

    render(
      <ViewportVideo
        label="Abstract blue and green light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    expect(
      screen.getByLabelText("Abstract blue and green light", {
        selector: "video",
      }),
    ).toBeInTheDocument();

    sendReducedMotion(true);

    expect(
      screen.getByRole("img", { name: "Abstract blue and green light" }),
    ).toHaveAttribute("src", HERO_POSTER);
    expect(pause).toHaveBeenCalledOnce();
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it("disconnects the observer and pauses unconditionally on unmount", () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);
    const { unmount } = render(
      <ViewportVideo
        label="Abstract blue and green light"
        poster={HERO_POSTER}
        src={HERO_VIDEO}
      />,
    );

    unmount();

    expect(disconnect).toHaveBeenCalledOnce();
    expect(pause).toHaveBeenCalledOnce();
  });
});

describe("homepage media manifest", () => {
  it("assigns a distinct local file to every homepage role", () => {
    const roles = homepageMedia.map(({ role }) => role);
    const primaryFiles = homepageMedia.map(
      ({ poster, video }) => video ?? poster,
    );

    expect(homepageMedia).toHaveLength(9);
    expect(new Set(roles).size).toBe(homepageMedia.length);
    expect(new Set(primaryFiles).size).toBe(homepageMedia.length);
    expect(
      primaryFiles.every(
        (filename) =>
          filename.startsWith("/") && filename.includes("/media/"),
      ),
    ).toBe(true);
  });
});
