import { describe, expect, it, vi } from "vitest";

import {
  DIRECT_MEDIA_MAX_BYTES,
  importDirectMediaUrl,
} from "./url-import";

function responseFromChunks(
  chunks: Uint8Array[],
  options: {
    contentType?: string;
    contentLength?: number;
    redirected?: boolean;
    url?: string;
  } = {},
) {
  let index = 0;
  return {
    ok: true,
    redirected: options.redirected ?? false,
    url: options.url ?? "https://media.example/video.mp4",
    headers: new Headers({
      "content-type": options.contentType ?? "video/mp4",
      ...(options.contentLength === undefined
        ? {}
        : { "content-length": String(options.contentLength) }),
    }),
    body: new ReadableStream<Uint8Array>({
      pull(controller) {
        const chunk = chunks[index++];
        if (chunk) controller.enqueue(chunk);
        else controller.close();
      },
    }),
  } as Response;
}

describe("importDirectMediaUrl", () => {
  it("requires explicit consent before fetch", async () => {
    const fetch = vi.fn();

    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: false,
        fetch,
      }),
    ).rejects.toMatchObject({ code: "consent_required" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    "javascript:alert(1)",
    "http://media.example/video.mp4",
    "https://user:password@media.example/video.mp4",
    "https://media.example:8443/video.mp4",
  ])("rejects unsafe URL %s without fetching", async (url) => {
    const fetch = vi.fn();

    await expect(
      importDirectMediaUrl(url, { consent: true, fetch }),
    ).rejects.toMatchObject({ code: "invalid_url" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("returns a local File for a CORS-readable video response", async () => {
    const fetch = vi.fn().mockResolvedValue(
      responseFromChunks([new Uint8Array([1, 2, 3])], {
        url: "https://media.example/final-video.mp4",
      }),
    );

    const file = await importDirectMediaUrl(
      "https://media.example/video.mp4",
      { consent: true, fetch },
    );

    expect(fetch).toHaveBeenCalledWith(
      "https://media.example/video.mp4",
      expect.objectContaining({ redirect: "error" }),
    );
    expect(file).toMatchObject({
      name: "final-video.mp4",
      size: 3,
      type: "video/mp4",
    });
  });

  it("rejects non-video responses", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        responseFromChunks([new Uint8Array([1])], {
          contentType: "text/html",
        }),
      );

    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: true,
        fetch,
      }),
    ).rejects.toMatchObject({ code: "invalid_content_type" });
  });

  it("rejects oversized declared and streamed bodies", async () => {
    const declared = vi.fn().mockResolvedValue(
      responseFromChunks([], {
        contentLength: DIRECT_MEDIA_MAX_BYTES + 1,
      }),
    );
    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: true,
        fetch: declared,
      }),
    ).rejects.toMatchObject({ code: "file_too_large" });

    const streamed = vi.fn().mockResolvedValue(
      responseFromChunks(
        [new Uint8Array(8), new Uint8Array(8)],
        { contentLength: undefined },
      ),
    );
    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: true,
        fetch: streamed,
        maxBytes: 10,
      }),
    ).rejects.toMatchObject({ code: "file_too_large" });
  });

  it("rejects every redirected response instead of contacting a redirect target", async () => {
    const fetch = vi.fn().mockResolvedValue(
      responseFromChunks([new Uint8Array([1])], {
        redirected: true,
        url: "https://other.example/video.mp4",
      }),
    );

    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: true,
        fetch,
      }),
    ).rejects.toMatchObject({ code: "redirect_not_supported" });
  });

  it("rejects a response without a readable stream without materializing its body", async () => {
    const blob = vi.fn().mockResolvedValue(
      new Blob([new Uint8Array(32)], { type: "video/mp4" }),
    );
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      redirected: false,
      url: "https://media.example/video.mp4",
      headers: new Headers({ "content-type": "video/mp4" }),
      body: null,
      blob,
    } as unknown as Response);

    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: true,
        fetch,
      }),
    ).rejects.toMatchObject({ code: "stream_unavailable" });
    expect(blob).not.toHaveBeenCalled();
  });

  it("maps opaque fetch failures to a public CORS or network error", async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(
      importDirectMediaUrl("https://media.example/video.mp4", {
        consent: true,
        fetch,
      }),
    ).rejects.toMatchObject({ code: "cors_or_network" });
  });
});
