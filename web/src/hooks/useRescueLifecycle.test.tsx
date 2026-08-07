import { describe, expect, it } from "vitest";
import { mergeRescueSnapshot } from "./useRescueLifecycle";
import type { RescueJobResponse } from "../types";

const job = (status: RescueJobResponse["status"], updated = "2026-08-04T00:00:00Z"): RescueJobResponse => ({
  job_id: "a".repeat(32), status, message: status, created_at: updated, updated_at: updated,
  upload_size_bytes: 1, progress_percent: 10, strategy: "balanced", symptoms: [], plan_digest: null,
  locked_ranges: [], balanced_strength_limit: 0.6, private_artifacts: [],
  warnings: [], error: null, links: {},
});

describe("mergeRescueSnapshot", () => {
  it("ignores stale lifecycle snapshots", () => {
    expect(mergeRescueSnapshot(job("processing"), job("planning"))).toMatchObject({ status: "processing" });
  });
});
