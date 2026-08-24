# V15 Bounded Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, fail-closed real-source qualification for the omitted SHARPEN/DEBLUR and DENOISE_AUDIO actions, plus an optional transition-STABILIZE estimator profile, without changing any existing public verification threshold, tolerance, publication, or fallback rule.

**Architecture:** Keep the existing lifecycle `assessment -> draft plan -> private qualification -> final plan -> preview -> confirmation -> execution -> verification`. Qualification is a private pre-preview stage. Each track has a finite, deterministic profile inventory and a strict path-free evidence model. Evidence is bound to the draft/action/input, effective configuration, exact ranges and normalized actual PTS, same-generation controls, topology/encode contract, thresholds, metrics, and selected profile. The planner emits an action only from validated evidence; commands, preview, executor, and final verifier independently rederive the contract. Private controls are created from the immediate parent and removed in a `finally` block under the private workspace root. Existing behavior remains the fallback when no profile passes.

**Tech Stack:** Python 3.11+, Pydantic strict models, pathlib, NumPy/OpenCV measurement helpers already used by rescue verification, FFmpeg/ffprobe 8.1.2 for bounded native qualification only, pytest, Ruff, and mypy. No network, model download, GPU, Git, or publication work.

**Spec:** `docs/superpowers/specs/2026-08-21-v15-bounded-qualification-design.md`

## Global Constraints

- Preserve all current clarity, ringing, overshoot, noise, recovered-baseline, audio attenuation, persistence, boundary, stabilization P90, seam, crop, coverage, and required residual thresholds exactly. Do not add a tolerance, skip, fixture-name, hash, path, or codec exception.
- Keep SHARPEN/DEBLUR, DENOISE_AUDIO, and STABILIZE qualification independent. There is no joint optimizer and no automatic cross-action substitution.
- Every new model is strict, frozen where appropriate, `extra="forbid"`, finite, deterministic, canonical-JSON serializable, and path-free. Missing, stale, reordered, duplicate, range-incomplete, topology-mismatched, or semantically tampered evidence must fail closed.
- Do not preview or confirm a candidate until its qualification evidence has passed every required gate. A no-pass or unavailable provider writes a stable limitation and omits the action.
- Same-generation controls are temporary and confined to the current private root. Verify parent/control/candidate identity, SHA, topology, normalized actual PTS/count, ranges, and encode contract before measuring; cleanup failures are errors and path escapes are never unlinked.
- Preserve current GREEN behavior when an optional profile is unavailable or no profile passes. Do not claim a track is fixed when all real-source profiles are omitted.
- Before implementation, inspect existing code and reuse current contracts where possible; do not duplicate an already-correct model or bypass an existing trust boundary.
- No PREPARE, confirmation, execution, Task8, publication, network access, model download, or Git operation occurs during implementation or native qualification. Native runs are one per approved track in a fresh no-clobber directory and are never retried after failure.

## 1. Baseline and contract inventory

- [ ] **1.1 Record the current boundary.** Inspect the approved spec, current dirty-worktree diff, and the existing qualification paths in `models.py`, `qualification.py`, `tonal_qualification.py`, `tonal.py`, `planner.py`, `commands.py`, `preview.py`, `executor.py`, `pipeline.py`, and `verification.py`. Record which parts of the approved spec already exist from the prior V15 work and which are genuinely missing. Do not alter code in this step.
- [ ] **1.2 Add a focused inventory test.** Extend `tests/rescue/test_v15_rnd.py` (or add `tests/rescue/test_v15_qualification.py` if separation is clearer) with a deterministic assertion that the three track profile inventories are finite, ordered, unique, and independent. The test must be RED for any unbounded, duplicate, or cross-track inventory and GREEN against the existing defaults.
- [ ] **1.3 Establish commands and evidence locations.** Use current-source `PYTHONPATH`, the project virtual environment, fixed FFmpeg/ffprobe 8.1.2, offline CPU-only environment, and fresh no-clobber audit directories. Record exact commands in the task report; do not run native commands yet.

## 2. Shared strict qualification/control contracts

- [ ] **2.1 Write genuine RED tests for shared binding.** Add tests covering missing/stale/reordered/duplicated profile evidence, incomplete half-open ranges, non-normalized or duplicate PTS, source/control/candidate topology mismatch, encode-contract drift, input/action/draft ID mismatch, and path-bearing fields. Include same-generation control parent SHA and cleanup-root/path-escape cases. Tests must recompute action IDs and plan digests before asserting parser/command/preview/executor rejection.
- [ ] **2.2 Implement the smallest shared contract extension.** Extend the existing strict models in `src/videoscope/rescue/models.py` and `src/videoscope/rescue/qualification.py` only where missing. Reuse `VerificationControlRecipeV1`/handles and existing strict action/video contracts. Add a versioned, path-free qualification envelope with explicit track kind, finite profile order, exact ranges/PTS/topology/encode fields, control recipe, thresholds, metrics, and selected profile. Keep serialization canonical and deterministic, including signed-zero/finite float handling already established by the current visual evidence ordering.
- [ ] **2.3 Wire trust-boundary validation.** In `planner.py`, `commands.py`, `preview.py`, `executor.py`, and `pipeline.py`, validate the envelope independently at every boundary and recompute IDs/digests after embedding evidence. Create controls from the action's immediate parent, pass a runtime handle into final verification, and clean every temporary control in `finally` under the private root. RED cases must fail before writing a preview/runner output; cancellation and cleanup errors must remain structured errors.
- [ ] **2.4 Run focused GREEN and static checks.** Run the shared contract tests, then:
  `& .venv\Scripts\python.exe -m pytest tests\rescue\test_v15_qualification.py tests\rescue\test_v15_rnd.py -q`;
  `ruff check src tests`;
  `ruff format --check src tests`;
  `mypy --no-incremental src tests`.
  Expected result: all focused tests pass, no production threshold changes, and no new path-bearing evidence.

## 3. SHARPEN / DEBLUR clarity qualification

- [ ] **3.1 Add RED tests for the missing clarity semantics.** Cover a finite profile axis (including radius/strength or deblur profile values already represented by the effective config), complete retained-range coverage, same-generation baseline/visibility/control/candidate artifacts, exact PTS/topology/encode binding, first-all-pass selection, and no-pass omission. Add negative tests for partial range, missing control, wrong-generation baseline, ringing/overshoot/noise/recovered-baseline failures, and a candidate that passes only aggregate rather than every required gate.
- [ ] **3.2 Implement the provider against existing gates.** Extend `src/videoscope/rescue/qualification.py` and, where needed, `src/videoscope/rescue/deblur.py`/the existing sharpen renderer so the provider renders a finite set of real-source candidates from one parent and measures the complete retained range. Use existing `NativeRescueCandidateQualifier`, `SharpenQualificationProfile`, `SharpenQualificationEvidenceV1`, `validate_plan_sharpen_qualification_contracts`, and the final verifier’s clarity measurements rather than creating a second gate implementation. Select the first profile for which every unchanged required clarity/ringing/overshoot/noise/recovered-baseline gate passes; otherwise return a stable omission limitation.
- [ ] **3.3 Wire planning and rendering.** Update `planner.py`, `commands.py`, `preview.py`, and `executor.py` so the validated clarity evidence is embedded only in the final action, the candidate uses the same-generation baseline/visibility/control, and the full retained action range is rendered/measured before preview. Ensure DEBLUR is never mislabeled as SHARPEN and is omitted honestly when all deblur profiles fail.
- [ ] **3.4 Verify clarity locally.** Add/adjust focused tests in `tests/rescue/test_fixture_rescue.py`, `tests/rescue/test_deblur.py`, `tests/rescue/test_planner.py`, `tests/rescue/test_commands.py`, `tests/rescue/test_preview.py`, `tests/rescue/test_executor.py`, and `tests/rescue/test_verification.py`. Run the focused clarity selectors and the affected non-native suites; expected result is RED-to-GREEN without threshold or publisher changes.

## 4. DENOISE_AUDIO qualification

- [ ] **4.1 Add RED tests for encoded candidate qualification.** Extend `tests/rescue/test_tonal.py` and `tests/rescue/test_v15_qualification.py` to require a finite encoded AAC profile inventory from one parent, every complete 50-ms target and non-target window, persistence, and bilateral boundary-transient measurements. Include first-pass ordering, missing-window, timeline/topology, raw-PCM-only, boundary, and candidate/control identity failures.
- [ ] **4.2 Reuse and extend the V3 encoded contract.** Extend `src/videoscope/rescue/tonal_qualification.py` only where the approved spec is not already satisfied. Preserve `TonalEncodedQualificationEvidenceV3`, `TonalEncodedProfileQualificationV2`, `TonalEncodedCandidateAttemptV2`, `validate_encoded_tonal_qualification`, `validate_tonal_runtime_parent`, `validate_tonal_runtime_candidate`, and `NativeTonalCandidateQualifier`. Bind exact AAC topology/timeline, normalized actual PTS, complete 50-ms windows, persistent-tone checks, boundary metrics, profile order, and unchanged thresholds. Select the first all-pass candidate, otherwise omit DENOISE_AUDIO.
- [ ] **4.3 Wire the audio path and cleanup.** Ensure `executor.py` creates identity/control/candidate generations from the same parent, `verification.py` consumes only the bound encoded evidence, and `pipeline.py` cleans private audio controls in `finally`. Commands must use argument arrays with `shell=False`; unsupported/error/cancel paths must not leave partial candidate files.
- [ ] **4.4 Verify audio locally.** Run the focused tonal qualification selectors, the affected tonal/verification/planner/commands/preview/executor suites, Ruff, format, and mypy. Expected result: all existing on-bin/off-bin/native-negative tests retain their current semantics and no public result is emitted for a no-pass candidate.

## 5. Optional transition-STABILIZE estimator qualification

- [ ] **5.1 Add RED tests for the optional axis.** Extend `tests/rescue/test_stabilization.py` and a focused qualification test to require a finite estimator profile inventory, exact `transition_anchor_v1`, complete [32,36) range/96 actual PTS binding, crop/seam/consensus/coverage checks, and unchanged P90/residual gates. Include no-pass fallback to the current GREEN STABILIZE profile, profile-order determinism, missing/duplicate PTS, parent/control topology mismatch, and crop/seam failures.
- [ ] **5.2 Implement an optional profile provider.** Add a small track-local qualification module or extend `stabilization.py` without changing the existing default path. Render each finite estimator profile from the immediate parent, retain the same-generation identity control, measure full transition P90/seam/crop/required residual evidence, and select the first profile passing every unchanged required gate. If none passes or the provider is unavailable, preserve the existing STABILIZE action and limitation exactly.
- [ ] **5.3 Bind the optional evidence through the lifecycle.** Update planner/commands/preview/executor/pipeline/verifier seams to rederive the profile contract and clean controls. No profile may be previewed unless its full [32,36) evidence is present; no profile may alter the current thresholds or transition anchor semantics.
- [ ] **5.4 Verify STABILIZE locally.** Run focused synthetic/fake tests and affected non-native stabilization/planner/executor/verification tests, then Ruff/format/mypy. Expected result: current GREEN behavior is byte/metric stable when the optional provider returns no-pass.

## 6. Cross-track integration and evidence

- [ ] **6.1 Add final-plan integration tests.** Exercise `assessment -> draft -> private qualification -> final plan -> preview` for all three tracks. Assert action IDs and plan digests change only when bound qualification content changes; REMUX/VERIFY and unrelated actions do not drift. Assert omitted tracks carry stable limitations and cannot be confirmed.
- [ ] **6.2 Add publication/failure tests.** Verify optional needs-review checks remain visible but block publication, required failures omit the action, source and Downloads remain unchanged, no public path appears before successful verification, and cleanup errors/path escapes fail closed.
- [ ] **6.3 Run the affected non-native set and static gates.** Run the complete affected rescue suites (models, qualification, tonal, stabilization, deblur, planner, commands, preview, executor, pipeline, verification), then Ruff check/format and `mypy --no-incremental src tests`. Record exact counts and any skipped selectors; do not call a native node before independent review.

## 7. Independent review and bounded native qualification

- [ ] **7.1 Produce an audit-ready report.** Record source SHA, code/test aggregate SHA, profile inventories, thresholds (unchanged), candidate/control/parent hashes, normalized PTS/topology/encode evidence, exact limitations, and all unrun commands. List known retention limitations instead of inferring missing metrics.
- [ ] **7.2 Obtain an independent review.** The reviewer must inspect the spec, diff, evidence models, trust boundaries, and affected/static results. Proceed only with 0 Critical and 0 Important findings. Minor evidence limitations must be explicitly retained in the report.
- [ ] **7.3 Run at most one native qualification node per approved track.** Use a fresh no-clobber audit directory, current source, fixed FFmpeg/ffprobe 8.1.2, offline CPU-only environment, and the exact selector identified during implementation. Run the SHARPEN/DEBLUR clarity node, DENOISE_AUDIO encoded node, and optional STABILIZE node separately only when each has review approval. A failure is retained as RED and is never retried or “fixed” in the same stage.
- [ ] **7.4 Close native evidence.** Persist path-free qualification JSON, command/tool versions, source/artifact hashes, topology/timeline/PTS inventories, per-profile metrics, selected/no-pass decision, and cleanup/publication flags. If a profile fails, report the unchanged gate and omit it; do not fabricate a pass or substitute another action family.

## 8. Unified validation and handoff

- [x] **8.1 After all approved native qualification nodes, run one unified validation.** Use `python scripts/validate.py` exactly once with a fresh no-clobber log/cache, fixed FFmpeg/ffprobe 8.1.2, current source, offline CPU-only environment, and no network/model/Git. Do not modify or retry on failure. If the only failure is a known environment-only pinned-tool launch denial, retain it and request the separately authorized external identical rerun; any other failure remains blocking.
- [x] **8.2 Review unified results.** Confirm Ruff, format, mypy, base pytest, and isolated native counts. Verify no source/test changes occurred during validation and that all logs/reports are path-safe and hashable.
- [ ] **8.3 Only after a fully green validation, consider a new PREPARE-ONLY candidate.** The candidate must have a fresh plan/digest, exact action IDs, all preview hashes/topology/A/V decode evidence, explicit DEBLUR/SHARPEN/DENOISE/STABILIZE limitations or selected profiles, and no confirmation/execute/Task8/publication until the user gives fresh exact consent.

## 9. Completion checklist

- [x] All three tracks either have a real-source selected profile with complete evidence or an honest no-pass limitation.
- [x] No public threshold, tolerance, skip rule, fixture/path/hash exception, or publication rule changed.
- [x] All production and test modifications are listed in the final report.
- [x] Focused, affected, Ruff, format, mypy, approved native nodes, and unified validation results are recorded exactly; unrun checks are explicitly marked unrun.
- [x] No network/model/GPU/Git/prepare/confirm/execute/Task8/publication occurred outside explicitly authorized boundaries.
