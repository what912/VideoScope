import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectorClient } from "./connector-client";

describe("public-site connector client", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("pairs without persisting API keys and sends the short session", async () => {
    const fetcher = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            session_token: "session-token-that-is-long-enough",
            expires_at: "2099-01-01T00:00:00Z",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    const client = new ConnectorClient();
    await client.pair("pair-code");
    expect(client.isPaired()).toBe(true);
    await expect(client.providers()).resolves.toEqual([]);
    expect(fetcher.mock.calls[1]?.[1]).toMatchObject({
      headers: { "X-VideoScope-Session": "session-token-that-is-long-enough" },
    });
    expect(JSON.stringify(sessionStorage)).not.toContain("api_key");
  });

  it("uses one fixed loopback origin for every full mode", () => {
    const client = new ConnectorClient();
    expect(client.workbenchUrl("publish")).toBe(
      "http://127.0.0.1:8765/?mode=publish",
    );
    expect(client.workbenchUrl("privacy")).toContain("?mode=privacy");
    expect(client.workbenchUrl("rescue")).toContain("?mode=rescue");
    expect(client.workbenchUrl("content")).toContain("?mode=content");
  });
});
