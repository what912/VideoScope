# VideoScope Full Local Four-Mode Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one deterministic, copyright-clean 42-second local MP4 and an evidence package that exercises the real Publish Ready, Video Rescue, Long Video to Useful Content, and Safe Sharing workflows without modifying the source or auto-confirming user decisions.

**Architecture:** HyperFrames owns the deterministic seven-scene visual composition. A focused Python generator renders the clean base, adds declared bounded audio/video conditions with FFmpeg argument arrays, probes and hashes the final MP4, and atomically writes a manifest. A staged validation driver prepares A/B/C plans and D risks, accepts an explicit D review-decision file to create its exact preview/plan, and executes only a later matching confirmation file; an auditor then creates contact sheets and an honest verification summary from the actual outputs.

**Tech Stack:** Python 3.11+, HyperFrames HTML/GSAP, FFmpeg/ffprobe 8.x-compatible CLI, Pydantic/dataclasses already in the repository, Pillow/OpenCV already in the base package, pytest, ruff, mypy.

## Global Constraints

- Duration is `42.0` seconds with tolerance `1 / 24` second; canvas is `1280x720`; frame rate is constant `24 fps`.
- Final video is H.264 `yuv420p`; audio is AAC stereo `48000 Hz`; container is MP4.
- The seven exact source intervals are `[0,5]`, `[5,10]`, `[10,20]`, `[20,25]`, `[25,32]`, `[32,36]`, and `[36,42]` seconds.
- The Safe Sharing visual selection is exactly normalized box `[0.58, 0.18, 0.94, 0.78]` during `[25.0, 32.0]`; the matching manual audio mute interval is exactly `[25.0, 32.0]`.
- Useful Content reference keep ranges are exactly `[0,5]`, `[10,20]`, and `[36,42]`; the remaining four ranges are review/exclude candidates, not claims that the footage is objectively useless.
- Use only project-authored HTML/CSS/SVG, deterministic geometry, and locally generated tones. Do not download media, fonts, models, stock footage, music, or remote TTS.
- No real personal data. The visible identifiers must be `demo.user@example.invalid`, `+1 202-555-0107`, and `00.0000, 000.0000`, all labelled `FICTIONAL DATA / 虚构数据`.
- Every external command uses a sequence of arguments, `shell=False`, a timeout, return-code validation, and scrubbed diagnostics.
- The source file is immutable after publication into the review directory. Every A/B/C/D result uses a distinct output directory and must preserve the source SHA-256.
- Preparing and previewing are allowed automatically. Safe Sharing risk decisions require `privacy-review-decisions.json`; every A/B/C/D execution requires a later `confirmation.json` whose source hash, contract digest, plan digest, IDs, ranges, and review choices match exactly.
- Do not rewrite `needs_review`, `partial`, `failed`, or unavailable results as success. Actual pipeline measurements outrank intended fixture behavior.
- Generated videos, frames, reports, and module outputs stay under ignored `runs/full-local-demo/` and are not committed or published without separate authorization.
- Do not push, create a pull request, create a release, deploy, or upload any artifact in this plan.

---

## File Map

### Tracked composition and contract

- `demos/full-local-four-mode/DESIGN.md`: composition-level visual identity, timeline, motion, typography, and accessibility rules required by HyperFrames.
- `demos/full-local-four-mode/demo-contract.json`: canonical machine-readable duration, scene, privacy, content-selection, codec, and fictional-data contract.
- `demos/full-local-four-mode/index.html`: HyperFrames-initialized standalone composition, then authored as seven timed scenes with one paused registered GSAP master timeline.
- `demos/full-local-four-mode/README-template.md`: zero-beginner guide template whose status table is filled from actual verification results.

### Tracked generation and validation code

- `scripts/full_local_demo_contract.py`: typed contract loading, validation, public-path sanitization, deterministic JSON, and streaming hash helpers.
- `scripts/generate_full_local_demo.py`: HyperFrames/FFmpeg/ffprobe orchestration, deterministic audio/video post-processing, atomic source/manifest publication.
- `scripts/validate_full_local_demo.py`: two-phase `prepare` and `execute` driver for the real A/B/C/D pipelines.
- `scripts/audit_full_local_demo.py`: probe assertions, contact-sheet generation, redaction/mute/source-immutability checks, and verification summary/guide rendering.

### Tracked tests

- `tests/scripts/test_full_local_demo_contract.py`: exact contract, schema, path safety, deterministic serialization, and composition-static tests.
- `tests/scripts/test_generate_full_local_demo.py`: command-array, no-shell, failure atomicity, probe, and deterministic-generation tests using fake runners.
- `tests/scripts/test_validate_full_local_demo.py`: phase separation, digest binding, exact-range, source-immutability, and honest-status tests using injected fake pipelines.
- `tests/scripts/test_audit_full_local_demo.py`: contact-sheet sampling, privacy/audio/source checks, path redaction, and summary rendering tests.

### Ignored review output

- `runs/full-local-demo/VideoScope-Full-Local-Demo-Source.mp4`
- `runs/full-local-demo/demo-manifest.json`
- `runs/full-local-demo/prepared-review.json`
- `runs/full-local-demo/privacy-review-decisions.json`
- `runs/full-local-demo/confirmable-plan.json`
- `runs/full-local-demo/confirmation.json`
- `runs/full-local-demo/verification-summary.json`
- `runs/full-local-demo/README-demo.md`
- `runs/full-local-demo/source-contact-sheet.webp`
- `runs/full-local-demo/{publish-ready,video-rescue,useful-content,safe-sharing}/`

---

### Task 1: Lock the Machine-Readable Demo Contract

**Files:**
- Create: `demos/full-local-four-mode/DESIGN.md`
- Create: `demos/full-local-four-mode/demo-contract.json`
- Create: `demos/full-local-four-mode/index.html` (HyperFrames scaffold only)
- Create: `scripts/full_local_demo_contract.py`
- Create: `tests/scripts/test_full_local_demo_contract.py`

**Interfaces:**
- Consumes: approved design spec `docs/superpowers/specs/2026-08-12-full-local-demo-source-design.md`.
- Produces: `DemoContract`, `DemoScene`, `DemoPrivacySelection`, `load_demo_contract(path: Path) -> DemoContract`, `canonical_json_bytes(value: Mapping[str, object]) -> bytes`, `stream_sha256(path: Path) -> str`, and `safe_relative_path(path: Path, root: Path) -> str`.

- [ ] **Step 1: Run the offline HyperFrames preflight and scaffold**

```powershell
npx.cmd --no-install hyperframes doctor
npx.cmd --no-install hyperframes init demos/full-local-four-mode --non-interactive
```

Expected: the already available local HyperFrames CLI reports a usable browser/render environment and creates the composition directory. If `--no-install` cannot find HyperFrames, stop with the explicit blocker; do not permit npm to download it.

- [ ] **Step 2: Write failing exact-contract tests**

```python
def test_contract_has_exact_timeline_and_privacy_ranges() -> None:
    contract = load_demo_contract(CONTRACT_PATH)
    assert contract.duration_seconds == 42.0
    assert contract.frame_rate == 24
    assert [(scene.start_seconds, scene.end_seconds) for scene in contract.scenes] == [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 20.0),
        (20.0, 25.0),
        (25.0, 32.0),
        (32.0, 36.0),
        (36.0, 42.0),
    ]
    assert contract.privacy.start_seconds == 25.0
    assert contract.privacy.end_seconds == 32.0
    assert contract.privacy.box == (0.58, 0.18, 0.94, 0.78)
    assert contract.useful_keep_ranges == ((0.0, 5.0), (10.0, 20.0), (36.0, 42.0))


def test_contract_rejects_gaps_overlaps_remote_assets_and_real_identifiers(
    tmp_path: Path,
) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["scenes"][1]["start_seconds"] = 5.1
    with pytest.raises(ValueError, match="contiguous"):
        DemoContract.from_mapping(payload)
    assert "http://" not in CONTRACT_PATH.read_text(encoding="utf-8")
    assert "https://" not in CONTRACT_PATH.read_text(encoding="utf-8")
```

- [ ] **Step 3: Run the tests and confirm the missing-module failure**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/scripts/test_full_local_demo_contract.py -q
```

Expected: collection fails because `scripts.full_local_demo_contract` and the contract file do not exist.

- [ ] **Step 4: Implement frozen contract types and strict validation**

Use frozen dataclasses and explicit parsing. The public constructor must reject unknown keys, non-finite numbers, non-contiguous scenes, a final end other than `42.0`, a frame rate other than `24`, privacy ranges/boxes that differ from the approved constants, remote URLs, absolute paths, and identifiers other than the three reserved fictional values.

```python
@dataclass(frozen=True, slots=True)
class DemoScene:
    scene_id: str
    start_seconds: float
    end_seconds: float
    purpose: str


@dataclass(frozen=True, slots=True)
class DemoPrivacySelection:
    start_seconds: float
    end_seconds: float
    box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class DemoContract:
    schema_version: str
    duration_seconds: float
    width: int
    height: int
    frame_rate: int
    scenes: tuple[DemoScene, ...]
    privacy: DemoPrivacySelection
    useful_keep_ranges: tuple[tuple[float, float], ...]
    fictional_identifiers: tuple[str, ...]
```

Add `DemoContract.from_mapping(value: Mapping[str, object]) -> DemoContract` immediately after these fields. It must copy the mapping, compare its key set to the exact schema key set, parse nested scenes/privacy/ranges into new immutable tuples, reject booleans where numbers are expected, call `math.isfinite` for every float, and then call a private `_validate_contract(contract)` function containing the exact invariant checks from this task. `canonical_json_bytes` must use sorted keys, `ensure_ascii=False`, separators `(",", ":")`, `allow_nan=False`, and a trailing newline. `safe_relative_path` must resolve both paths, require containment in `root`, return POSIX separators, and reject `..`.

- [ ] **Step 5: Add the exact JSON contract and composition DESIGN.md**

The JSON must include seven scene IDs (`clean_hook`, `rescue_evidence`, `useful_tutorial`, `low_information`, `privacy_zone`, `motion_retake`, `verified_ending`), the exact intervals above, the video/audio format, privacy box/interval, keep ranges, fictional identifiers, and declared source conditions. `DESIGN.md` must repeat the approved color tokens, system-font rule, tabular numerals, functional scan/timecode motion, reduced-motion behavior, and the rule that the 25–32 second panel remains inside the declared box.

- [ ] **Step 6: Run focused tests**

Run the command from Step 2. Expected: all focused tests pass.

- [ ] **Step 7: Commit the scaffold and contract**

```powershell
git add demos/full-local-four-mode/DESIGN.md demos/full-local-four-mode/demo-contract.json demos/full-local-four-mode/index.html scripts/full_local_demo_contract.py tests/scripts/test_full_local_demo_contract.py
git commit -m "test: define full local demo contract"
```

---

### Task 2: Build the Seven-Scene HyperFrames Composition

**Files:**
- Modify: `demos/full-local-four-mode/index.html`
- Modify: `tests/scripts/test_full_local_demo_contract.py`

**Interfaces:**
- Consumes: `demo-contract.json` timing and `DESIGN.md` visual rules.
- Produces: standalone composition `data-composition-id="videoscope-full-local-demo"`, duration `42`, fps `24`, width `1280`, height `720`; global `window.__timelines` containing a paused GSAP timeline.

- [ ] **Step 1: Add failing static composition tests**

```python
def test_composition_is_offline_deterministic_and_registered() -> None:
    html = COMPOSITION_PATH.read_text(encoding="utf-8")
    assert 'data-composition-id="videoscope-full-local-demo"' in html
    assert 'data-duration="42"' in html
    assert 'data-fps="24"' in html
    assert html.count('data-scene-id="') == 7
    assert "window.__timelines" in html
    assert "paused: true" in html
    for banned in ("http://", "https://", "Math.random", "Date.now", "repeat: -1"):
        assert banned not in html


def test_all_scenes_have_transition_and_content_animation() -> None:
    html = COMPOSITION_PATH.read_text(encoding="utf-8")
    for scene_id in EXPECTED_SCENE_IDS:
        assert f'data-scene-id="{scene_id}"' in html
        assert f'animateScene("{scene_id}"' in html
    assert html.count('data-transition-id="') == 6
    assert "transitionBetween(" in html
```

- [ ] **Step 2: Run tests to confirm the scaffold is insufficient**

Run the Task 1 focused pytest command. Expected: the new composition assertions fail.

- [ ] **Step 3: Author the composition**

Create a single offline HTML source of truth with:

- a top-level composition element, seven absolutely positioned scene elements, and no `<template>`;
- CSS variables for all eight approved colors, 8px spacing, system sans-serif stack, and monospace/tabular timecodes;
- SVG observatory grid, scan lines, range bars, evidence markers, and privacy rectangle generated inline;
- bilingual text defined once in a `SCENE_COPY` object, not duplicated through the DOM;
- a paused `gsap.timeline({ paused: true })` registered as `window.__timelines = [masterTimeline]`;
- deterministic `animateScene(sceneId, at, duration)` and `transitionBetween(fromId, toId, at)` helpers;
- transforms/opacity only for high-frequency animation; no canvas particles, remote media, or unbounded loop;
- `prefers-reduced-motion` styling that removes nonessential scan/shake motion while keeping the state changes readable;
- a right-side fictional-data panel located at left `58%`, top `18%`, width `36%`, height `60%` during seconds 25–32;
- a repeated short instruction and stable reference grid during seconds 32–36; the camera-like whole-frame displacement is added later by the deterministic FFmpeg post-processor so the HyperFrames base remains clean.

The master timeline must use exact scene starts, animate every scene's title/content on entry, perform six short opacity/clip-path transitions, and not fade a scene out before its transition. The final scene may fade at 41.7 seconds.

- [ ] **Step 4: Run static, HyperFrames, and dense layout checks**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/scripts/test_full_local_demo_contract.py -q
npx.cmd --no-install hyperframes lint demos/full-local-four-mode
npx.cmd --no-install hyperframes inspect demos/full-local-four-mode --at 0,5,10,20,25,32,36,41.5 --strict
```

Expected: tests and lint pass; inspect reports no overflow, clipping, missing scene, or unreadable hero frame at the eight sample times.

- [ ] **Step 5: Commit the composition**

```powershell
git add demos/full-local-four-mode/index.html tests/scripts/test_full_local_demo_contract.py
git commit -m "feat: compose full local four-mode demo"
```

---

### Task 3: Generate the Deterministic Final Source and Manifest

**Files:**
- Create: `scripts/generate_full_local_demo.py`
- Create: `tests/scripts/test_generate_full_local_demo.py`

**Interfaces:**
- Consumes: `load_demo_contract`, composition directory, available HyperFrames/FFmpeg/ffprobe executables.
- Produces: `GenerationSummary`, `CommandRunner` protocol, `run_command(arguments: Sequence[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[str]`, `build_postprocess_arguments(base_video: Path, output_video: Path, ffmpeg: str) -> list[str]`, `probe_demo(path: Path, ffprobe: str) -> Mapping[str, object]`, and `generate_demo(project_root: Path, output_root: Path, *, force: bool, runner: CommandRunner = run_command) -> GenerationSummary`.

- [ ] **Step 1: Write failing orchestration tests with an injected command runner**

```python
def test_commands_are_arrays_and_never_use_shell(tmp_path: Path) -> None:
    runner = RecordingRunner()
    generate_demo(ROOT, tmp_path / "out", force=True, runner=runner)
    assert runner.calls
    assert all(isinstance(call.arguments, list) for call in runner.calls)
    assert all(call.shell is False for call in runner.calls)


def test_failed_second_generation_keeps_previous_published_source(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out"
    first = generate_demo(ROOT, output, force=True, runner=SuccessfulRunner())
    original = first.source_path.read_bytes()
    with pytest.raises(DemoGenerationError, match="ffprobe"):
        generate_demo(ROOT, output, force=True, runner=ProbeFailureRunner())
    assert first.source_path.read_bytes() == original


def test_postprocess_has_exact_bounded_conditions() -> None:
    args = build_postprocess_arguments(Path("base.mp4"), Path("final.mp4"), "ffmpeg")
    joined = " ".join(args)
    assert "between(t,5,10)" in joined
    assert "between(t,25,32)" in joined
    assert "libx264" in args and "yuv420p" in args
    assert "aac" in args and "48000" in args
```

Define `RecordedCall(arguments: list[str], cwd: Path, shell: bool)` and a `RecordingRunner` in the test file. `SuccessfulRunner` extends it and writes the requested render/post-process files plus fixed ffprobe JSON; `ProbeFailureRunner` returns exit code `1` only for the ffprobe call. These fakes must inspect argument positions rather than input filenames.

- [ ] **Step 2: Run tests and verify missing implementation failures**

```powershell
$env:PYTHONPATH = ((Resolve-Path 'src').Path + ';' + (Resolve-Path '.').Path)
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/scripts/test_generate_full_local_demo.py -q
```

- [ ] **Step 3: Implement safe command execution and staging**

`run_command` must call `subprocess.run(list(arguments), cwd=cwd, shell=False, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds)`, reject a non-zero return code, and scrub resolved workspace/user paths from the last 2000 stderr characters. `generate_demo` must build inside `output_root/.staging-<uuid>`, validate everything, then use `Path.replace` for final source and manifest. It must remove only that exact staging directory in `finally`.

- [ ] **Step 4: Implement deterministic render and post-processing**

Render the clean base at high quality:

```python
render_arguments = [
    "npx.cmd",
    "--no-install",
    "hyperframes",
    "render",
    "--output",
    str(staging_root / "base.mp4"),
    "--fps",
    "24",
    "--quality",
    "high",
    "--strict",
]
```

Run this argument list with `cwd=project_root / "demos" / "full-local-four-mode"`; `staging_root` is the resolved private staging directory created by `generate_demo`.

Post-process with one FFmpeg call that:

- maps the base video and one deterministic `aevalsrc` stereo source;
- uses time-bounded `eq` luminance/flicker and `boxblur` only for `between(t,5,10)`;
- applies deterministic whole-frame displacement only for `between(t,32,36)` by splitting the conditioned video, cropping the shake branch to `iw-32:ih-18` with sinusoidal `x/y` expressions, scaling it back to `1280x720`, and overlaying that branch with an `enable` expression; outside the interval the untouched conditioned branch remains visible;
- generates a low-level 220 Hz bed, bounded 60/118 Hz unwanted hum in 5–10 seconds, and a clearly measurable 880 Hz private cue in 25–32 seconds;
- encodes `libx264`, preset `medium`, CRF `16`, GOP `48`, scene-cut disabled, `yuv420p`, AAC `192k`, stereo `48000`;
- uses bitexact flags and fixed metadata; deliberately omits `+faststart`;
- writes only the fictional title/artist/comment/location metadata declared in the contract.

Do not use random-noise sources because they break byte determinism.

- [ ] **Step 5: Probe and atomically write the manifest**

`probe_demo` must request JSON `format` and `streams` from ffprobe and assert one H.264 video stream, `1280x720`, `24/1`, `yuv420p`, one AAC stereo audio stream at `48000`, and duration within one frame. The manifest must contain only relative paths, tool/version strings, command digests rather than raw absolute commands, exact contract ranges, final source SHA-256, source byte size, and normalized probe fields.

- [ ] **Step 6: Prove repeatability in tests**

Add a fake-runner test where two staged outputs with identical bytes produce byte-identical canonical manifests after removing no fields. Add a test that changing any contract value changes the contract digest. Run focused tests; expected: pass.

- [ ] **Step 7: Perform the first real generation twice**

```powershell
$ffbin = 'C:\Users\吴少泽\Documents\VideoScope\.release-audit\tools\ffmpeg\ffmpeg-8.1.2-essentials_build\bin'
$env:PATH = "$ffbin;$env:PATH"
$env:PYTHONPATH = ((Resolve-Path 'src').Path + ';' + (Resolve-Path '.').Path)
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/generate_full_local_demo.py --output runs/full-local-demo --force
Copy-Item runs/full-local-demo/VideoScope-Full-Local-Demo-Source.mp4 runs/full-local-demo/.first-source.mp4
Copy-Item runs/full-local-demo/demo-manifest.json runs/full-local-demo/.first-manifest.json
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/generate_full_local_demo.py --output runs/full-local-demo --force
Get-FileHash runs/full-local-demo/.first-source.mp4, runs/full-local-demo/VideoScope-Full-Local-Demo-Source.mp4 -Algorithm SHA256
Get-FileHash runs/full-local-demo/.first-manifest.json, runs/full-local-demo/demo-manifest.json -Algorithm SHA256
```

Expected: the two source hashes match and the two manifest hashes match. If the encoder produces different MP4 bytes, diagnose and remove nondeterministic mux fields; do not weaken the gate to decoded-pixel equality.

- [ ] **Step 8: Commit code, not rendered media**

```powershell
git add scripts/generate_full_local_demo.py tests/scripts/test_generate_full_local_demo.py
git commit -m "feat: generate deterministic full local demo source"
```

---

### Task 4: Prepare and Execute Publish Ready and Video Rescue Safely

**Files:**
- Create: `scripts/validate_full_local_demo.py`
- Create: `tests/scripts/test_validate_full_local_demo.py`

**Interfaces:**
- Consumes: final source/manifest, `PublishReadyPipeline`, `VideoRescuePipeline`.
- Produces: `DemoPipelineDependencies`, `WorkflowCandidate`, `PreparedWorkflow`, `PreparedReview`, `WorkflowConfirmation`, `ExecutionConfirmation`, `WorkflowOutcome`, `prepare_publish_ready(source: Path, output: Path) -> PreparedWorkflow`, `execute_publish_ready(prepared: PreparedWorkflow, confirmation: WorkflowConfirmation) -> WorkflowOutcome`, `prepare_rescue(source: Path, output: Path) -> PreparedWorkflow`, `execute_rescue(prepared: PreparedWorkflow, confirmation: WorkflowConfirmation) -> WorkflowOutcome`, and CLI phases `prepare`/`execute`.

- [ ] **Step 1: Write tests that prohibit implicit confirmation**

```python
def test_prepare_never_calls_execute_or_confirm(
    fake_dependencies: DemoPipelineDependencies,
) -> None:
    result = prepare_all(SOURCE, OUTPUT, dependencies=fake_dependencies)
    assert result.workflows["publish_ready"].plan_digest
    assert result.workflows["video_rescue"].plan_digest
    assert fake_dependencies.confirm_calls == []
    assert fake_dependencies.execute_calls == []


def test_execute_rejects_digest_or_action_mismatch(prepared: PreparedWorkflow) -> None:
    forged = confirmation_for(prepared).model_copy(update={"plan_digest": "0" * 64})
    with pytest.raises(DemoConfirmationError, match="digest"):
        execute_from_confirmation(prepared, forged)
```

- [ ] **Step 2: Add strict local review/confirmation models**

Use Pydantic `extra="forbid"` models with these exact fields:

```python
class WorkflowCandidate(BaseModel):
    id: str
    kind: str
    ranges: tuple[tuple[float, float], ...] = ()
    requires_confirmation: bool
    evidence: tuple[dict[str, JsonValue], ...] = ()
    preview_relative_path: str | None = None
    limitations: tuple[str, ...] = ()


class PreparedWorkflow(BaseModel):
    workflow_id: Literal[
        "publish_ready", "video_rescue", "useful_content", "safe_sharing"
    ]
    plan_digest: str | None
    candidates: tuple[WorkflowCandidate, ...]
    confirmation_required: Literal[True] = True
    preparation_status: str


class PreparedReview(BaseModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str
    contract_sha256: str
    workflows: dict[str, PreparedWorkflow]


class WorkflowConfirmation(BaseModel):
    workflow_id: str
    plan_digest: str
    accepted_action_ids: tuple[str, ...] = ()
    accepted_trim_damage_ids: tuple[str, ...] = ()


class ExecutionConfirmation(BaseModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str
    contract_sha256: str
    workflows: dict[str, WorkflowConfirmation]


class WorkflowOutcome(BaseModel):
    workflow_id: str
    status: str
    source_sha256_before: str
    source_sha256_after: str
    actions: tuple[dict[str, JsonValue], ...] = ()
    checks: tuple[dict[str, JsonValue], ...] = ()
    artifacts: dict[str, str] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    final_human_review_required: bool = False
```

Use `Field(default_factory=dict)` for the implementation of `WorkflowOutcome.artifacts`; the literal above is only the compact public shape. Define frozen `DemoPipelineDependencies` with four callable factories named `publish_factory`, `rescue_factory`, `content_factory`, and `privacy_factory`, each accepting its normal config/output input and returning the corresponding real pipeline. Production defaults construct the real pipelines; tests inject recording fakes. `prepared-review.json` contains source hash, contract digest, plan digests, action IDs/kinds/ranges, preview relative paths, limitations, and `confirmation_required: true`. `confirmation.json` repeats the source hash and contract digest plus exact accepted IDs. It must never be created by `prepare`. Because Publish and Rescue preparations own in-process source descriptors, `execute` must create a fresh pipeline, prepare again, compare the new digest/candidate IDs/ranges byte-for-byte to `prepared-review.json`, and only then execute the matching confirmation.

- [ ] **Step 3: Implement Publish Ready adapter**

Create `PublishReadyPipeline(PublishReadyConfig(profile_id=PublishProfileId.COMPATIBLE_MP4, output_directory=output))`; call `prepare(source)`; serialize its plan and preview for review. On execute, require exact `confirmation.plan_digest`, call `pipeline.execute(preparation, confirmed_plan_digest=confirmation.plan_digest)`, and always `pipeline.discard(preparation)` in `finally`. Record completed only when `result.status is PublishReadyStatus.COMPLETED` and `result.technical_report.verification.status is VerificationStatus.PASSED`. Record cover/report/video paths relative to the A output root and verify the source hash is unchanged.

- [ ] **Step 4: Implement Rescue adapter with evidence-gated selection**

Create `VideoRescuePipeline(RescueConfig(output_directory=output, strategy=RescueStrategy.BALANCED, symptoms=(RescueSymptom.DARK, RescueSymptom.SOFT_DETAIL, RescueSymptom.FLICKER, RescueSymptom.SHAKE, RescueSymptom.AUDIO_NOISE)))`; call `prepare(source)`. Persist every candidate action, its evidence/ranges, preview, `requires_confirmation`, and limitation. The execute phase accepts only IDs present in both the re-prepared plan and the user-authored confirmation. Build `RescueConfirmation` with `plan_digest=confirmation.plan_digest`, `publish_faithful=True`, `publish_improved` equal to whether an accepted action is an existing improvement kind, `accepted_action_ids=confirmation.accepted_action_ids`, and `accepted_trim_damage_ids=confirmation.accepted_trim_damage_ids`. Call `confirm`, then `execute`; call `abort` if execution never begins.

Never accept all `requires_confirmation` actions automatically. For the real run, inspect `prepared-review.json` and the private previews, then manually author `confirmation.json` with only measured, visually supported actions.

- [ ] **Step 5: Add honest result tests**

Cover completed, needs-review, partial, and failed fake outcomes. Assert the public summary preserves the enum value, a failed verification cannot become completed, a missing improved result is not advertised, no absolute path is serialized, and source hashes match before/after.

- [ ] **Step 6: Run focused tests and prepare the real A/B review**

```powershell
$env:PYTHONPATH = ((Resolve-Path 'src').Path + ';' + (Resolve-Path '.').Path)
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/scripts/test_validate_full_local_demo.py -q
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/validate_full_local_demo.py prepare --source runs/full-local-demo/VideoScope-Full-Local-Demo-Source.mp4 --manifest runs/full-local-demo/demo-manifest.json --output runs/full-local-demo
```

Expected: tests pass; prepare writes review data and previews but no final A/B/C/D output and no confirmation file.

- [ ] **Step 7: Review and execute A/B only**

Inspect the prepared A/B plans and previews. Write `confirmation.json` containing the exact Publish digest and only the Rescue action IDs/ranges supported by the evidence. Then run:

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/validate_full_local_demo.py execute --prepared runs/full-local-demo/prepared-review.json --confirmation runs/full-local-demo/confirmation.json --only publish-ready --only video-rescue
```

Expected: separate A/B result directories; source hash unchanged; truthful statuses and verification checks written even when Rescue reports review-needed rather than improvement.

- [ ] **Step 8: Commit the validation driver and tests**

```powershell
git add scripts/validate_full_local_demo.py tests/scripts/test_validate_full_local_demo.py
git commit -m "test: validate publish ready and rescue demo outcomes"
```

---

### Task 5: Prepare and Execute Useful Content and Safe Sharing Safely

**Files:**
- Modify: `scripts/validate_full_local_demo.py`
- Modify: `tests/scripts/test_validate_full_local_demo.py`

**Interfaces:**
- Consumes: Task 4 prepared/confirmation models, `LongVideoContentPipeline`, `SafeSharingPipeline`.
- Produces: `PrivacyReviewChoice`, `PrivacyReviewFile`, `ConfirmablePlan`, `prepare_useful_content`, `execute_useful_content`, `scan_safe_sharing`, `preview_safe_sharing`, and `execute_safe_sharing`; the final CLI phases are `prepare`, `preview`, and `execute`.

- [ ] **Step 1: Add failing exact-range tests**

```python
def test_useful_content_uses_only_approved_keep_ranges(
    fake_dependencies: DemoPipelineDependencies,
) -> None:
    prepared = prepare_useful_content(SOURCE, OUTPUT, dependencies=fake_dependencies)
    assert [item.ranges[0] for item in prepared.candidates if item.kind == "keep"] == [
        (0.0, 5.0),
        (10.0, 20.0),
        (36.0, 42.0),
    ]


def test_safe_sharing_uses_exact_manual_visual_and_audio_selections(
    fake_dependencies: DemoPipelineDependencies,
) -> None:
    scanned = scan_safe_sharing(SOURCE, OUTPUT, dependencies=fake_dependencies)
    assert scanned.manual_visual_regions[0].box.model_dump() == {
        "x_min": 0.58,
        "y_min": 0.18,
        "x_max": 0.94,
        "y_max": 0.78,
    }
    assert (
        scanned.manual_audio_intervals[0].start_seconds,
        scanned.manual_audio_intervals[0].end_seconds,
    ) == (25.0, 32.0)
```

- [ ] **Step 2: Implement Useful Content preparation**

Compute the source SHA-256, create three `ContentUserRange(kind=KEEP)` values using `ContentTimeRange` and `make_user_range_id`, and use `ContentGoal.SELECTED_CLIPS` with `export_clips=True`, `minimum_chapter_duration_seconds=1`, explicit local ffmpeg/ffprobe paths, `ContentPreviewBuilder`, and `NativeContentExecutor`. Call `prepare` then `preview`; persist the proposed actions and previews without accepting them.

Execution must require exact accepted action IDs from the confirmation, assign `content_confirmation = pipeline.confirm(review, accepted_action_ids=confirmation.accepted_action_ids)`, then call `pipeline.execute(review, content_confirmation)`. Record source-time mappings and distinguish completed/partial/needs-review/failed. As with A/B, the execute process must re-prepare and compare digest, candidate IDs, and exact source ranges before confirmation.

- [ ] **Step 3: Implement Safe Sharing scan and explicit review file**

Create `SafeSharingConfig(audience="public", sample_fps=5.0)`. After `scan`, build:

```python
visual = ManualVisualRegionInput(
    start_seconds=25.0,
    end_seconds=32.0,
    box=NormalizedBox(x_min=0.58, y_min=0.18, x_max=0.94, y_max=0.78),
    style=RedactionStyle.SOLID_FILL,
)
audio = ManualAudioIntervalInput(
    start_seconds=25.0,
    end_seconds=32.0,
    style=RedactionStyle.MUTE,
)
```

Build corresponding manual risks and write them with all scanner risks to `prepared-review.json`; do not call `pipeline.review`, `prepare`, `preview`, or `confirm` during `scan_safe_sharing`. Add strict models:

```python
@dataclass(frozen=True, slots=True)
class SafeSharingScanPreparation:
    scan: PrivacyScanResult
    scan_digest: str
    manual_visual_regions: tuple[ManualVisualRegionInput, ...]
    manual_audio_intervals: tuple[ManualAudioIntervalInput, ...]


class PrivacyReviewChoice(BaseModel):
    risk_id: str
    decision: Literal["allow", "redact"]
    style: str | None


class PrivacyReviewFile(BaseModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str
    contract_sha256: str
    scan_digest: str
    reviewed_at: datetime
    choices: tuple[PrivacyReviewChoice, ...]


class ConfirmablePlan(BaseModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str
    contract_sha256: str
    workflows: dict[str, PreparedWorkflow]
```

The user-authored `privacy-review-decisions.json` must contain exactly one choice for every risk and no unknown IDs. Metadata risks use `redact/remove_metadata`; the manual visual risk uses `redact/solid_fill`; the manual audio risk uses `redact/mute`. Any heuristic risk must be explicitly `allow` or `redact`; absence is an error, never implicit allow.

- [ ] **Step 4: Implement Safe Sharing preview and execution**

The `preview` CLI phase must re-scan, compare the source/contract/scan digests and exact risk IDs to `prepared-review.json`, convert every `PrivacyReviewChoice` into a `PrivacyReviewDecision` with one fixed UTC timestamp stored in the review file, then call:

```python
reviewed = pipeline.review(
    scan.scan_id,
    reviews,
    manual_visual_regions=(visual,),
    manual_audio_intervals=(audio,),
)
prepared = pipeline.prepare(reviewed.review_id)
preview_path = pipeline.preview(prepared.preparation_id)
```

Write `confirmable-plan.json` with the exact D plan digest, D reviewed risk IDs/styles, preview relative path, and the already prepared A/B/C plan data. Do not create `confirmation.json` and do not call `pipeline.confirm`.

The `execute` phase must re-run the full scan/review/prepare/preview sequence with the same fixed review timestamp, compare the new plan digest and actions to `confirmable-plan.json`, then require the user-authored `confirmation.json` to echo every A/B/C/D plan digest and accepted ID before calling `pipeline.confirm(prepared.preparation_id, confirmation.plan_digest)`. If any rescan or plan changes, stop and regenerate review instead of accepting the new plan.

Record the `public` profile's final-human-review requirement. Completed is allowed only when `PrivacyJobOutcome.COMPLETED` and all required verification checks pass. Always call `discard` on an unconsumed lifecycle.

- [ ] **Step 5: Add negative and honesty tests**

Test one-frame-shifted ranges, expanded boxes, mismatched risk IDs, duplicate/missing review decisions, changed scan digest, changed plan digest, non-public audience, and attempted serialization of private paths. All must fail before native execution. Test that `NEEDS_REVIEW` stays `needs_review` and final human review remains true even after technical checks pass.

- [ ] **Step 6: Run focused tests, refresh preparation, and review C/D**

```powershell
$env:PYTHONPATH = ((Resolve-Path 'src').Path + ';' + (Resolve-Path '.').Path)
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/scripts/test_validate_full_local_demo.py -q
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/validate_full_local_demo.py prepare --source runs/full-local-demo/VideoScope-Full-Local-Demo-Source.mp4 --manifest runs/full-local-demo/demo-manifest.json --output runs/full-local-demo
```

Inspect `prepared-review.json`. Author `privacy-review-decisions.json` with an explicit choice for every D risk, then run:

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/validate_full_local_demo.py preview --prepared runs/full-local-demo/prepared-review.json --privacy-review runs/full-local-demo/privacy-review-decisions.json --output runs/full-local-demo
```

Inspect C and D previews plus `confirmable-plan.json`; only then author `confirmation.json` with the exact A/B/C/D digests and accepted IDs.

- [ ] **Step 7: Execute C/D and verify independent outputs**

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/validate_full_local_demo.py execute --prepared runs/full-local-demo/prepared-review.json --privacy-review runs/full-local-demo/privacy-review-decisions.json --confirmable-plan runs/full-local-demo/confirmable-plan.json --confirmation runs/full-local-demo/confirmation.json --only useful-content --only safe-sharing
```

Expected: separate C/D output roots; C includes source mappings for all delivered clips; D removes forbidden metadata, applies visual redaction and audio mute only in 25–32 seconds, preserves the source hash, and retains the human-review warning.

- [ ] **Step 8: Commit C/D support**

```powershell
git add scripts/validate_full_local_demo.py tests/scripts/test_validate_full_local_demo.py
git commit -m "test: validate content and safe sharing demo outcomes"
```

---

### Task 6: Audit the Media and Package the Local Review Evidence

**Files:**
- Create: `scripts/audit_full_local_demo.py`
- Create: `tests/scripts/test_audit_full_local_demo.py`
- Create: `demos/full-local-four-mode/README-template.md`

**Interfaces:**
- Consumes: source manifest, A/B/C/D result summaries and public artifacts.
- Produces: `AuditWorkflow`, `AuditSummary`, `hero_timestamps(contract: DemoContract) -> tuple[float, ...]`, `assemble_summary(outcomes: Sequence[WorkflowOutcome]) -> AuditSummary`, `build_contact_sheet(video: Path, timestamps: Sequence[float], output: Path) -> Path`, `audit_source_and_results(root: Path) -> AuditSummary`, `write_verification_summary(summary: AuditSummary, path: Path) -> None`, and `render_beginner_guide(summary: AuditSummary, template: Path, output: Path) -> None`.

Use strict frozen Pydantic models. `AuditWorkflow` contains `workflow_id`, actual `status`, `checks`, `actions`, `limitations`, relative `artifacts`, `source_unchanged`, and `final_human_review_required`. `AuditSummary` contains schema/tool/environment versions, source SHA-256, contract digest, deterministic-generation status, `workflows: dict[str, AuditWorkflow]`, `overall_status`, and global limitations. `overall_status` is `passed` only when every mandatory technical check passed and no workflow is partial, review-needed, failed, or unavailable.

- [ ] **Step 1: Write failing audit tests**

```python
def test_contact_sheet_uses_all_seven_hero_frames(tmp_path: Path) -> None:
    timestamps = hero_timestamps(load_demo_contract(CONTRACT_PATH))
    assert timestamps == (2.5, 7.5, 15.0, 22.5, 28.5, 34.0, 39.0)


def test_summary_never_promotes_review_needed_to_passed() -> None:
    workflow = WorkflowOutcome(
        workflow_id="video_rescue",
        status="needs_review",
        source_sha256_before="a" * 64,
        source_sha256_after="a" * 64,
    )
    summary = assemble_summary((workflow,))
    assert summary.workflows["video_rescue"].status == "needs_review"
    assert summary.overall_status != "passed"


def test_public_files_have_no_absolute_paths_or_secrets(
    tmp_path: Path,
    audit_summary: AuditSummary,
) -> None:
    write_verification_summary(audit_summary, tmp_path / "verification-summary.json")
    text = (tmp_path / "verification-summary.json").read_text(encoding="utf-8")
    assert str(Path.home()) not in text
    assert not re.search(r"[A-Za-z]:[/\\\\]", text)
    assert "api_key" not in text.lower()
```

The `audit_summary` fixture must contain the fixed source hash, contract digest, one relative artifact `publish-ready/video.mp4`, and four explicit workflow states; it must contain no temporary-directory path.

- [ ] **Step 2: Implement deterministic contact sheets**

Use FFmpeg argument arrays to extract exact frames at `2.5, 7.5, 15.0, 22.5, 28.5, 34.0, 39.0`, then Pillow to arrange a `4x2` sheet with the final cell as legend, fixed `320x180` thumbnails, local system font fallback, bilingual scene labels, and timestamp text. Use the same seven source timestamps mapped through C output mappings for result sheets. Delete only the sheet's private frame staging directory.

- [ ] **Step 3: Implement targeted technical checks**

The auditor must:

- re-probe source and all completed outputs;
- confirm source hash equals manifest before and after every workflow;
- inspect Publish Ready's verification/checks, cover, report, and fast-start state;
- inspect Rescue faithful verification and improved verification only when present;
- verify C's delivered source mappings equal the explicitly confirmed selection;
- sample D at 24.9, 25.1, 28.5, 31.9, and 32.1 seconds to show redaction only inside the interval;
- measure RMS of D audio in 25.25–31.75 seconds and adjacent 24–25/32–33 second control windows, requiring the muted interval to be at least 30 dB below both controls;
- verify forbidden `public` metadata fields are absent;
- record any unavailable check as `not_verified`, never as pass.

- [ ] **Step 4: Implement canonical summary and zero-beginner guide**

`verification-summary.json` must include schema/tool versions, source hash, contract digest, environment versions, actual status/checks/actions/limitations per A/B/C/D, relative artifact paths and hashes, source immutability, deterministic-generation status, and `final_human_review_required` for D. The guide must explain exactly:

1. use the generated source;
2. open one module at a time;
3. review the prepared plan/preview;
4. confirm only the displayed digest/actions;
5. compare the separate output against the source;
6. read review-needed/limitations before sharing.

It must not claim all Rescue actions are always found or corrected.

- [ ] **Step 5: Run focused tests and generate the audit package**

```powershell
$ffbin = 'C:\Users\吴少泽\Documents\VideoScope\.release-audit\tools\ffmpeg\ffmpeg-8.1.2-essentials_build\bin'
$env:PATH = "$ffbin;$env:PATH"
$env:PYTHONPATH = ((Resolve-Path 'src').Path + ';' + (Resolve-Path '.').Path)
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/scripts/test_audit_full_local_demo.py -q
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/audit_full_local_demo.py --root runs/full-local-demo
```

Expected: focused tests pass; source and available result contact sheets, verification summary, and beginner guide are written atomically. The overall status is the minimum truthful state across required checks.

- [ ] **Step 6: Run the complete local quality gate**

```powershell
npx.cmd --no-install hyperframes lint demos/full-local-four-mode
npx.cmd --no-install hyperframes inspect demos/full-local-four-mode --at 0,5,10,20,25,32,36,41.5 --strict
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts/validate.py
git status --short
```

Expected: HyperFrames gates pass; repository validation passes; `git status` shows only the intended tracked code/docs changes and no generated `runs/full-local-demo` media.

- [ ] **Step 7: Manually inspect the video and contact sheets**

Play the full source and each completed output locally. Confirm bilingual text is readable, scene changes happen at the contract boundaries, the 25–32 second fictional panel remains inside the declared rectangle, audio has no clipping, and no output overwrites the source. Record any visual defect in the verification summary and regenerate rather than editing the report to hide it.

- [ ] **Step 8: Commit the audit tooling and guide template**

```powershell
git add scripts/audit_full_local_demo.py tests/scripts/test_audit_full_local_demo.py demos/full-local-four-mode/README-template.md
git commit -m "docs: package full local demo verification evidence"
```

---

## Final Acceptance Checklist

- [ ] `VideoScope-Full-Local-Demo-Source.mp4` is 42 seconds ± one frame, 1280x720, constant 24 fps, H.264/yuv420p, AAC stereo/48 kHz.
- [ ] Two clean generation runs have identical source SHA-256 and manifest SHA-256.
- [ ] The source contains no remote asset, real identifier, personal path, secret, downloaded model, or copyrighted third-party media.
- [ ] A, B, C, and D were prepared and previewed before a separate confirmation file was authored.
- [ ] No confirmation accepts an unknown, changed, or unsupported action/risk/range.
- [ ] A writes a verified separate compatible MP4, cover, and report or records its actual non-completed state.
- [ ] B verifies the faithful output; an improved output is claimed only when its evidence, confirmation, and verification all pass.
- [ ] C delivers only the explicitly reviewed ranges and preserves source-time mappings.
- [ ] D removes public-forbidden metadata, redacts and mutes exactly 25–32 seconds, and still states that final human review is required.
- [ ] Source SHA-256 is unchanged after every workflow.
- [ ] `verification-summary.json` preserves actual partial/review-needed/failed states.
- [ ] Contact sheets and the full video have passed manual visual/audio review.
- [ ] `python scripts/validate.py` passes.
- [ ] No generated artifact was committed, pushed, uploaded, released, or deployed.
