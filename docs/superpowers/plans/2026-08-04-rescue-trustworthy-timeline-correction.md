# Rescue Trustworthy Timeline Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale-low FFprobe duration from bypassing decoded-tail auditing while preserving ordinary fixed-rate cadence and bounded single-decode sampling.

**Architecture:** Every `sample_frames(..., timeline_duration_seconds=...)` call enters the existing streaming timeline decoder. The probe-derived requested count selects either fixed-rate targets for an uncapped input or uniformly distributed endpoint targets for a capped input; the decoder still runs to EOF and validates its actual normalized end before publishing samples.

**Tech Stack:** Python 3.11+, FFmpeg/ffprobe subprocess argument arrays, Pillow, Pydantic, pytest.

## Global Constraints

- Base tests remain offline and CPU-only; no model, GPU, or network dependency is added.
- External commands use argument arrays with `shell=False`; user paths are never interpolated into shell strings.
- Timeline requests use exactly one FFmpeg video decode and no full-frame ffprobe scan.
- Retained PNG payloads remain bounded at two; staged selected candidates remain bounded at 1,000.
- Accurate uncapped inputs retain target times `0, 1/rate, 2/rate, ...` strictly before the proposed duration.
- Capped inputs retain uniformly distributed targets including both proposed endpoints.
- A decoded end that materially disagrees with probe timing fails closed and cleans partial files.
- Non-timeline callers preserve the existing legacy fixed-rate extraction path.
- No absolute personal path may appear in diagnostics, reports, tests, or documentation.

---

### Task 1: Audit every Rescue timeline request in the streaming decoder

**Files:**
- Modify: `src/videoscope/video/sampling.py`
- Modify: `tests/video/test_sampling.py`
- Modify: `tests/rescue/test_performance.py`

**Interfaces:**
- Consumes: `sample_frames(path, sample_rate, max_edge, image_format, workspace_parent, ffmpeg, ffprobe, timeout_seconds, max_samples, frame_indices, timeline_duration_seconds) -> FrameSamplingResult`.
- Consumes: `_stream_timeline_candidates(...) -> _TimelineStreamResult` and its bounded one-pass decoder.
- Produces: unchanged public `FrameSamplingResult`; `truncated` is true only when the probe-derived fixed-rate target count exceeds the effective cap.
- Produces: private target selection that uses fixed-rate targets when uncapped and uniform endpoint targets when capped.

- [ ] **Step 1: Add a real failing stale-low routing regression**

In `tests/rescue/test_performance.py`, use the existing real 12-second resource video and monkeypatch only `_timeline_probe` so it returns `_TimelineProbe(duration_seconds=2.0, raw_duration_seconds=2.0)`. Configure `sample_rate=2.0` and `maximum_sample_count=6`, so the stale probe predicts four samples and the current implementation takes the legacy branch. Assert that `_sample_frames_once(...)` raises `FrameSamplingError` containing `duration`, that exactly one FFmpeg video `Popen` was started, and that the resulting workspace contains no published `*.png` files.

The production mutation this test catches is: restoring a duration-based branch that avoids the decoded-tail audit.

- [ ] **Step 2: Run the stale-low regression and verify RED**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python -m pytest tests/rescue/test_performance.py -k "stale_low_probe_cannot_bypass" -vv
```

Expected before implementation: FAIL because the legacy fixed-rate branch returns a successful beginning-only sample set instead of auditing the real 12-second tail.

- [ ] **Step 3: Add focused target-schedule tests**

In `tests/video/test_sampling.py`, cover private streaming behavior with literal expected timestamps:

```python
assert [sample.timestamp_seconds for sample in result.samples] == pytest.approx(
    [0.0, 0.5, 1.0, 1.5], abs=0.11
)
assert result.truncated is False
assert result.decode_passes == 1
```

Use a two-second, 2 fps accurate timeline and a cap above four. Keep the existing capped target, one-frame, non-zero PTS, VFR, duplicate-source-frame, cardinality, cleanup, and bounded-work tests. Update the existing real uncapped cadence regression so it requires one streaming `Popen` rather than asserting that timeline decoding is skipped.

The production mutation these assertions catch is: forcing the decoded last frame into an uncapped fixed-rate schedule or redistributing uncapped targets uniformly.

- [ ] **Step 4: Route every timeline request through bounded streaming selection**

In `_stream_timeline_candidates_unchecked`, retain the existing requested-count calculation and replace unconditional uniform target construction with:

```python
requested_count = max(1, math.ceil(duration_seconds * sample_rate - 1e-9))
truncated = requested_count > maximum_count
target_count = min(maximum_count, requested_count)
if truncated:
    targets = (
        (0.0,)
        if target_count == 1
        else tuple(
            position * duration_seconds / (target_count - 1)
            for position in range(target_count)
        )
    )
else:
    targets = tuple(position / sample_rate for position in range(target_count))
```

Only replace the final target with the decoded final frame when `truncated and target_count > 1`. Return `truncated=truncated`. Continue decoding after all targets have advanced so the existing actual-end audit remains authoritative.

In `sample_frames`, remove the `if requested_count > capped_count` routing condition. For every non-null `timeline_duration_seconds`, call `_stream_timeline_candidates` once, convert or move its staged candidates into the public frames directory, validate cardinality, clean `.timeline-candidates`, and return its `FrameSamplingResult`. Leave the code below this early return unchanged for non-timeline callers.

- [ ] **Step 5: Verify GREEN and resource invariants**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python -m pytest tests/video/test_sampling.py tests/rescue/test_performance.py -vv
```

Expected: all selected tests pass. Confirm instrumentation still reports one FFmpeg video process, retained payload high-water at most two, linear target/finalization visits, a public sample count at most the configured cap, deterministic continuous filenames, and cleanup after audit failure.

- [ ] **Step 6: Run Task 12 real verification and repository validation**

Run the real FFmpeg Task 12 test selection documented in the Task 12 report, then:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python scripts/validate.py
```

Expected: Ruff, format, and pytest pass. If the known installed NumPy stub remains incompatible with the configured mypy Python target, record that exact environmental blocker without changing or weakening mypy configuration.

- [ ] **Step 7: Self-review and commit**

Check the diff for an unchanged public API, no second decode/full-frame probe, no `shell=True`, bounded selection, cleanup on every failure path, and no unrelated refactor. Commit only the three listed source/test files:

```powershell
git add src/videoscope/video/sampling.py tests/video/test_sampling.py tests/rescue/test_performance.py
git commit -m "fix: audit every Rescue timeline decode"
```
