import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { usePrivacyLifecycle, type PrivacyLifecycleApi } from "./usePrivacyLifecycle";
import {
  PRIVACY_PROFILES,
  PRIVACY_RISK_MAP,
  privacyJob,
} from "../test/privacyFixtures";

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it("ignores an old job stage response after recovery switches to a new job", async () => {
  const oldJobId = "a".repeat(32);
  const newJobId = "b".repeat(32);
  const oldRiskMap = deferred<typeof PRIVACY_RISK_MAP>();
  const newRiskMap = {
    ...PRIVACY_RISK_MAP,
    input_hash: "9".repeat(64),
    risks: [PRIVACY_RISK_MAP.risks[1]],
  };
  const api: PrivacyLifecycleApi = {
    getJob: vi.fn(async (jobId) =>
      privacyJob("awaiting_review", { job_id: jobId }),
    ),
    getRiskMap: vi.fn((jobId) =>
      jobId === oldJobId ? oldRiskMap.promise : Promise.resolve(newRiskMap),
    ),
    getPlan: vi.fn(),
    getTechnicalReport: vi.fn(),
    subscribeToEvents: vi.fn(() => ({ close: vi.fn() })),
  };
  const { result, rerender } = renderHook(
    ({ jobId }) => usePrivacyLifecycle({ api, jobId }),
    { initialProps: { jobId: oldJobId } },
  );

  await waitFor(() => expect(api.getRiskMap).toHaveBeenCalledWith(oldJobId));
  rerender({ jobId: newJobId });
  await waitFor(() => expect(result.current.riskMap?.input_hash).toBe(newRiskMap.input_hash));
  await act(async () => oldRiskMap.resolve(PRIVACY_RISK_MAP));
  expect(result.current.riskMap?.input_hash).toBe(newRiskMap.input_hash);
  expect(api.getRiskMap).toHaveBeenCalledWith(oldJobId);
  expect(api.getRiskMap).toHaveBeenCalledWith(newJobId);
  expect(PRIVACY_PROFILES).toHaveLength(2);
});

it.each(["needs_review", "partial", "failed"] as const)(
  "does not request public reports for a %s Safe Sharing result",
  async (status) => {
    const jobId = "c".repeat(32);
    const api: PrivacyLifecycleApi = {
      getJob: vi.fn(async () => privacyJob(status, { job_id: jobId })),
      getRiskMap: vi.fn(),
      getPlan: vi.fn(),
      getTechnicalReport: vi.fn(async () => {
        throw new Error("public report must remain unavailable");
      }),
      subscribeToEvents: vi.fn(() => ({ close: vi.fn() })),
    };
    const { result } = renderHook(() =>
      usePrivacyLifecycle({ api, jobId }),
    );

    await waitFor(() => expect(result.current.job?.status).toBe(status));
    expect(result.current.technicalReport).toBeNull();
    expect(api.getTechnicalReport).not.toHaveBeenCalled();
  },
);
