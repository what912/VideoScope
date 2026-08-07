# VideoScope Publish Ready Native MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native-local Publish Ready workflow that turns one local video into a newly written, verified MP4 for one of three versioned output profiles, with preview, explicit plan confirmation, change records, CLI and local Web support.

**Architecture:** Keep the existing `AnalysisPipeline` and `AnalysisReport` unchanged as the diagnostic fact source. Add a focused `videoscope.resolve` package containing versioned contracts, publish profiles, a deterministic planner, a shell-free FFmpeg executor, output verification, and an orchestration pipeline; expose it through a new CLI command and a separate publish job manager in the existing loopback FastAPI service. This plan implements `native_local` only; browser transcoding and any remote backend require separate designs after this contract is proven.

**Tech Stack:** Python 3.11+, Pydantic 2, Typer, FFmpeg/ffprobe as external executables, FastAPI, React 19, TypeScript 5.9, Vite, Vitest, pytest, Ruff, mypy.

## Global Constraints

- Read `AGENTS.md`, `docs/product-spec.md`, `docs/architecture.md`, `docs/roadmap.md`, `docs/report-schema.md`, and `docs/superpowers/specs/2026-08-01-videoscope-dual-track-product-direction-design.md` before each task.
- Preserve every v0.1 `AnalysisReport` field and the existing `videoscope analyze` behavior.
- Treat Publish Ready as the v0.3 development line. Do not relabel the existing v0.1 or v0.2 behavior; use package version `0.3.0.dev0` until a separate release audit authorizes an RC.
- Source videos are read-only. Every successful publish task writes a new file named `publish-ready.mp4` under a separate output directory.
- The native MVP exposes exactly three profile IDs: `compatible_mp4`, `social_vertical_9_16`, and `social_horizontal_16_9`.
- A profile pass is a versioned compatibility result, not an overall quality score or an artistic judgment.
- The MVP uses no remote backend, network API, GPU, AI model, model download, face recognition, identity recognition, automatic crop, clip deletion, interpolation, stabilization, music, or generative enhancement.
- The vertical and horizontal profiles preserve the complete source image through scale-and-pad. They do not crop content.
- All FFmpeg and ffprobe calls use argument arrays with `shell=False`; paths are never interpolated into command strings.
- All public JSON paths are relative to the task output root and use forward slashes.
- Tests are offline, CPU-only, deterministic, and cover paths containing spaces, Chinese, and non-ASCII characters.
- A failed output verification must produce `needs_review` or `failed`, never `completed` or “publish ready.”
- Do not add FFmpeg binaries or WASM builds to the repository or distributions.
- Commit commands below are review checkpoints only. Execute each one only when the user has explicitly authorized implementation commits.
- After each task run its focused tests. After every modification run `\.venv\Scripts\python.exe scripts\validate.py`; after front-end changes also run `npm test` and `npm run build` in `web/`.

---

## File Map

### Product contract and documentation

- Modify: `docs/product-spec.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`
- Add: `docs/publish-ready.md`
- Add: `docs/resolve-schema.md`

### Resolve domain and serialization

- Add: `src/videoscope/resolve/__init__.py`
- Add: `src/videoscope/resolve/models.py`
- Add: `src/videoscope/resolve/serialization.py`
- Add: `src/videoscope/resolve/errors.py`
- Test: `tests/resolve/__init__.py`
- Test: `tests/resolve/test_models.py`
- Test: `tests/resolve/test_serialization.py`

### Profiles and planning

- Add: `src/videoscope/resolve/profiles.py`
- Add: `src/videoscope/resolve/planner.py`
- Modify: `src/videoscope/video/probe.py`
- Test: `tests/resolve/test_profiles.py`
- Test: `tests/resolve/test_planner.py`
- Modify: `tests/video/test_probe.py`

### Native execution and previews

- Add: `src/videoscope/resolve/commands.py`
- Add: `src/videoscope/resolve/executor.py`
- Test: `tests/resolve/test_commands.py`
- Test: `tests/resolve/test_executor.py`

### Verification and orchestration

- Add: `src/videoscope/resolve/verification.py`
- Add: `src/videoscope/resolve/pipeline.py`
- Test: `tests/resolve/test_verification.py`
- Test: `tests/resolve/test_pipeline.py`

### CLI

- Modify: `src/videoscope/cli.py`
- Modify: `tests/test_cli.py`

### Local Web API

- Add: `src/videoscope/web/storage.py`
- Add: `src/videoscope/web/publish_jobs.py`
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/jobs.py`
- Modify: `src/videoscope/web/app.py`
- Test: `tests/web/test_storage.py`
- Test: `tests/web/test_publish_api.py`
- Modify: `tests/web/test_api.py`

### Packaged React workbench

- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/api.test.ts`
- Modify: `web/src/App.tsx`
- Add: `web/src/components/PublishReadyView.tsx`
- Add: `web/src/components/PublishReadyView.test.tsx`
- Add: `web/src/components/PublishProfileSelector.tsx`
- Add: `web/src/components/PublishPlanReview.tsx`
- Add: `web/src/components/PublishPreview.tsx`
- Add: `web/src/components/PublishResult.tsx`
- Modify: `web/src/styles.css`

### Fixtures, packaging, and release-facing docs

- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`
- Add: `tests/resolve/test_fixture_publish.py`
- Modify: `scripts/smoke_test.py`
- Modify: `pyproject.toml`
- Modify: `src/videoscope/__init__.py`
- Modify: `CITATION.cff`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/web-api.md`
- Modify: `docs/release-checklist.md`
- Modify: `.github/workflows/ci.yml`

## Task 1: Change the formal product boundary without rewriting v0.1

**Files:**

- Modify: `docs/product-spec.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`
- Add: `docs/publish-ready.md`
- Add: `docs/resolve-schema.md`

**Interfaces:**

- Consumes: the approved dual-track design specification.
- Produces: the normative scope, lifecycle, exit-code, artifact, and privacy rules used by every later task.

- [ ] **Step 1: Add the version boundary to the product specification**

Keep the existing v0.1 section intact. Add a v0.3 development section containing these exact boundaries:

```text
VideoScope Resolve is an opt-in processing workflow built on VideoScope Check.
It never changes the v0.1 analysis contract, never overwrites the source, and
does not make processing dependencies part of the base diagnostic path.
```

State that A MVP includes only compatible MP4, 9:16 scale-and-pad, 16:9 scale-and-pad, metadata stripping, fast-start layout, a representative cover, preview, change records, and post-output verification.

- [ ] **Step 2: Define architecture and versioned artifacts**

Add the data flow:

```text
input -> Check baseline -> PublishPlan -> confirmation -> native FFmpeg
      -> output Check -> VerificationReport -> artifact publication
```

Define `plan.json`, `preview/publish-preview.mp4`, `publish-ready.mp4`, `cover.jpg`, `changes.json`, `technical-report.json`, `analysis-before/report.json`, and `analysis-after/report.json`. JSON reports reference only relative paths.

- [ ] **Step 3: Define CLI and lifecycle behavior**

Document these publish exit codes:

```text
0   output exists and verification passed
2   input, profile, configuration, or confirmation error
3   FFmpeg/ffprobe could not process the media
4   internal orchestration or artifact failure
5   output exists but verification requires human review
130 user cancellation
```

Document `created -> inspecting -> planning -> awaiting_confirmation -> processing -> verifying -> completed|needs_review|failed|cancelled`.

- [ ] **Step 4: Run documentation checks**

Run:

```powershell
rg -n "v0.1|Publish Ready|compatible_mp4|needs_review|source video|源视频|总质量分" docs/product-spec.md docs/architecture.md docs/roadmap.md docs/publish-ready.md docs/resolve-schema.md
\.venv\Scripts\python.exe scripts\validate.py
```

Expected: the new scope is explicitly later than v0.1, source overwrite is forbidden, and the repository validation passes.

- [ ] **Step 5: Record the review checkpoint**

```powershell
git add docs/product-spec.md docs/architecture.md docs/roadmap.md docs/publish-ready.md docs/resolve-schema.md
git commit -m "docs: define Publish Ready processing contract"
```

## Task 2: Add strict Resolve domain models and deterministic serialization

**Files:**

- Add: `src/videoscope/resolve/__init__.py`
- Add: `src/videoscope/resolve/models.py`
- Add: `src/videoscope/resolve/serialization.py`
- Add: `src/videoscope/resolve/errors.py`
- Test: `tests/resolve/__init__.py`
- Test: `tests/resolve/test_models.py`
- Test: `tests/resolve/test_serialization.py`

**Interfaces:**

- Consumes: `videoscope.domain.VideoMetadata` and JSON-compatible Pydantic values.
- Produces: `PublishProfileId`, `PublishBackend`, `PublishAction`, `PublishPlan`, `PublishArtifact`, `VerificationCheck`, `VerificationReport`, `PublishChangeLog`, `PublishTechnicalReport`, `make_publish_plan_digest()`, and UTF-8 read/write helpers.

- [ ] **Step 1: Write failing model validation tests**

Create tests for strict extra-field rejection, invalid SHA-256, absolute artifact paths, parent traversal, duplicate action IDs, reversed ordering, invalid verification status, Chinese descriptions, round trips, and deterministic plan digests.

```python
def test_plan_digest_is_deterministic(
    sample_actions: tuple[PublishAction, ...],
) -> None:
    first = make_publish_plan_digest(
        input_hash="a" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=sample_actions,
        output_filename="publish-ready.mp4",
    )
    second = make_publish_plan_digest(
        input_hash="a" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=sample_actions,
        output_filename="publish-ready.mp4",
    )
    assert first == second
    assert len(first) == 64
```

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_models.py tests/resolve/test_serialization.py -v
```

Expected: collection fails because `videoscope.resolve` does not exist.

- [ ] **Step 3: Implement the strict public models**

Use `ConfigDict(extra="forbid")` throughout. Define the stable enums exactly:

```python
class PublishProfileId(StrEnum):
    COMPATIBLE_MP4 = "compatible_mp4"
    SOCIAL_VERTICAL = "social_vertical_9_16"
    SOCIAL_HORIZONTAL = "social_horizontal_16_9"


class PublishBackend(StrEnum):
    NATIVE_LOCAL = "native_local"


class PublishActionKind(StrEnum):
    REMUX = "remux"
    TRANSCODE = "transcode"
    SCALE_PAD = "scale_pad"
    STRIP_METADATA = "strip_metadata"
    FASTSTART = "faststart"
    EXTRACT_COVER = "extract_cover"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
```

`PublishAction` contains `action_id`, `kind`, `description`, `parameters`, `affects`, `changes_content_semantics`, and `confirmation_required`. `PublishPlan` contains the source hash, normalized source metadata, profile ID/version, backend, ordered actions, output filename, and verified digest. Reject any action with `changes_content_semantics=True` because this MVP has no such action.

`PublishArtifact.relative_path` must pass this validator:

```python
path = PurePosixPath(value)
if path.is_absolute() or ".." in path.parts or value != path.as_posix():
    raise ValueError("artifact path must be a normalized relative POSIX path")
```

- [ ] **Step 4: Implement canonical JSON and atomic writers**

Use `model_dump(mode="json")`, `json.dumps(..., ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)`, UTF-8, `\n`, a same-directory temporary file, and `Path.replace()`. Provide distinct helpers for plan, change log, and technical report.

- [ ] **Step 5: Run focused and full validation**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_models.py tests/resolve/test_serialization.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

Expected: all tests pass and no existing report schema test changes.

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add src/videoscope/resolve tests/resolve
git commit -m "feat: add Publish Ready domain contracts"
```

## Task 3: Add versioned profiles and a deterministic safe-action planner

**Files:**

- Add: `src/videoscope/resolve/profiles.py`
- Add: `src/videoscope/resolve/planner.py`
- Modify: `src/videoscope/video/probe.py`
- Test: `tests/resolve/test_profiles.py`
- Test: `tests/resolve/test_planner.py`
- Modify: `tests/video/test_probe.py`

**Interfaces:**

- Consumes: `VideoMetadata`, `PublishProfileId`, `PublishBackend`.
- Produces: `PublishProfile`, `list_publish_profiles()`, `get_publish_profile()`, and `build_publish_plan(metadata, input_hash, profile_id) -> PublishPlan`.

- [ ] **Step 1: Write failing profile and planner tests**

Test stable profile order, duplicate rejection, unknown profile, compatible remux, codec-incompatible transcode, vertical scale-and-pad, horizontal scale-and-pad, high frame-rate limiting, audio/no-audio parameters, fixed action order, source filename exclusion, and deterministic digest.

```python
def test_vertical_plan_preserves_content_with_scale_and_pad(
    landscape_metadata: VideoMetadata,
) -> None:
    plan = build_publish_plan(
        landscape_metadata,
        input_hash="a" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
    )
    scale = next(action for action in plan.actions if action.kind == "scale_pad")
    assert scale.parameters == {
        "width": 1080,
        "height": 1920,
        "mode": "fit",
        "pad_color": "black",
    }
    assert scale.changes_content_semantics is False
```

- [ ] **Step 2: Extend the sanitized probe summary**

Add `audio_codec` from the selected audio stream to `VideoMetadata.raw_probe`. Keep the public `VideoMetadata` schema unchanged and never copy tags, paths, GPS, title, author, or complete ffprobe JSON.

```python
audio_stream = next(
    (stream for stream in streams if stream.get("codec_type") == "audio"),
    None,
)
if audio_stream is not None and audio_stream.get("codec_name"):
    raw_probe["audio_codec"] = str(audio_stream["codec_name"])
```

- [ ] **Step 3: Define the three built-in profiles**

Use immutable Pydantic models and these exact v1 values:

```python
COMPATIBLE_MP4 = PublishProfile(
    id=PublishProfileId.COMPATIBLE_MP4,
    version="1.0.0",
    width=None,
    height=None,
    maximum_fps=60.0,
    video_codec="h264",
    audio_codec="aac",
    pixel_format="yuv420p",
    container="mp4",
)

SOCIAL_VERTICAL = COMPATIBLE_MP4.model_copy(
    update={
        "id": PublishProfileId.SOCIAL_VERTICAL,
        "width": 1080,
        "height": 1920,
    }
)

SOCIAL_HORIZONTAL = COMPATIBLE_MP4.model_copy(
    update={
        "id": PublishProfileId.SOCIAL_HORIZONTAL,
        "width": 1920,
        "height": 1080,
    }
)
```

- [ ] **Step 4: Implement fixed-order planning**

For `compatible_mp4`, choose `REMUX` only when the container includes MP4, video codec is H.264, audio is absent or AAC, pixel format is yuv420p, and FPS is at most 60. Otherwise choose `TRANSCODE`. Vertical and horizontal always choose `TRANSCODE` plus `SCALE_PAD`. Append `STRIP_METADATA`, `FASTSTART`, and `EXTRACT_COVER` in that order. No action requires content-semantic confirmation.

- [ ] **Step 5: Verify**

```powershell
\.venv\Scripts\python.exe -m pytest tests/video/test_probe.py tests/resolve/test_profiles.py tests/resolve/test_planner.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add src/videoscope/video/probe.py src/videoscope/resolve/profiles.py src/videoscope/resolve/planner.py tests/video/test_probe.py tests/resolve
git commit -m "feat: plan safe Publish Ready transformations"
```

## Task 4: Build shell-free FFmpeg commands and the native executor

**Files:**

- Add: `src/videoscope/resolve/commands.py`
- Add: `src/videoscope/resolve/executor.py`
- Modify: `src/videoscope/resolve/errors.py`
- Test: `tests/resolve/test_commands.py`
- Test: `tests/resolve/test_executor.py`

**Interfaces:**

- Consumes: `PublishPlan`, source path, task work directory, FFmpeg executable name, cancellation callback.
- Produces: `build_publish_arguments()`, `build_preview_arguments()`, `build_cover_arguments()`, `ExternalCommandRunner`, and `NativePublishExecutor`.

- [ ] **Step 1: Write failing command-array tests**

Assert exact arrays, including paths with spaces and Chinese characters. Assert no string command, no shell metacharacter interpretation, optional audio mapping, no-audio behavior, scale/pad filter, fast-start, metadata removal, preview time bounds, and cover extraction.

```python
assert arguments[:4] == ("ffmpeg", "-hide_banner", "-nostdin", "-y")
assert ("-map", "0:v:0") == arguments[arguments.index("-map") :][:2]
assert "-map_metadata" in arguments
assert "-1" in arguments
assert str(source) in arguments
assert str(partial_output) == arguments[-1]
```

- [ ] **Step 2: Run and confirm failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_commands.py tests/resolve/test_executor.py -v
```

- [ ] **Step 3: Implement command construction**

For transcode, use H.264/AAC and the selected canvas:

```text
-map 0:v:0 -map 0:a:0? -c:v libx264 -preset medium -crf 20
-pix_fmt yuv420p -c:a aac -b:a 192k -map_metadata -1
-movflags +faststart
```

When a canvas is present, add one filter string created only from validated integers:

```text
scale=w=1080:h=1920:force_original_aspect_ratio=decrease,
pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1
```

Join the filter into one argument without a shell. For remux, use `-c copy`, metadata stripping, and fast-start. If source FPS exceeds 60, append `fps=60` to the video filter chain; do not change lower frame rates.

- [ ] **Step 4: Implement the bounded external runner**

Define:

```python
class ExternalCommandRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult: ...
```

The production runner calls `subprocess.run(list(arguments), shell=False, check=False, capture_output=True, encoding="utf-8", errors="replace", timeout=...)`. Sanitize bounded stderr with `sanitize_diagnostic()`.

- [ ] **Step 5: Implement atomic execution and cleanup**

`NativePublishExecutor` writes `publish-ready.partial.mp4`, checks cancellation before each command, deletes partial files on failure, verifies a non-empty output exists, and uses `Path.replace()` to publish `publish-ready.mp4`. Reject source and output paths that resolve to the same file. Generate a six-second preview centered on the source midpoint and one midpoint JPEG cover. Do not catch `KeyboardInterrupt` or `SystemExit`.

- [ ] **Step 6: Verify**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_commands.py tests/resolve/test_executor.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add src/videoscope/resolve tests/resolve
git commit -m "feat: execute native Publish Ready transforms"
```

## Task 5: Verify output compatibility and detector regressions

**Files:**

- Add: `src/videoscope/resolve/verification.py`
- Test: `tests/resolve/test_verification.py`

**Interfaces:**

- Consumes: source and output `VideoMetadata`, selected `PublishProfile`, source and output `AnalysisReport`.
- Produces: `PublishVerifier.verify(...) -> VerificationReport`.

- [ ] **Step 1: Write failing verification tests**

Cover valid output, wrong dimensions, wrong codec, excessive FPS, missing expected audio, duration drift, undecodable output, new high near-black, new high possible-freeze, pre-existing finding, detector error, and deterministic check order.

```python
report = verifier.verify(
    source_metadata=source_metadata,
    output_metadata=output_metadata,
    profile=SOCIAL_VERTICAL,
    before=before_report,
    after=after_report,
)
assert [check.check_id for check in report.checks] == [
    "decodable",
    "duration",
    "dimensions",
    "container",
    "video_codec",
    "pixel_format",
    "frame_rate",
    "audio_stream",
    "audio_codec",
    "near_black_regression",
    "possible_freeze_regression",
]
```

- [ ] **Step 2: Run and confirm failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_verification.py -v
```

- [ ] **Step 3: Implement technical checks**

Use `max(0.5, 2 / max(source_fps, 1.0))` seconds as duration tolerance. Exact canvas dimensions are required for vertical/horizontal; compatible MP4 preserves source dimensions. Require an MP4 container, H.264 video, yuv420p pixels, FPS at most 60, and an AAC audio stream when the source had audio. Audio absence on a silent source passes. Read pixel and audio codec only from the sanitized `raw_probe` summary added in Task 3.

- [ ] **Step 4: Implement detector regression checks**

Compare `near_black` and `possible_freeze` high/critical event count and total duration. A post-output increase is `needs_review`; a detector error is also `needs_review`, not a pass. These detector-local comparisons must not be combined into a score.

```python
def severe_summary(report: AnalysisReport, detector_id: str) -> tuple[int, float]:
    matches = [
        item
        for item in report.findings
        if item.detector_id == detector_id
        and item.severity in {Severity.HIGH, Severity.CRITICAL}
    ]
    return len(matches), sum(
        item.time_range.end_seconds - item.time_range.start_seconds for item in matches
    )
```

- [ ] **Step 5: Derive the final verification status**

Any failed technical check yields `FAILED`. Detector regression or incomplete detector execution yields `NEEDS_REVIEW`. All checks passing yields `PASSED`. Store plain-language messages and measured values without absolute paths.

- [ ] **Step 6: Verify**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_verification.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add src/videoscope/resolve/verification.py tests/resolve/test_verification.py
git commit -m "feat: verify Publish Ready outputs"
```

## Task 6: Orchestrate preparation, confirmation, processing, and atomic artifacts

**Files:**

- Add: `src/videoscope/resolve/pipeline.py`
- Modify: `src/videoscope/resolve/__init__.py`
- Test: `tests/resolve/test_pipeline.py`

**Interfaces:**

- Consumes: `PublishReadyConfig`, `AnalysisPipeline` factory, planner, native executor, verifier, progress callback, cancellation callback.
- Produces: `PublishReadyPipeline.prepare(input_path) -> PublishPreparation` and `PublishReadyPipeline.execute(preparation, confirmed_plan_digest) -> PublishResult`.

- [ ] **Step 1: Write failing orchestration tests with fakes**

Cover missing input, Chinese paths, prepare without mutation, preview creation, digest mismatch, source immutability, output directory collision, successful artifact set, verification `needs_review`, analysis failure, executor failure, cancellation, workspace cleanup, keep-workspace, and no absolute path in JSON.

```python
preparation = pipeline.prepare(input_path)
assert preparation.plan.profile_id == PublishProfileId.COMPATIBLE_MP4
assert input_path.read_bytes() == original_bytes

result = pipeline.execute(
    preparation,
    confirmed_plan_digest=preparation.plan.plan_digest,
)
assert result.video_path == output / "publish-ready.mp4"
assert result.technical_report.verification.status == VerificationStatus.PASSED
```

- [ ] **Step 2: Run and confirm failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_pipeline.py -v
```

- [ ] **Step 3: Implement configuration and internal preparation state**

Use this public config:

```python
class PublishReadyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: PublishProfileId
    output_directory: Path = Path("videoscope-publish-output")
    preview_seconds: float = Field(default=6.0, gt=0, le=10)
    keep_workspace: bool = False
    run_diagnostics: bool = True
```

Keep paths only in an internal frozen `PublishPreparation` dataclass. The serialized `plan.json` contains no workspace or source path.

- [ ] **Step 4: Implement prepare**

Validate the input, create a task workspace, compute the source SHA-256, run a baseline `AnalysisPipeline` into `analysis-before/`, build the plan from the returned metadata, generate the preview, write `plan.json`, and emit stage callbacks. A failure cleans unpublished work unless `keep_workspace` is true.

- [ ] **Step 5: Implement execute and publication**

Reject a missing or mismatched digest before FFmpeg runs. Execute into a staging directory, probe and analyze the output into `analysis-after/`, verify it, hash each public artifact, write `changes.json` and `technical-report.json`, then atomically publish the complete directory. If verification is `FAILED`, do not expose `publish-ready.mp4` as a final artifact. If verification is `NEEDS_REVIEW`, keep the output and mark it clearly.

- [ ] **Step 6: Verify**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_pipeline.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add src/videoscope/resolve tests/resolve/test_pipeline.py
git commit -m "feat: add Publish Ready orchestration"
```

## Task 7: Add the `videoscope publish` CLI

**Files:**

- Modify: `src/videoscope/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: `PublishReadyConfig`, `PublishReadyPipeline`, `PublishProfileId`.
- Produces: `videoscope publish INPUT --profile PROFILE --output DIRECTORY`.

- [ ] **Step 1: Write failing CLI tests**

Cover help, all profile values, plan display, interactive confirmation, non-interactive confirmation refusal, `--yes`, `--preview-only`, quiet mode, missing input, FFmpeg failure, `needs_review` exit 5, cancellation exit 130, and filenames with spaces/Chinese.

```python
result = runner.invoke(
    app,
    [
        "publish",
        str(input_path),
        "--profile",
        "social_vertical_9_16",
        "--output",
        str(output),
        "--yes",
    ],
)
assert result.exit_code == 0
assert (output / "publish-ready.mp4").is_file()
```

- [ ] **Step 2: Run and confirm failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/test_cli.py -k publish -v
```

- [ ] **Step 3: Implement the command and confirmation rules**

Options:

```text
--profile compatible_mp4|social_vertical_9_16|social_horizontal_16_9
--output PATH
--preview-only
--yes
--keep-workspace
--quiet
```

Always run `prepare()` first and print profile, backend, ordered actions, output filename, and preview path unless quiet. `--preview-only` exits 0 after preparation. Without `--yes`, require an interactive TTY and call `typer.confirm("Process the full video with this plan?")`; non-interactive invocations exit 2 with instructions to review the plan and pass `--yes`.

- [ ] **Step 4: Map errors and outcome status**

Map configuration/confirmation to 2, FFmpeg/ffprobe processing to 3, internal errors to 4, `needs_review` to 5, and interruption to 130. Quality Findings alone do not change the exit code; only publish verification does.

- [ ] **Step 5: Verify**

```powershell
\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add src/videoscope/cli.py tests/test_cli.py
git commit -m "feat: add Publish Ready CLI"
```

## Task 8: Extract safe local job storage without breaking analysis jobs

**Files:**

- Add: `src/videoscope/web/storage.py`
- Modify: `src/videoscope/web/jobs.py`
- Test: `tests/web/test_storage.py`
- Modify: `tests/web/test_api.py`

**Interfaces:**

- Consumes: configured job root and random 32-character lowercase hex job IDs.
- Produces: `LocalJobStore.reserve()`, `require_directory()`, `resolve_artifact()`, `discard()`, and `cleanup_orphans()` used by analysis and publish managers.

- [ ] **Step 1: Write failing storage tests**

Test random IDs, directory containment, normalized upload suffix, path traversal rejection, symlink escape where supported, terminal cleanup, orphan cleanup, Chinese root path, and concurrent reservations.

- [ ] **Step 2: Run and confirm failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/web/test_storage.py -v
```

- [ ] **Step 3: Implement `LocalJobStore`**

Use `Path.resolve(strict=False)` and `Path.relative_to(root)` containment checks. The public upload filename never becomes a path; only a validated lowercase suffix is retained for `input.<suffix>`. Artifact access requires a completed job root and rejects absolute paths, empty parts, `.` and `..`.

- [ ] **Step 4: Refactor existing `JobManager` to compose the store**

Move only reservation, artifact resolution, discard, and orphan cleanup. Preserve `/api/jobs`, all existing statuses, report paths, analysis concurrency, and response JSON. Do not rename existing public methods in this task.

- [ ] **Step 5: Verify all existing Web behavior**

```powershell
\.venv\Scripts\python.exe -m pytest tests/web/test_storage.py tests/web/test_api.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add src/videoscope/web/storage.py src/videoscope/web/jobs.py tests/web
git commit -m "refactor: share safe local job storage"
```

## Task 9: Add confirmation-gated Publish Ready Web jobs and API

**Files:**

- Add: `src/videoscope/web/publish_jobs.py`
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/app.py`
- Test: `tests/web/test_publish_api.py`

**Interfaces:**

- Consumes: `LocalJobStore`, `PublishReadyPipeline`, `PublishProfileId`.
- Produces: profile listing, upload/prepare, plan, confirmation, SSE progress, artifact, cancellation, and cleanup endpoints under `/api/publish/`.

- [ ] **Step 1: Write failing API tests**

Cover:

```text
GET  /api/publish/profiles
POST /api/publish/jobs
GET  /api/publish/jobs/{job_id}
GET  /api/publish/jobs/{job_id}/events
GET  /api/publish/jobs/{job_id}/plan
POST /api/publish/jobs/{job_id}/confirm
GET  /api/publish/jobs/{job_id}/artifacts/{path}
DELETE /api/publish/jobs/{job_id}
```

Test upload limits, invalid profile, ffprobe rejection, ordered events, `awaiting_confirmation`, digest mismatch, exactly-once confirmation, cancel-before-confirm, cancel-during-process, path traversal, sanitized errors, `needs_review`, TTL cleanup, and analysis API regression.

- [ ] **Step 2: Run and confirm failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/web/test_publish_api.py -v
```

- [ ] **Step 3: Add path-free API models**

Define:

```python
class PublishJobStatus(StrEnum):
    QUEUED = "queued"
    INSPECTING = "inspecting"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishConfirmation(WebModel):
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
```

`PublishJobResponse` includes job ID, status, message, percent, profile ID, warnings, sanitized error, and relative API links. It contains no local path.

- [ ] **Step 4: Implement a separate `PublishJobManager`**

The first worker calls `prepare()` and stops at `AWAITING_CONFIRMATION`. `confirm()` compares the submitted digest in constant time with `hmac.compare_digest()`, then submits execution exactly once. Use the existing CPU pool limit; the MVP has no heavy-model queue. Keep all events ordered and monotonic.

- [ ] **Step 5: Mount API routes with existing security policy**

Reuse upload chunking, MIME/extension warnings, trusted hosts, loopback-origin checks, maximum config size, artifact containment, and FastAPI lifespan shutdown. Do not add wildcard CORS or a cloud upload route.

- [ ] **Step 6: Verify**

```powershell
\.venv\Scripts\python.exe -m pytest tests/web/test_publish_api.py tests/web/test_api.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add src/videoscope/web tests/web
git commit -m "feat: add local Publish Ready API"
```

## Task 10: Add Publish Ready to the packaged React workbench

**Files:**

- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/api.test.ts`
- Modify: `web/src/App.tsx`
- Add: `web/src/components/PublishReadyView.tsx`
- Add: `web/src/components/PublishReadyView.test.tsx`
- Add: `web/src/components/PublishProfileSelector.tsx`
- Add: `web/src/components/PublishPlanReview.tsx`
- Add: `web/src/components/PublishPreview.tsx`
- Add: `web/src/components/PublishResult.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**

- Consumes: `/api/publish/*` typed responses and SSE events.
- Produces: a local workbench flow for upload, profile selection, plan review, preview, confirmation, processing, verification, and download.

- [ ] **Step 1: Write failing typed client tests**

Add `listPublishProfiles`, `createPublishJob`, `getPublishJob`, `getPublishPlan`, `confirmPublishJob`, `subscribeToPublishEvents`, and `publishArtifactUrl`. Verify encoded IDs, `FormData`, digest JSON, reconnect cursor, terminal states including `needs_review`, and API error messages.

- [ ] **Step 2: Write failing component tests**

Test three profile choices, source-local statement, original-not-overwritten text, staged progress, plan action list, six-second preview, confirmation button, disabled duplicate confirmation, cancellation, passed result, needs-review result, failed result, and download links. Use fake API functions; tests must not start FFmpeg or a server.

- [ ] **Step 3: Run and confirm failure**

```powershell
Set-Location web
npm test -- --run src/api.test.ts src/components/PublishReadyView.test.tsx
```

- [ ] **Step 4: Implement strict TypeScript contracts**

Mirror the Python JSON exactly. Use:

```ts
export type PublishProfileId =
  | "compatible_mp4"
  | "social_vertical_9_16"
  | "social_horizontal_16_9";

export type PublishJobStatus =
  | "queued"
  | "inspecting"
  | "planning"
  | "awaiting_confirmation"
  | "processing"
  | "verifying"
  | "completed"
  | "needs_review"
  | "failed"
  | "cancelled";
```

Do not use `any`; unknown JSON parameters remain `Record<string, unknown>`.

- [ ] **Step 5: Implement the workflow UI**

Add a top-level `Analyze` / `Publish Ready` mode switch without adding a router dependency. The publish view explains that processing stays in the loopback local service, the source is not overwritten, and the browser upload is copied only into the configured local job directory. Show profile cards, source metadata, plan actions, before/after preview, explicit confirmation, stage progress, verification checks, and artifacts.

- [ ] **Step 6: Add responsive and accessible states**

Use labels, visible focus, keyboard-operable profile cards, status text in addition to color, `aria-live` for progress, meaningful preview labels, and mobile stacking. Respect `prefers-reduced-motion`; no progress animation may be required to understand state.

- [ ] **Step 7: Verify front end and repository**

```powershell
Set-Location web
npm test
npm run build
Set-Location ..
\.venv\Scripts\python.exe scripts\validate.py
```

- [ ] **Step 8: Record the review checkpoint**

```powershell
git add web src/videoscope/web/static
git commit -m "feat(web): add Publish Ready workbench"
```

## Task 11: Add deterministic audio/video fixtures and native end-to-end coverage

**Files:**

- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`
- Add: `tests/resolve/test_fixture_publish.py`

**Interfaces:**

- Consumes: local FFmpeg/ffprobe and the real `PublishReadyPipeline`.
- Produces: generated `publish_av.mp4` plus fixture-based proof for all three profiles.

- [ ] **Step 1: Add the fixture test before the fixture**

The test skips with a clear message if FFmpeg/ffprobe is absent. For each profile, run real native processing in a temporary directory, then assert:

```python
assert result.video_path.is_file()
assert result.technical_report.verification.status is VerificationStatus.PASSED
assert result.video_path.read_bytes() != source.read_bytes()
assert source.read_bytes() == original_source_bytes
assert not any(str(tmp_path) in path.read_text("utf-8") for path in json_reports)
```

Probe the vertical output as 1080×1920, horizontal as 1920×1080, and compatible output with source dimensions. Require H.264 video, retained audio, duration within declared tolerance, and a readable cover.

- [ ] **Step 2: Run and confirm the missing fixture failure**

```powershell
\.venv\Scripts\python.exe -m pytest tests/resolve/test_fixture_publish.py -v
```

- [ ] **Step 3: Generate `publish_av.mp4` deterministically**

Create a four-second, 320×180, 12 FPS video with smooth lavfi motion and a 440 Hz sine track. Use the existing shell-free fixture command helper and overwrite only `tests/fixtures/generated/publish_av.mp4` when `--force` is supplied. Add manifest metadata without claiming a detector anomaly.

- [ ] **Step 4: Run real end-to-end checks**

```powershell
\.venv\Scripts\python.exe scripts\generate_test_videos.py --force
\.venv\Scripts\python.exe -m pytest tests/resolve/test_fixture_publish.py -v
\.venv\Scripts\python.exe scripts\validate.py
```

Expected: all three outputs pass technical and detector-regression verification; generated videos remain untracked or ignored according to the existing fixture policy.

- [ ] **Step 5: Record the review checkpoint**

```powershell
git add scripts/generate_test_videos.py tests/fixtures/manifest.json tests/resolve/test_fixture_publish.py
git commit -m "test: cover Publish Ready profiles end to end"
```

## Task 12: Finish smoke tests, CI, packaging, and user documentation

**Files:**

- Modify: `scripts/smoke_test.py`
- Modify: `pyproject.toml`
- Modify: `src/videoscope/__init__.py`
- Modify: `CITATION.cff`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/web-api.md`
- Modify: `docs/release-checklist.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**

- Consumes: built wheel, generated `publish_av.mp4`, CLI, Web API, and packaged React assets.
- Produces: repeatable release evidence and accurate user instructions.

- [ ] **Step 1: Extend the clean-wheel smoke test**

After installing the base wheel in a clean temporary environment, run:

```text
videoscope --version
videoscope doctor
videoscope publish publish_av.mp4 --profile compatible_mp4 --output publish-smoke --yes
```

Assert `publish-ready.mp4`, `cover.jpg`, `changes.json`, and `technical-report.json` exist and verification is `passed`. Do not install AI, OCR, Web, or download models for the base smoke test.

- [ ] **Step 2: Move the package to the v0.3 development line**

Set `pyproject.toml` and `src/videoscope/__init__.py` to `0.3.0.dev0`, set `CITATION.cff` to `0.3.0-dev0`, and add an `Unreleased / 0.3.0` CHANGELOG section that describes only work actually completed by this plan. Do not create a tag, GitHub Release, or PyPI upload.

- [ ] **Step 3: Extend CI**

Keep Linux and Windows on Python 3.11 and 3.12. Generate fixtures before validation. Add the fixture publish test and retain the wheel/sdist audit. The front-end job runs `npm ci`, `npm test`, and `npm run build` in `web/`. Do not publish PyPI or deploy a website.

- [ ] **Step 4: Update user documentation**

README first-use example:

```powershell
videoscope publish input.mp4 `
  --profile social_vertical_9_16 `
  --output runs\vertical-publish
```

Explain the interactive plan, `--yes` for reviewed non-interactive jobs, Profile meanings, scale-and-pad behavior, output files, exit 5, local-only handling, original preservation, FFmpeg requirement, and limits. Do not claim current platform rules remain permanently valid or that verification proves artistic quality.

- [ ] **Step 5: Run complete release validation**

```powershell
\.venv\Scripts\python.exe scripts\generate_test_videos.py --force
\.venv\Scripts\python.exe scripts\validate.py
Set-Location web
npm ci
npm test
npm run build
Set-Location ..
\.venv\Scripts\python.exe -m build
\.venv\Scripts\python.exe scripts\audit_distribution.py dist
\.venv\Scripts\python.exe scripts\smoke_test.py --dist dist --video tests\fixtures\generated\publish_av.mp4
git status --short
```

Expected: Python validation passes, React tests/build pass, wheel and sdist build, distribution audit passes, smoke output verification is `passed`, no generated fixture video or run directory is staged, and no network/model download occurs.

- [ ] **Step 6: Run manual local Web acceptance**

Start `videoscope serve` on loopback, upload `publish_av.mp4`, choose each profile, inspect the plan and preview, confirm once, watch SSE stages, download the output, and play it in Firefox and Chromium. Verify cancellation before confirmation and during processing, refresh recovery, mobile layout, keyboard focus, and `needs_review` presentation with an injected test failure.

- [ ] **Step 7: Record actual limitations**

Record any unsupported encoder, codec, VFR, long-video resource, platform-player, or browser behavior in `docs/publish-ready.md` and the release checklist. Do not weaken tests or adjust the fixture manifest to hide a failure.

- [ ] **Step 8: Record the review checkpoint**

```powershell
git add scripts/smoke_test.py pyproject.toml src/videoscope/__init__.py CITATION.cff README.md CHANGELOG.md docs .github/workflows/ci.yml
git commit -m "docs: complete Publish Ready MVP release checks"
```

## Deferred Subprojects Requiring Separate Design and Plans

The following approved dual-track work is intentionally not part of this native MVP plan because each uses an independent runtime or product workflow:

- browser-local video transcoding on the public site, including FFmpeg-WASM/WebCodecs licensing, bundle-size, codec, memory, and cancellation decisions;
- public-site handoff between GitHub Pages and the loopback native service;
- D Safe Sharing visual/audio tracking and private/share package separation;
- B Video Rescue recoverability classification and salvage algorithms;
- C long-video transcription, content map, storyboard, and source mapping;
- aggressive AI enhancement and generated-region provenance;
- optional remote processing, accounts, cloud storage, and collaboration;
- real-world annotated Benchmark expansion and third-party integration program.

Each item receives its own design review before implementation. None may be silently added to this plan.

## Plan Self-Review Checklist

- [ ] Every native-local A MVP requirement maps to one task.
- [ ] The existing v0.1 analysis schema and CLI remain compatible.
- [ ] `PublishProfileId`, `PublishJobStatus`, and JSON field names match across Python, API, and TypeScript tasks.
- [ ] Plan confirmation uses the deterministic digest and cannot be bypassed by a stale or mismatched value.
- [ ] Source files are never overwritten and are byte-checked in tests.
- [ ] Public reports contain relative paths and sanitized diagnostics only.
- [ ] No action crops, deletes, interpolates, generates, or claims to repair source content.
- [ ] No overall quality score is introduced.
- [ ] Verification failure cannot appear as completed.
- [ ] FFmpeg commands are arrays with `shell=False` and bounded sanitized stderr.
- [ ] Base tests are offline, CPU-only, and model-free.
- [ ] Generated videos and run artifacts remain outside the distribution.
- [ ] All commit steps remain conditional on explicit user authorization.
- [ ] `rg -n -e "T[B]D" -e "T[O]DO" -e "implement[ ]later" -e "fill[ ]in" -e "similar[ ]to" docs/superpowers/plans/2026-08-01-publish-ready-native-mvp.md` returns no unresolved placeholders.
