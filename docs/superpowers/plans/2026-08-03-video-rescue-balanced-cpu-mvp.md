# Video Rescue Balanced CPU MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, CPU-first Video Rescue workflow that delivers an independently verified faithful rescue and, when evidence supports it, a preview-confirmed improved viewing copy without modifying the source.

**Architecture:** Add a versioned `videoscope.rescue` domain beside the existing Check, Publish Ready, and Safe Sharing domains. A streaming scanner creates a deterministic damage map; a planner produces conservative and balanced candidates; bounded previews bind human confirmation to an exact plan; isolated executors create new media; a verification gate and artifact manager decide whether each output is completed, partial, needs review, or failed.

**Tech Stack:** Python 3.11+, Pydantic, NumPy, OpenCV headless, FFmpeg/ffprobe subprocess argument arrays, Typer, FastAPI, React, strict TypeScript, Vitest, pytest, Ruff, mypy.

## Global Constraints

- Preserve the frozen v0.1 `AnalysisReport`, detector protocol, `videoscope analyze`, Publish Ready, and Safe Sharing behavior.
- Source videos are read-only; every media-changing action writes a new file in a job workspace and is delivered atomically.
- Base installation and base tests are offline, CPU-only, GPU-free, model-free, and do not download assets.
- Every external command uses an argument array with `shell=False`, a bounded timeout, checked return code, and sanitized stderr.
- Windows, Linux, and macOS paths are supported, including spaces, Chinese, and non-ASCII characters.
- Public JSON and reports contain only normalized output-root-relative paths and never include a username or absolute personal path.
- `faithful-rescue.mp4` and `improved-viewing.mp4` are independently verified; failure of the improved copy cannot invalidate a verified faithful copy.
- A stale or mismatched plan digest is rejected before execution; any plan change invalidates prior confirmation.
- Conservative never performs subjective enhancement. Balanced only proposes evidence-triggered actions with configured strength limits and preview confirmation.
- AI interpolation, super-resolution, generative completion, model download, remote processing, and GPU execution are out of scope.
- Do not describe sharpening, filtering, cropping, held frames, or synthesized media as recovered source information.
- Do not calculate an uncalibrated overall quality score, recovery percentage, or real-world accuracy claim.
- The recoverable-time ratio means scanned decodable duration divided by scanned source duration; it is not a visual quality score.
- Damage IDs, action order, preview selection, plans, source mappings, and public JSON are deterministic for the same input, versions, and effective configuration.
- Unverified required checks produce `needs_review` or `failed`, never `completed`.
- Do not push, open a Pull Request, publish, deploy, tag, or create a release unless the user separately authorizes it.

---

## File structure

New Python package:

```text
src/videoscope/rescue/
  __init__.py          stable public Rescue exports
  errors.py            structured sanitized workflow errors
  models.py            versioned requests, damage map, plan, artifacts and reports
  serialization.py     canonical UTF-8 JSON readers and atomic writers
  scanner.py           streaming packet/frame scan and damage interval mapping
  symptoms.py          observable symptom classification
  planner.py           deterministic Conservative/Balanced planning
  commands.py          FFmpeg/ffprobe argument builders
  preview.py           bounded same-range source/faithful/improved previews
  visual.py            luma, noise, sharpness and flicker measurements/actions
  stabilization.py     bounded CPU motion estimation and stabilization
  audio.py             loudness, clipping, noise and fixed-offset actions
  executor.py          staged faithful and improved execution
  verification.py      independent output checks and status gate
  artifacts.py         private review/public result isolation and atomic publication
  pipeline.py          prepare, confirm, execute, cancel and result orchestration
```

New Web orchestration:

```text
src/videoscope/web/rescue_jobs.py
```

New React units:

```text
web/src/rescueI18n.ts
web/src/hooks/useRescueLifecycle.ts
web/src/components/RescueView.tsx
web/src/components/RescueSymptomSelector.tsx
web/src/components/RescueDamageTimeline.tsx
web/src/components/RescuePreviewComparison.tsx
web/src/components/RescuePlanReview.tsx
web/src/components/RescueResult.tsx
```

Focused tests mirror these modules under `tests/rescue/`, with API, CLI,
fixture, distribution, and component coverage in the existing suites.

---

### Task 1: Formal scope, Rescue domain contract, and canonical JSON

**Files:**
- Create: `src/videoscope/rescue/__init__.py`
- Create: `src/videoscope/rescue/errors.py`
- Create: `src/videoscope/rescue/models.py`
- Create: `src/videoscope/rescue/serialization.py`
- Create: `tests/rescue/__init__.py`
- Create: `tests/rescue/test_models.py`
- Create: `tests/rescue/test_serialization.py`
- Create: `docs/rescue-schema.md`
- Modify: `docs/product-spec.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`

**Interfaces:**
- Produces: `RescueStrategy`, `RescueSymptom`, `DamageKind`, `DamageInterval`, `MediaDamageMap`, `RescueActionKind`, `RescueAction`, `RescueEffectiveConfig`, `RescuePlan`, `RescueConfirmation`, `RescueArtifact`, `RescueChangeLog`, `RescueVerificationStatus`, `RescueVerificationCheck`, `RescueVerificationReport`, and `RescueTechnicalReport`.
- Produces: `make_damage_id(input_hash, stream_id, kind, start_seconds, end_seconds) -> str` and `make_rescue_plan_digest(plan_without_digest: Mapping[str, JsonValue]) -> str`.
- Produces: `RescueError`, `RescueInputError`, `RescueScanError`, `RescuePlanError`, `RescueConfirmationError`, `RescueMediaError`, `RescueArtifactError`, and `RescueCancelledError` with sanitized public messages.
- Consumes: existing strict Pydantic and atomic JSON conventions without modifying `AnalysisReport`.

- [ ] **Step 1: Write model RED tests**

```python
def test_damage_id_is_deterministic() -> None:
    first = make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 2.0, 3.5)
    second = make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 2.0, 3.5)
    assert first == second
    assert first.startswith("damage_")


def test_damage_interval_rejects_reverse_time() -> None:
    with pytest.raises(ValueError):
        DamageInterval.model_validate(
            make_damage_payload(start_seconds=4.0, end_seconds=3.0)
        )


def test_plan_rejects_stale_digest() -> None:
    payload = make_plan_payload()
    payload["plan_digest"] = "0" * 64
    with pytest.raises(ValueError, match="plan_digest"):
        RescuePlan.model_validate(payload)
```

- [ ] **Step 2: Run model tests and verify RED**

Run `python -m pytest tests/rescue/test_models.py -v`.

Expected: collection fails because `videoscope.rescue.models` does not exist.

- [ ] **Step 3: Implement strict versioned models**

Use these exact stable enum values:

```python
RESCUE_SCHEMA_VERSION = "0.1"


class RescueStrategy(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"


class DamageKind(StrEnum):
    DECODABLE = "decodable"
    UNDECODABLE = "undecodable"
    TIMESTAMP_DISCONTINUITY = "timestamp_discontinuity"
    MISSING_STREAM = "missing_stream"
    FIXED_AV_OFFSET = "fixed_av_offset"
    DARK = "dark"
    VIDEO_NOISE = "video_noise"
    SOFT_DETAIL = "soft_detail"
    FLICKER = "flicker"
    SHAKE = "shake"
    LOW_LOUDNESS = "low_loudness"
    AUDIO_NOISE = "audio_noise"
    AUDIO_CLIPPING = "audio_clipping"
    UNCERTAIN = "uncertain"
    MISSING_INFORMATION = "missing_information"


class RescueOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class RescueVerificationStatus(StrEnum):
    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
```

Every model uses `ConfigDict(extra="forbid")`. Seconds are finite and
non-negative; intervals have `end_seconds >= start_seconds`; artifact paths are
normalized relative POSIX paths; SHA-256 values are lowercase 64-character hex.
`RescueAction` contains `id`, `version`, `kind`, `description`, `source_ranges`,
`parameters`, `changes_content`, `requires_confirmation`, `depends_on`, and
`fallback`. `RescuePlan` contains one strategy, effective config, ordered actions,
preview ranges, expected private/public artifacts, and a canonical digest.
`RescueConfirmation` contains `plan_digest`, `publish_faithful: Literal[True]`,
`publish_improved: bool`, `accepted_action_ids`, and
`accepted_trim_damage_ids`. Every accepted ID must exist in the confirmed plan;
`publish_improved=True` requires at least one accepted Balanced action.

- [ ] **Step 4: Write serialization RED tests**

```python
@pytest.mark.parametrize(
    ("writer", "reader", "factory"),
    [
        (write_damage_map_json, read_damage_map_json, make_damage_map),
        (write_rescue_plan_json, read_rescue_plan_json, make_plan),
        (
            write_rescue_technical_report_json,
            read_rescue_technical_report_json,
            make_report,
        ),
    ],
)
def test_atomic_json_round_trip_in_unicode_directory(
    tmp_path: Path,
    writer: Callable[[object, Path], None],
    reader: Callable[[Path], object],
    factory: Callable[[], object],
) -> None:
    destination = tmp_path / "中文 目录" / "result.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    value = factory()
    writer(value, destination)
    assert reader(destination) == value
    assert list(destination.parent.glob("*.tmp")) == []
```

- [ ] **Step 5: Implement canonical JSON and scope documentation**

Use UTF-8, `ensure_ascii=False`, `allow_nan=False`, sorted object keys,
deterministic model ordering, newline termination, validation before write, and
same-directory atomic replacement. Document Rescue as a post-v0.1 Resolve line
and link `docs/rescue-schema.md` from the product, architecture, and roadmap.

- [ ] **Step 6: Verify and commit**

```powershell
python -m pytest tests/rescue/test_models.py tests/rescue/test_serialization.py -v
python scripts/validate.py
git add docs src/videoscope/rescue tests/rescue
git commit -m "feat: define Video Rescue domain contract"
```

Expected: focused and repository validation pass with the frozen report schema unchanged.

---

### Task 2: Streaming damage scanner and deterministic damaged-media fixtures

**Files:**
- Create: `src/videoscope/rescue/scanner.py`
- Create: `src/videoscope/rescue/symptoms.py`
- Create: `tests/rescue/test_scanner.py`
- Create: `tests/rescue/test_symptoms.py`
- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`
- Modify: `tests/test_fixture_factory.py`

**Interfaces:**
- Consumes: Task 1 `MediaDamageMap`, `DamageInterval`, `DamageKind`, and ID helper.
- Produces: `RescueScanConfig`, `PacketObservation`, `DecodeObservation`, `RescueScanner.scan(source: Path, input_hash: str, metadata: VideoMetadata, config: RescueScanConfig) -> MediaDamageMap`.
- Produces: `classify_symptoms(damage_map: MediaDamageMap, requested: tuple[RescueSymptom, ...]) -> tuple[RescueSymptomAssessment, ...]`.

- [ ] **Step 1: Write scanner RED tests**

```python
def test_scanner_recovers_after_middle_decode_failure(tmp_path: Path) -> None:
    runner = FakeMediaRunner(
        packet_observations=observations_with_gap(2.0, 3.0),
        decode_failures=(DecodeFailure(2.0, 3.0, "invalid data"),),
    )
    damage_map = RescueScanner(runner=runner).scan(
        source=tmp_path / "损坏 视频.mp4",
        input_hash="a" * 64,
        metadata=video_metadata(duration_seconds=6.0),
        config=RescueScanConfig(),
    )
    assert [
        (item.kind, item.start_seconds, item.end_seconds)
        for item in damage_map.intervals
    ] == [
        (DamageKind.DECODABLE, 0.0, 2.0),
        (DamageKind.UNDECODABLE, 2.0, 3.0),
        (DamageKind.DECODABLE, 3.0, 6.0),
    ]
```

- [ ] **Step 2: Run and verify RED**

Run `python -m pytest tests/rescue/test_scanner.py tests/rescue/test_symptoms.py -v`.

Expected: missing scanner and symptom modules.

- [ ] **Step 3: Implement bounded streaming scan**

Use ffprobe packet/frame JSON in bounded chunks or line-delimited form, never a
whole decoded video in memory. Record stream identity, DTS/PTS monotonicity,
packet gaps, decode errors, first/last valid timestamps, and scan coverage.
Merge only adjacent intervals of the same kind within configured tolerance.
Sanitize paths from stderr and cap summaries at 2 KiB.

- [ ] **Step 4: Add deterministic damaged fixtures**

Add manifest entries and generators for:

```text
rescue_clean_av.mp4
rescue_missing_audio.mp4
rescue_low_loudness.mp4
rescue_fixed_av_offset.mp4
rescue_dark_noise.mp4
rescue_soft_detail.mp4
rescue_flicker.mp4
rescue_shake.mp4
rescue_tail_damaged.mp4
rescue_middle_damaged.mp4
```

Generate pristine sources at no more than `320x180`, `10` or `12` fps, and six
seconds. Corrupted variants are derived locally by deterministic byte-range or
container operations that preserve a documented source hash and expected damage
interval. Generated media remains ignored by Git.

- [ ] **Step 5: Verify fixtures, scanner, and commit**

```powershell
python scripts/generate_test_videos.py --force
python -m pytest tests/rescue/test_scanner.py tests/rescue/test_symptoms.py tests/test_fixture_factory.py -v
git add src/videoscope/rescue scripts/generate_test_videos.py tests/fixtures/manifest.json tests/rescue tests/test_fixture_factory.py
git commit -m "feat: scan recoverable and damaged video intervals"
```

Expected: all generated files are ffprobe-readable when expected; deliberate
corruption is reported with explicit tolerance and no fixture-name branches.

---

### Task 3: Conservative planning and bounded same-range previews

**Files:**
- Create: `src/videoscope/rescue/planner.py`
- Create: `src/videoscope/rescue/commands.py`
- Create: `src/videoscope/rescue/preview.py`
- Create: `tests/rescue/test_planner.py`
- Create: `tests/rescue/test_commands.py`
- Create: `tests/rescue/test_preview.py`

**Interfaces:**
- Consumes: `MediaDamageMap`, metadata, requested strategy, locked ranges, and effective config.
- Produces: `build_rescue_plan(...) -> RescuePlan`, `build_preview_commands(plan: RescuePlan, source: Path, work_root: Path) -> tuple[list[str], ...]`, and `RescuePreviewBuilder.build(plan, source, private_review_root) -> RescuePreviewSet`.

- [ ] **Step 1: Write planning RED tests**

```python
def test_conservative_plan_never_contains_subjective_enhancement() -> None:
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map_with_dark_noise(),
        strategy=RescueStrategy.CONSERVATIVE,
        config=effective_config(),
    )
    assert {action.kind for action in plan.actions}.isdisjoint(
        {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
            RescueActionKind.DEFLICKER,
            RescueActionKind.STABILIZE,
        }
    )


def test_preview_ranges_are_identical_across_variants() -> None:
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan=make_plan(preview_ranges=((2.0, 8.0),)),
        source=Path("input.mp4"),
        private_review_root=Path("private"),
    )
    assert previews.source.time_ranges == previews.faithful.time_ranges
    assert previews.source.time_ranges == previews.improved.time_ranges
```

- [ ] **Step 2: Implement deterministic action planning**

Use this stable action order:

```python
REMUX = "remux"
REBUILD_TIMESTAMPS = "rebuild_timestamps"
SELECT_TRACKS = "select_tracks"
NORMALIZE_ROTATION = "normalize_rotation"
SALVAGE_SEGMENTS = "salvage_segments"
TRIM_DAMAGED_EDGES = "trim_damaged_edges"
CORRECT_FIXED_AV_OFFSET = "correct_fixed_av_offset"
ADJUST_LUMA = "adjust_luma"
DENOISE_VIDEO = "denoise_video"
SHARPEN = "sharpen"
DEFLICKER = "deflicker"
STABILIZE = "stabilize"
NORMALIZE_AUDIO = "normalize_audio"
DENOISE_AUDIO = "denoise_audio"
VERIFY = "verify"
```

Locked ranges cannot be trimmed. Content-changing actions require confirmation.
Preview selection ranks damaged intervals by severity and coverage, then chooses
up to three non-overlapping ranges whose total duration is at most ten seconds.

- [ ] **Step 3: Implement safe command builders and previews**

Every builder returns `list[str]`; tests reject a string command and assert that
paths remain single arguments. Private previews are named only from variant and
index, never from the source filename. If no evidence supports an improved
variant, return `improved=None` rather than copying the faithful preview.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/rescue/test_planner.py tests/rescue/test_commands.py tests/rescue/test_preview.py -v
python scripts/validate.py
git add src/videoscope/rescue tests/rescue
git commit -m "feat: plan and preview conservative video rescue"
```

---

### Task 4: Faithful executor, segment salvage, and source mapping

**Files:**
- Create: `src/videoscope/rescue/executor.py`
- Create: `tests/rescue/test_executor.py`
- Modify: `src/videoscope/rescue/commands.py`

**Interfaces:**
- Consumes: confirmed `RescuePlan`, read-only source path, and validated work root.
- Produces: `RescueExecutionResult`, `RescuedSegment`, `SourceMapping`, and `NativeRescueExecutor.execute_faithful(plan, source, work_root, cancellation_callback) -> RescueExecutionResult`.

- [ ] **Step 1: Write executor RED tests**

```python
def test_middle_damage_yields_two_traceable_segments(tmp_path: Path) -> None:
    result = NativeRescueExecutor(runner=fake_success_runner()).execute_faithful(
        plan=plan_with_damage_gap(2.0, 3.0),
        source=tmp_path / "源 视频.mp4",
        work_root=tmp_path / "工作区",
        cancellation_callback=lambda: False,
    )
    assert [(s.source_start, s.source_end) for s in result.segments] == [
        (0.0, 2.0),
        (3.0, 6.0),
    ]
    assert all(
        segment.output_relative_path.startswith("staging/")
        for segment in result.segments
    )
```

- [ ] **Step 2: Implement faithful actions**

Prefer stream copy for valid remux-only paths. Re-encode only when timestamp,
rotation, concatenation, or decoder recovery requires it. Segment salvage seeks
from validated keyframes, writes independent temporary files, verifies each
file before concatenation, and preserves a source-to-output mapping. Cancellation
terminates the child process and never publishes a partial temporary file.

- [ ] **Step 3: Test real Unicode and corrupted media paths**

Add integration coverage using generated fixtures. Hash the source before and
after. Assert `faithful-rescue.mp4` is playable, source unchanged, and the mapped
duration equals the sum of retained source ranges within manifest tolerance.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/rescue/test_executor.py -v
python scripts/validate.py
git add src/videoscope/rescue tests/rescue
git commit -m "feat: execute faithful segment rescue"
```

---

### Task 5: Visual assessment and bounded luma, denoise, and sharpening actions

**Files:**
- Create: `src/videoscope/rescue/visual.py`
- Create: `tests/rescue/test_visual.py`
- Modify: `src/videoscope/rescue/planner.py`
- Modify: `src/videoscope/rescue/commands.py`

**Interfaces:**
- Produces: `VisualAssessment`, `LumaAdjustmentConfig`, `VideoDenoiseConfig`, `SharpenConfig`, `assess_visual_samples(samples, scenes, config) -> VisualAssessment`, and FFmpeg filter fragments with explicit numeric parameters.

- [ ] **Step 1: Write metric and planner RED tests**

```python
def test_clean_video_does_not_receive_balanced_filters() -> None:
    assessment = assess_visual_samples(clean_samples(), clean_scenes(), visual_config())
    assert assessment.recommended_actions == ()


def test_dark_noisy_video_gets_bounded_luma_and_denoise_without_false_recovery_claim() -> (
    None
):
    assessment = assess_visual_samples(
        dark_noisy_samples(), one_scene(), visual_config()
    )
    assert assessment.recommended_actions == (
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
    )
    assert "recover" not in assessment.public_explanation.lower()
```

- [ ] **Step 2: Implement evidence-triggered assessment**

Reuse sampled frames and scenes from Check where possible. Measure robust luma
percentiles, clipped-pixel ratios, noise residual relative to local structure,
and Laplacian sharpness relative to scene baseline. All thresholds and strengths
live in strict config models. Whole-scene intentional darkness and shallow-depth
softness remain limitations and default to preview-required recommendations.

- [ ] **Step 3: Implement bounded filters and side-effect measurements**

Luma filters cap output clipping; denoise strength has a small fixed range;
sharpening caps radius and amount. Produce before/after luma percentiles, noise
residual, sharpness, and clipped ratios. A worsening side-effect check becomes
`needs_review`; it is never overridden by a subjective quality score.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/rescue/test_visual.py tests/rescue/test_planner.py tests/rescue/test_commands.py -v
python scripts/validate.py
git add src/videoscope/rescue tests/rescue
git commit -m "feat: add bounded CPU visual improvements"
```

---

### Task 6: Flicker smoothing and bounded CPU stabilization

**Files:**
- Create: `src/videoscope/rescue/stabilization.py`
- Create: `tests/rescue/test_stabilization.py`
- Modify: `src/videoscope/rescue/visual.py`
- Modify: `src/videoscope/rescue/planner.py`
- Modify: `src/videoscope/rescue/executor.py`

**Interfaces:**
- Produces: `FlickerCorrectionPlan`, `MotionTransform`, `StabilizationAssessment`, `estimate_motion_transforms(...)`, `smooth_motion_transforms(...)`, and `render_stabilized_video(...)`.

- [ ] **Step 1: Write RED tests for scene guards and crop budget**

```python
def test_scene_cut_is_not_smoothed_as_flicker() -> None:
    plan = plan_flicker_correction(
        brightness_with_scene_cut(), scenes_with_cut(), flicker_config()
    )
    assert plan.intervals == ()


def test_stabilization_is_skipped_when_required_crop_exceeds_budget() -> None:
    result = assess_stabilization(
        large_shake_transforms(), stabilization_config(max_crop_ratio=0.08)
    )
    assert result.recommended is False
    assert result.reason == "crop_budget_exceeded"
```

- [ ] **Step 2: Implement flicker correction**

Remove low-frequency luma trend, exclude scene-boundary guards, and derive a
bounded multiplicative correction curve only for repeated high-frequency global
variation. Store the curve and affected ranges in the action parameters.

- [ ] **Step 3: Implement streaming stabilization**

Estimate affine partial transforms from downscaled grayscale frames using
feature tracking and RANSAC. Reject low-inlier, high-residual, scene-boundary,
and excessive-crop intervals. Smooth transforms deterministically; render frames
streamingly through a bounded queue and preserve audio with the existing command
runner. If reliable stabilization is unavailable, omit the action and explain
the fallback instead of failing the task.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/rescue/test_stabilization.py tests/rescue/test_visual.py -v
python scripts/validate.py
git add src/videoscope/rescue tests/rescue
git commit -m "feat: smooth flicker and stabilize bounded CPU video"
```

---

### Task 7: Audio assessment, loudness/noise improvement, and fixed sync correction

**Files:**
- Create: `src/videoscope/rescue/audio.py`
- Create: `tests/rescue/test_audio.py`
- Modify: `src/videoscope/rescue/planner.py`
- Modify: `src/videoscope/rescue/commands.py`
- Modify: `src/videoscope/rescue/executor.py`

**Interfaces:**
- Produces: `AudioAssessment`, `LoudnessConfig`, `AudioDenoiseConfig`, `FixedOffsetAssessment`, `assess_audio(...)`, `measure_fixed_av_offset(...)`, and deterministic FFmpeg filter arguments.

- [ ] **Step 1: Write audio RED tests**

```python
def test_low_loudness_proposes_normalization_with_peak_guard() -> None:
    assessment = assess_audio(low_loudness_measurements(), audio_config())
    assert assessment.recommended_actions == (RescueActionKind.NORMALIZE_AUDIO,)
    assert assessment.parameters["true_peak_limit_dbtp"] == -1.5


def test_unreliable_offset_is_not_corrected() -> None:
    assessment = measure_fixed_av_offset(
        ambiguous_audio_events(), ambiguous_video_events(), sync_config()
    )
    assert assessment.offset_seconds is None
    assert assessment.reason == "insufficient_correlation"
```

- [ ] **Step 2: Implement two-pass loudness and bounded denoise**

Use FFmpeg loudness measurement then apply the measured parameters with a true
peak guard. Detect clipping independently. Audio denoise requires a measured
noise floor and configured maximum reduction; isolated low-confidence noise
does not trigger processing.

- [ ] **Step 3: Implement fixed-offset correction only**

Estimate one constant offset from repeated high-confidence audio/video events.
Require minimum correlation, event count, and agreement. The action shifts audio
or video once and records the exact offset. Drift curves and guessed offsets are
out of scope and produce an explicit manual-review reason.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/rescue/test_audio.py tests/rescue/test_planner.py tests/rescue/test_commands.py -v
python scripts/validate.py
git add src/videoscope/rescue tests/rescue
git commit -m "feat: improve audio and correct reliable fixed offsets"
```

---

### Task 8: Verification gate, private review artifacts, and atomic public results

**Files:**
- Create: `src/videoscope/rescue/verification.py`
- Create: `src/videoscope/rescue/artifacts.py`
- Create: `tests/rescue/test_verification.py`
- Create: `tests/rescue/test_artifacts.py`
- Modify: `src/videoscope/rescue/executor.py`

**Interfaces:**
- Produces: `RescueVerifier.verify(source, faithful, improved, plan, mappings) -> RescueVerificationReport` and `RescueArtifactLayout.create(job_root) -> RescueArtifactLayout`.
- Produces: `publish_verified_rescue(...) -> tuple[RescueArtifact, ...]`, which publishes faithful and improved outputs independently.

- [ ] **Step 1: Write verification RED tests**

```python
def test_failed_improved_copy_does_not_invalidate_faithful_copy() -> None:
    report = RescueVerifier(probe=fake_probe()).verify(
        source=source_metadata(),
        faithful=valid_faithful_metadata(),
        improved=improved_with_new_clipping(),
        plan=balanced_plan(),
        mappings=source_mappings(),
    )
    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert report.improved_status is RescueVerificationStatus.NEEDS_REVIEW
    assert report.outcome is RescueOutcome.NEEDS_REVIEW
```

- [ ] **Step 2: Implement independent checks**

Check complete decode, streams, durations, source mapping, fixed-offset direction,
black/freeze/flicker regressions, luma clipping, denoise/sharpen side effects,
stabilization crop, audio loudness/peaks, artifact hashes, and relative paths.
Each check has one stable ID and measured values. Aggregate status follows failed,
needs-review, passed precedence without a global score.

- [ ] **Step 3: Implement private/public isolation and atomic publication**

Use exactly:

```text
rescue-review-private/
  damage-map-private.json
  previews/
  staging/
rescue-output/
  rescue-plan.json
  faithful-rescue.mp4
  improved-viewing.mp4          only when verified or explicitly needs-review
  damaged-segments.json
  changes.json
  verification-report.json
  technical-report.json
  report.html
```

Reject absolute paths, `..`, symlinks escaping the job root, unexpected files,
and private-root members in a public manifest. Publish from a sibling staging
directory with atomic rename. A partial result uses deterministic segment names
and a complete source map.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/rescue/test_verification.py tests/rescue/test_artifacts.py -v
python scripts/validate.py
git add src/videoscope/rescue tests/rescue
git commit -m "feat: verify and publish rescue artifacts safely"
```

---

### Task 9: Core pipeline, confirmation lifecycle, HTML report, and CLI

**Files:**
- Create: `src/videoscope/rescue/pipeline.py`
- Create: `src/videoscope/rescue/report.py`
- Create: `src/videoscope/reporting/templates/rescue_report.html.j2`
- Create: `tests/rescue/test_pipeline.py`
- Create: `tests/rescue/test_report.py`
- Modify: `src/videoscope/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `RescueConfig`, `RescueStatus`, `RescuePreparation`, `RescueResult`, and `VideoRescuePipeline.prepare(source)`, `.confirm(preparation, confirmation)`, `.execute(preparation, confirmation)`, and `.cancel()`.
- Produces CLI `videoscope rescue INPUT --output DIR --strategy conservative|balanced`, plus `--symptom`, `--locked-range`, `--preview-seconds`, `--confirm-plan`, `--keep-workspace`, and `--quiet`.

- [ ] **Step 1: Write lifecycle RED tests**

```python
def test_confirmation_is_bound_to_issued_plan_instance(tmp_path: Path) -> None:
    pipeline = VideoRescuePipeline(config=rescue_config(tmp_path), dependencies=fakes())
    preparation = pipeline.prepare(tmp_path / "input.mp4")
    altered = preparation.plan.model_copy(update={"plan_digest": "f" * 64})
    with pytest.raises(RescueConfirmationError):
        pipeline.execute(
            preparation=replace(preparation, plan=altered),
            confirmation=RescueConfirmation(
                plan_digest="f" * 64,
                publish_faithful=True,
                publish_improved=False,
                accepted_action_ids=(),
                accepted_trim_damage_ids=(),
            ),
        )


def test_balanced_without_supported_improvement_delivers_only_faithful(
    tmp_path: Path,
) -> None:
    result = run_pipeline_with_clean_input(tmp_path)
    assert result.faithful_path.is_file()
    assert result.improved_path is None
    assert "no supported improvement" in result.technical_report.limitations
```

- [ ] **Step 2: Implement orchestration and cancellation**

Use the issued canonical plan, source hash, immutable preparation, and constant-time
digest comparison. Progress stages are scanning, planning, previewing, awaiting
confirmation, processing, verifying, and terminal status. Single failures retain
verified independent artifacts but do not mark the overall job completed.

- [ ] **Step 3: Implement offline HTML report**

Render validated public models only. Include source summary without absolute path,
decodable/damaged timeline, selected actions, source mappings, independent output
statuses, measured before/after values, limitations, and download links. Escape
all content, load no remote assets, and never embed the source video by default.

- [ ] **Step 4: Implement CLI and exit codes**

Use exit code `0` for completed or user-accepted partial delivery, `2` for input or
config errors, `3` for unprocessable media without output, and `4` for internal
failure. An interactive terminal shows the plan digest and asks for confirmation;
non-interactive execution requires the exact `--confirm-plan` digest and never
implicitly accepts content changes.

- [ ] **Step 5: Verify and commit**

```powershell
python -m pytest tests/rescue/test_pipeline.py tests/rescue/test_report.py tests/test_cli.py -v
python scripts/validate.py
git add src/videoscope/rescue src/videoscope/reporting src/videoscope/cli.py tests
git commit -m "feat: add end-to-end Video Rescue pipeline and CLI"
```

---

### Task 10: Persisted local Web jobs, API, SSE, cancellation, and cleanup

**Files:**
- Create: `src/videoscope/web/rescue_jobs.py`
- Create: `tests/web/test_rescue_api.py`
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/app.py`

**Interfaces:**
- Produces: `RescueJobStatus`, `RescueJobEvent`, `RescueJobResponse`, `RescueJobManager`, and local routes under `/api/rescue`.
- Consumes: Task 9 `VideoRescuePipeline`; does not duplicate scanner, planner, executor, or verifier logic.

- [ ] **Step 1: Write API RED tests**

```python
def test_rescue_job_requires_exact_confirmation_digest(client, video_bytes) -> None:
    created = create_rescue_job(client, video_bytes)
    wait_for_status(client, created["job_id"], "awaiting_confirmation")
    response = client.post(
        f"/api/rescue/jobs/{created['job_id']}/confirm",
        json={
            "plan_digest": "0" * 64,
            "publish_faithful": True,
            "publish_improved": True,
            "accepted_action_ids": ["adjust_luma_v1"],
            "accepted_trim_damage_ids": [],
        },
    )
    assert response.status_code == 409


def test_rescue_artifact_rejects_path_traversal(client, completed_rescue_job) -> None:
    response = client.get(
        f"/api/rescue/jobs/{completed_rescue_job}/artifacts/%2e%2e/source.mp4"
    )
    assert response.status_code in {400, 404}
```

- [ ] **Step 2: Implement persisted state machine**

Persist path-free state by atomic JSON, recover jobs after restart, and restore
only valid lifecycle transitions. Use random job IDs, bounded upload streaming,
the existing CPU semaphore, explicit cancellation callbacks, TTL cleanup, and
private/public artifact resolvers. Deleting a job removes its exact validated job
root and no ancestor.

- [ ] **Step 3: Add local API endpoints**

```text
POST   /api/rescue/jobs
GET    /api/rescue/jobs/{job_id}
GET    /api/rescue/jobs/{job_id}/events
GET    /api/rescue/jobs/{job_id}/damage-map
GET    /api/rescue/jobs/{job_id}/plan
POST   /api/rescue/jobs/{job_id}/confirm
GET    /api/rescue/jobs/{job_id}/artifacts/{path}
GET    /api/rescue/jobs/{job_id}/private-artifacts/{path}
DELETE /api/rescue/jobs/{job_id}
```

Private preview routes are loopback-only and never listed in the public result
manifest. Error bodies are sanitized and CORS remains non-wildcard by default.

- [ ] **Step 4: Verify and commit**

```powershell
python -m pytest tests/web/test_rescue_api.py -v
python scripts/validate.py
git add src/videoscope/web tests/web
git commit -m "feat: add local Video Rescue web jobs"
```

---

### Task 11: Bilingual Rescue workbench and synchronized preview comparison

**Files:**
- Create: `web/src/rescueI18n.ts`
- Create: `web/src/rescueI18n.test.ts`
- Create: `web/src/hooks/useRescueLifecycle.ts`
- Create: `web/src/hooks/useRescueLifecycle.test.tsx`
- Create: `web/src/components/RescueView.tsx`
- Create: `web/src/components/RescueSymptomSelector.tsx`
- Create: `web/src/components/RescueDamageTimeline.tsx`
- Create: `web/src/components/RescuePreviewComparison.tsx`
- Create: `web/src/components/RescuePlanReview.tsx`
- Create: `web/src/components/RescueResult.tsx`
- Create: `web/src/components/RescueView.test.tsx`
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/App.test.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**
- Produces typed Rescue API methods, resumable lifecycle hook, and a new `rescue` workbench mode.
- Consumes: Task 10 API and Task 1 public schemas; no media analysis is reimplemented in TypeScript.

- [ ] **Step 1: Write API and lifecycle RED tests**

Assert form encoding, plan/damage fetch, digest confirmation, SSE event ordering,
reconnect using last sequence, cancellation, refresh recovery, explicit delete,
and artifact URL encoding.

- [ ] **Step 2: Write workbench RED tests**

```tsx
it("keeps the what912 mark invariant while switching language", async () => {
  render(<RescueView initialLocale="en" api={fakeRescueApi()} />);
  expect(screen.getByText("what912")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "切换到简体中文" }));
  expect(screen.getByText("what912")).toBeVisible();
  expect(screen.getByText("视频抢救")).toBeVisible();
});


it("does not present an unsupported improved copy", async () => {
  render(<RescueView api={fakeRescueApi({ improved_artifact: null })} />);
  expect(screen.getByRole("link", { name: /download faithful/i })).toBeVisible();
  expect(screen.queryByRole("link", { name: /download improved/i })).toBeNull();
});
```

- [ ] **Step 3: Implement the complete responsive flow**

Support symptom selection, local-processing disclosure, scan progress, damage
timeline, recoverable-time explanation, strategy comparison, original/faithful/
improved synchronized previews, per-action enable/disable, bounded strength
controls, locked ranges, plan confirmation, progress, result comparison, reports,
new task, cancellation, and deletion. Mobile uses a bottom sheet; advanced
controls are collapsed; focus states, keyboard seeking, reduced motion, text plus
color status, and Simplified Chinese/English are required.

- [ ] **Step 4: Build and synchronize packaged assets**

```powershell
cd web
npm test
npm run build
cd ..
git status --porcelain --untracked-files=all -- src/videoscope/web/static
```

Expected: tests and production build pass; every intentional hashed asset
replacement is staged, and a clean checkout rebuild produces no unexplained drift.

- [ ] **Step 5: Commit**

```powershell
git add web src/videoscope/web/static
git commit -m "feat: add bilingual Video Rescue workbench"
```

---

### Task 12: Real end-to-end fixture acceptance and performance bounds

**Files:**
- Create: `tests/rescue/test_fixture_rescue.py`
- Create: `tests/rescue/test_performance.py`
- Modify: `tests/test_fixture_factory.py`
- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`

**Interfaces:**
- Consumes: Tasks 2–11 public workflows and deterministic fixture manifest.
- Produces: real FFmpeg acceptance evidence for faithful, improved, partial, and clean no-op cases.

- [ ] **Step 1: Write real E2E RED tests**

```python
def test_real_dark_noisy_fixture_delivers_both_verified_outputs(
    rescue_dark_noise: Path,
    tmp_path: Path,
) -> None:
    source_hash = sha256_file(rescue_dark_noise)
    result = run_confirmed_balanced_rescue(rescue_dark_noise, tmp_path)
    assert result.faithful_path.is_file()
    assert result.improved_path is not None and result.improved_path.is_file()
    assert result.verification.faithful_status is VerificationStatus.PASSED
    assert result.verification.improved_status is VerificationStatus.PASSED
    assert sha256_file(rescue_dark_noise) == source_hash
    assert_improvement_within_manifest_bounds(result, "rescue_dark_noise.mp4")
```

- [ ] **Step 2: Cover every contract outcome**

Assert `completed` for supported clean/structural paths, `partial` for recoverable
middle damage, `needs_review` for injected side effects, `failed` for zero safe
output, and `cancelled` for interrupted execution. Compare numerical metrics and
time intervals to manifest tolerances; never assert only that results are non-empty.

- [ ] **Step 3: Add bounded-resource tests**

Instrument decoded-frame queues and command invocations. Assert frames are
streamed once per stage, preview duration never exceeds ten seconds, memory use is
bounded by configured queues rather than video duration, and shared frame samples
are reused across compatible assessments. Performance tests record conditions but
do not claim universal speed.

- [ ] **Step 4: Verify and commit**

```powershell
python scripts/generate_test_videos.py --force
python -m pytest tests/rescue/test_fixture_rescue.py tests/rescue/test_performance.py -v
python scripts/validate.py
git add scripts/generate_test_videos.py tests/fixtures/manifest.json tests/rescue tests/test_fixture_factory.py
git commit -m "test: verify real Video Rescue outcomes"
```

---

### Task 13: Documentation, packaging, CI, smoke testing, and release audit

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `SECURITY.md`
- Modify: `docs/release-checklist.md`
- Modify: `release-audit.md`
- Modify: `pyproject.toml`
- Modify: `CITATION.cff`
- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/audit_distribution.py`
- Modify: `scripts/smoke_test.py`
- Create: `docs/video-rescue-guide.md`
- Create: `examples/video_rescue.ps1`
- Create: `examples/video_rescue.sh`
- Create: `examples/rescue-config.example.json`

**Interfaces:**
- Produces documented CLI/Web workflows, archive audit, clean-wheel smoke path, and a v0.5 development-line release audit.
- Consumes every prior task.

- [ ] **Step 1: Write distribution and smoke RED tests**

Require Rescue modules, guide, examples, current dashboard assets, and JSON schema.
Forbid generated videos, private review roots, rescue outputs, workspaces, caches,
personal paths, and fixture corruption intermediates. Extend clean-wheel smoke to
generate a small local fixture, run Conservative and Balanced confirmed flows,
and validate independent faithful/improved outputs.

- [ ] **Step 2: Update versions and user documentation**

Move the development line to Python `0.5.0.dev0`, CFF `0.5.0-dev0`, and npm
`0.5.0-dev.0`. Document installation, FFmpeg, symptoms, strategy differences,
preview confirmation, output files, partial recovery, limitations, privacy,
deletion, JSON, local Web use, and the fact that filtering cannot recreate lost
source information. Do not claim measured outcomes that were not actually run.

- [ ] **Step 3: Extend CI and archive gates**

On Linux and Windows with Python 3.11 and 3.12: install FFmpeg, generate fixtures,
run `scripts/validate.py`, real Rescue E2E, frontend tests/build, static drift
gate, wheel/sdist build, archive audit, and clean-wheel smoke. Base jobs do not
install AI/OCR extras or download models.

- [ ] **Step 4: Run full local release verification**

```powershell
python scripts/generate_test_videos.py --force
python scripts/validate.py
cd web
npm test
npm run build
cd ..
python -m build
python scripts/audit_distribution.py dist
$wheel = (Get-ChildItem dist\*.whl | Select-Object -First 1).FullName
python scripts/smoke_test.py --wheel $wheel
git status --short
```

Expected: Python validation, frontend, build, archive audit, and clean-wheel
smoke pass. If an uncached build dependency requires network, record the exact
local blocker and keep the packaging CI gate rather than weaken the smoke test.

- [ ] **Step 5: Complete manual acceptance and release audit**

Test representative real phone, camera, screen-recording, meeting, and abnormal
export files. Verify actual players on Windows plus documented Linux/macOS gates,
English/Chinese, keyboard, mobile, refresh, cancellation, explicit deletion,
source hash preservation, no console errors, and invariant `what912`. Separate
passed, human-review, unverified, risk, and blocker sections in `release-audit.md`.

- [ ] **Step 6: Commit local release preparation**

```powershell
git add .
git commit -m "release: prepare Video Rescue Balanced CPU MVP"
```

Do not push, tag, open a Pull Request, publish PyPI, create a GitHub Release, or
deploy without a separate explicit authorization.

---

## Plan completion criteria

- All 13 tasks have independent implementer reports and scoped reviews.
- Every production behavior was preceded by a focused failing test and recorded RED result.
- Conservative and Balanced are complete end-to-end; Aggressive AI remains absent.
- Real fixture tests prove playable faithful output, measurable bounded improvements, partial salvage, and source hash preservation.
- Unsupported or harmful improvements are omitted or marked `needs_review`; they never masquerade as completed.
- All public artifacts are path-safe, deterministic, source-traceable, and separate from private previews and staging files.
- Full Python, frontend, build, archive, and clean-wheel smoke gates pass before completion is claimed.
- A broad final review reports no unresolved Critical or Important findings.
- The branch remains local unless the user explicitly authorizes integration or publication.
