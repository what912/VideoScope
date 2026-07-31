import { describe, expect, it, vi } from "vitest";

import { createDemoReport } from "../../data/demo-report";
import {
  createShareClient,
  FakeShareClient,
  isUnavailableShareClient,
  SupabaseShareClient,
} from "./index";
import { sanitizeReportForShare } from "./sanitize-report";

describe("share client configuration", () => {
  it("stays unavailable unless sharing is explicitly enabled and public settings exist", () => {
    expect(
      isUnavailableShareClient(
        createShareClient({
          anonKey: "anon",
          enabled: "false",
          url: "https://project.supabase.co",
        }),
      ),
    ).toBe(true);
    expect(
      isUnavailableShareClient(
        createShareClient({ enabled: "true", url: "https://project.supabase.co" }),
      ),
    ).toBe(true);
  });

  it("inserts only the sanitized JSON and share envelope", async () => {
    const insert = vi.fn(async () => ({ error: null }));
    const randomId = vi.fn(() => "public-random-id");
    const now = vi.fn(() => new Date("2026-07-30T08:00:00.000Z"));
    const client = new SupabaseShareClient({
      anonKey: "anon",
      loadSupabase: async () => ({
        createClient: () =>
          ({
            from: (table: string) => {
              expect(table).toBe("shared_reports");
              return { insert };
            },
          }) as never,
      }),
      now,
      randomId,
      url: "https://project.supabase.co",
    });
    const report = sanitizeReportForShare(createDemoReport("en"), {
      includePrompt: false,
      selectedEvidence: new Set(),
    });

    const result = await client.createShare({
      expiresAt: "2026-08-06T08:00:00.000Z",
      ownerId: "owner-123",
      report,
    });

    expect(result).toEqual({
      createdAt: "2026-07-30T08:00:00.000Z",
      expiresAt: "2026-08-06T08:00:00.000Z",
      publicId: "public-random-id",
    });
    expect(insert).toHaveBeenCalledWith({
      created_at: "2026-07-30T08:00:00.000Z",
      expires_at: "2026-08-06T08:00:00.000Z",
      owner_id: "owner-123",
      public_id: "public-random-id",
      report_json: report,
      revoked_at: null,
    });
    expect(JSON.stringify(insert.mock.calls)).not.toMatch(
      /blob:|data:image|file:\/\//i,
    );
    expect(randomId).toHaveBeenCalledOnce();
  });

  it("provides an offline fake client for UI tests", async () => {
    const client = new FakeShareClient();
    const report = sanitizeReportForShare(createDemoReport("en"), {
      includePrompt: false,
      selectedEvidence: new Set(),
    });

    await client.createShare({ ownerId: "owner", report });

    expect(client.requests).toHaveLength(1);
  });

  it("reads one public report through the controlled RPC and revokes an owned link", async () => {
    const report = sanitizeReportForShare(createDemoReport("en"), {
      includePrompt: false,
      selectedEvidence: new Set(),
    });
    const rpc = vi.fn(async () => ({
      data: [{ report_json: report }],
      error: null,
    }));
    const select = vi.fn(async () => ({
      data: [{ public_id: "public-id" }],
      error: null,
    }));
    const eq = vi.fn(() => ({ select }));
    const update = vi.fn(() => ({ eq }));
    const client = new SupabaseShareClient({
      anonKey: "anon",
      loadSupabase: async () => ({
        createClient: () =>
          ({
            from: () => ({ update }),
            rpc,
          }) as never,
      }),
      url: "https://project.supabase.co",
    });

    await expect(client.getSharedReport("public-id")).resolves.toEqual(report);
    expect(rpc).toHaveBeenCalledWith("get_shared_report", {
      requested_public_id: "public-id",
    });

    await client.revokeShare("public-id");
    expect(update).toHaveBeenCalledWith({
      revoked_at: expect.stringMatching(/Z$/),
    });
    expect(eq).toHaveBeenCalledWith("public_id", "public-id");
    expect(select).toHaveBeenCalledWith("public_id");
  });

  it("treats an RLS-filtered zero-row revoke as a failure", async () => {
    const select = vi.fn(async () => ({ data: [], error: null }));
    const eq = vi.fn(() => ({ select }));
    const client = new SupabaseShareClient({
      anonKey: "anon",
      loadSupabase: async () => ({
        createClient: () =>
          ({
            from: () => ({
              update: () => ({ eq }),
            }),
          }) as never,
      }),
      url: "https://project.supabase.co",
    });

    await expect(client.revokeShare("not-owned")).rejects.toThrow(
      /not found or not owned/i,
    );
  });

  it("treats an empty public RPC result as missing and rejects an invalid payload", async () => {
    const responses = [
      { data: [], error: null },
      { data: [{ report_json: { share_schema_version: "invalid" } }], error: null },
    ];
    const client = new SupabaseShareClient({
      anonKey: "anon",
      loadSupabase: async () => ({
        createClient: () =>
          ({
            rpc: async () => responses.shift(),
          }) as never,
      }),
      url: "https://project.supabase.co",
    });

    await expect(client.getSharedReport("missing")).resolves.toBeNull();
    await expect(client.getSharedReport("invalid")).rejects.toThrow(
      /shared report/i,
    );
  });
});
