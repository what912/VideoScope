import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "..",
  "supabase",
  "migrations",
  "202607290001_public_site_auth_and_reports.sql",
);
const migration = readFileSync(migrationPath, "utf8");

describe("shared report database boundary", () => {
  it("uses owner-scoped writes and a controlled public-id RPC", () => {
    expect(migration).toMatch(/create table if not exists public\.shared_reports/i);
    expect(migration).toMatch(/enable row level security/i);
    expect(migration).toMatch(/auth\.uid\(\)\s*=\s*owner_id/i);
    expect(migration).toMatch(/get_shared_report\s*\(\s*requested_public_id/i);
    expect(migration).toMatch(/revoked_at is null/i);
    expect(migration).toMatch(
      /public\.shared_reports\.expires_at is null\s+or\s+public\.shared_reports\.expires_at > pg_catalog\.now\(\)/i,
    );
    expect(migration).toMatch(/revoke all on public\.shared_reports from anon/i);
    expect(migration).toMatch(/set search_path = ''/i);
    expect(migration).toMatch(/pg_catalog\.now\(\)/i);
    expect(migration).not.toMatch(
      /create policy\s+"[^"]*anon[^"]*"\s+on public\.shared_reports\s+for select/i,
    );
  });
});
