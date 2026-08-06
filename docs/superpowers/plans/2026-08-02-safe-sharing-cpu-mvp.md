# VideoScope Safe Sharing CPU MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, CPU-first Safe Sharing workflow that scans a video for reviewable privacy risks, accepts explicit user redaction decisions, renders a new share copy, and verifies the output without modifying the source.

**Architecture:** Add a versioned `videoscope.privacy` domain beside the frozen v0.1 report domain and reuse the existing Resolve confirmation, job, storage, SSE, and artifact foundations. Privacy scanners produce an immutable risk map; a planner turns reviewed decisions into a digest-bound plan; a streaming executor writes visual/audio redactions; a conservative verification gate alone decides whether a share package is completed or needs review.

**Tech Stack:** Python 3.11+, Pydantic, NumPy, OpenCV headless, FFmpeg/ffprobe subprocess argument arrays, Typer, FastAPI, React, strict TypeScript, Vitest, pytest, Ruff, mypy.

## Global Constraints

- Preserve the frozen v0.1 `AnalysisReport`, detector protocol, `videoscope analyze`, and existing report schema.
- Source videos are read-only; every media-changing action writes a new file in a job workspace and is delivered atomically.
- Base installation and base tests are offline, CPU-only, GPU-free, model-free, and do not download assets.
- Every external command uses an argument array with `shell=False`, a bounded timeout, checked return code, and sanitized stderr.
- Windows, Linux, and macOS paths are supported, including spaces, Chinese, and non-ASCII characters.
- No absolute user path, username, GPS value, raw sensitive OCR text, or unredacted private evidence may enter the share package.
- Face regions are anonymous tracks only; do not identify people or create an identity database.
- Optional OCR is lazy, explicit, and failure-isolated; the baseline still supports metadata cleanup, manual regions, and manual audio intervals.
- Automatic scanners make reviewable proposals, not claims that all privacy risks were found.
- A stale or mismatched plan digest is rejected in constant time before execution.
- Unverified required checks produce `needs_review` or `failed`, never `completed`.
- Risk IDs, plans, action ordering, evidence selection, and public JSON serialization are deterministic for the same input and effective configuration.
- The public share package and private review directory are physically and logically separated.
- Do not push, open a Pull Request, publish, deploy, or create a release unless the user separately authorizes it.

---

## File structure

New Python package:

```text
src/videoscope/privacy/
  __init__.py              public privacy-domain exports
  errors.py                structured sanitized privacy workflow errors
  models.py                versioned risk, decision, plan, artifact and report models
  serialization.py         canonical UTF-8 JSON and atomic writers
  profiles.py              versioned share-audience profiles
  scanners.py              scanner protocol, runner and failure isolation
  metadata.py              metadata privacy scanner
  manual.py                manual visual/audio input validation
  visual.py                anonymous face and QR/barcode CPU scanners
  text.py                  optional OCR suspicious-text scanner
  planner.py               deterministic reviewed-risk to redaction-plan mapping
  commands.py              FFmpeg argument builders for preview/audio/remux
  renderer.py              bounded streaming visual-frame redaction
  executor.py              staged execution and atomic artifacts
  verification.py          privacy-specific output rescan and status gate
  pipeline.py              scan, review, prepare, confirm, cancel and result orchestration
```

New Web orchestration:

```text
src/videoscope/web/privacy_jobs.py   persisted privacy-job state machine
```

New React components:

```text
web/src/components/PrivacyView.tsx
web/src/components/PrivacyRiskList.tsx
web/src/components/PrivacyTimeline.tsx
web/src/components/PrivacyOverlayEditor.tsx
web/src/components/PrivacyPlanReview.tsx
web/src/components/PrivacyResult.tsx
```

New focused tests mirror those modules under `tests/privacy/`, plus API, CLI,
fixture, distribution, and component tests in the existing suites.

---

### Task 1: Formal scope, privacy domain models, and canonical JSON

**Files:**
- Create: `src/videoscope/privacy/__init__.py`
- Create: `src/videoscope/privacy/errors.py`
- Create: `src/videoscope/privacy/models.py`
- Create: `src/videoscope/privacy/serialization.py`
- Create: `tests/privacy/__init__.py`
- Create: `tests/privacy/test_models.py`
- Create: `tests/privacy/test_serialization.py`
- Modify: `docs/product-spec.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`
- Create: `docs/privacy-schema.md`

**Interfaces:**
- Produces: `NormalizedBox`, `PrivacyRisk`, `PrivacyRiskMap`, `PrivacyReviewDecision`, `PrivacyEffectiveConfig`, `PrivacyAction`, `PrivacyPlan`, `PrivacyArtifact`, `PrivacyChangeLog`, `PrivacyVerificationCheck`, `PrivacyVerificationReport`, `PrivacyTechnicalReport`.
- Produces: structured `PrivacyError`, `PrivacyInputError`, `PrivacyArtifactError`, `PrivacyPlanError`, `PrivacyConfirmationError`, `PrivacyMediaError`, and `PrivacyCancelledError` with sanitized public messages.
- Produces: `make_privacy_risk_id(input_hash, scanner_id, risk_type, start_seconds, end_seconds, box) -> str`, `make_privacy_plan_digest(input_hash, profile, effective_config, risks, actions, artifacts) -> str`, plus canonical model-specific JSON reader and writer helpers.
- Consumes: `ResolveModel` validation style and existing atomic writer conventions without changing `AnalysisReport`.

- [ ] **Step 1: Write model RED tests**

Add tests that demonstrate the public API and deterministic identity:

```python
def test_privacy_risk_id_is_deterministic() -> None:
    box = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5)
    first = make_privacy_risk_id(
        input_hash="a" * 64,
        scanner_id="qr_barcode_region",
        risk_type=PrivacyRiskType.QR_CODE,
        start_seconds=1.25,
        end_seconds=2.5,
        box=box,
    )
    second = make_privacy_risk_id(
        input_hash="a" * 64,
        scanner_id="qr_barcode_region",
        risk_type=PrivacyRiskType.QR_CODE,
        start_seconds=1.25,
        end_seconds=2.5,
        box=box,
    )
    assert first == second
    assert first.startswith("privacy_risk_")


def test_normalized_box_rejects_inverted_coordinates() -> None:
    with pytest.raises(ValueError):
        NormalizedBox(x_min=0.6, y_min=0.2, x_max=0.4, y_max=0.5)


def test_risk_map_sorts_risks_deterministically() -> None:
    risk_map = PrivacyRiskMap.model_validate(make_unsorted_risk_map_payload())
    assert [risk.id for risk in risk_map.risks] == expected_sorted_ids()
```

- [ ] **Step 2: Run model tests and verify RED**

Run:

```powershell
python -m pytest tests/privacy/test_models.py -v
```

Expected: collection fails because `videoscope.privacy.models` does not exist.

- [ ] **Step 3: Implement strict versioned models**

Use exact enum values and top-level contracts:

```python
PRIVACY_SCHEMA_VERSION = "0.1"


class PrivacyRiskType(StrEnum):
    METADATA = "metadata"
    FACE_REGION = "face_region"
    QR_CODE = "qr_code"
    BARCODE = "barcode"
    SUSPICIOUS_TEXT = "suspicious_text"
    MANUAL_VISUAL = "manual_visual"
    MANUAL_AUDIO = "manual_audio"


class PrivacyDecision(StrEnum):
    UNREVIEWED = "unreviewed"
    ALLOW = "allow"
    REDACT = "redact"


class RedactionStyle(StrEnum):
    BLUR = "blur"
    PIXELATE = "pixelate"
    SOLID_FILL = "solid_fill"
    CROP = "crop"
    MUTE = "mute"
    REMOVE_METADATA = "remove_metadata"


class PrivacyActionKind(StrEnum):
    REMOVE_METADATA = "remove_metadata"
    CROP = "crop"
    VISUAL_REDACTION = "visual_redaction"
    AUDIO_MUTE = "audio_mute"
    REMUX = "remux"
    VERIFY = "verify"


class PrivacyJobOutcome(StrEnum):
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    FAILED = "failed"
```

`PrivacyRisk` has these exact public fields: `id`, `scanner_id`,
`scanner_version`, `risk_type`, `title`, `public_description`, `severity`,
`confidence`, `start_seconds`, `end_seconds`, `box`, `track_id`,
`metadata_scope`, `metadata_key`, `recommended_style`, `decision`, `style`,
`limitations`, `evidence`, and `private_evidence`. `private_evidence` is legal
only in the private risk map and is removed when constructing a public summary.
`PrivacyReviewDecision` contains `risk_id`, `decision`, `style`, `edited_box`,
and `reviewed_at`; the timestamp is an audit field and is excluded from
deterministic plan identity. `PrivacyAction` contains `id`, `version`, `kind`,
`start_seconds`, `end_seconds`, `box`, `parameters`, `changes_semantics`, and
`requires_confirmation`.

All models use `ConfigDict(extra="forbid")`. All seconds are finite and
non-negative. Boxes are normalized to `[0, 1]`, have positive area, and use
`x_min < x_max`, `y_min < y_max`. A `REDACT` decision requires an applicable
style; `ALLOW` forbids a redaction style. `CROP` is valid only for one static
full-duration box. Public artifact paths are relative, forward-slash paths.

- [ ] **Step 4: Write serialization RED tests**

Cover Chinese text, stable key ordering, round trip, replacement of an existing
destination, Unicode directories, and absence of temporary residue:

```python
@pytest.mark.parametrize(
    ("writer", "reader", "value_factory"),
    [
        (write_privacy_risk_map_json, read_privacy_risk_map_json, make_risk_map),
        (write_privacy_plan_json, read_privacy_plan_json, make_plan),
        (
            write_privacy_technical_report_json,
            read_privacy_technical_report_json,
            make_report,
        ),
    ],
)
def test_atomic_privacy_writers_replace_in_unicode_directory(
    tmp_path: Path,
    writer: Callable[[object, Path], None],
    reader: Callable[[Path], object],
    value_factory: Callable[[], object],
) -> None:
    destination = tmp_path / "中文 目录" / "result.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    value = value_factory()
    writer(value, destination)
    assert reader(destination) == value
    assert list(destination.parent.glob("*.tmp")) == []
```

- [ ] **Step 5: Run serialization tests and verify RED**

Run `python -m pytest tests/privacy/test_serialization.py -v`.

Expected: import failure for missing serialization helpers.

- [ ] **Step 6: Implement canonical serialization and update scope docs**

Use UTF-8, `ensure_ascii=False`, sorted object keys, stable array ordering,
`allow_nan=False`, newline termination, revalidation before writing, and atomic
replacement. Document Safe Sharing as a new Resolve line, not a v0.1 ability,
and link `docs/privacy-schema.md` from architecture and roadmap.

- [ ] **Step 7: Run focused and repository validation**

Run:

```powershell
python -m pytest tests/privacy/test_models.py tests/privacy/test_serialization.py -v
python scripts/validate.py
```

Expected: both commands pass; existing v0.1 schema tests remain unchanged.

- [ ] **Step 8: Commit**

```powershell
git add docs src/videoscope/privacy tests/privacy
git commit -m "feat: define Safe Sharing domain contract"
```

---

### Task 2: Audience profiles, metadata scanner, and artifact isolation

**Files:**
- Create: `src/videoscope/privacy/profiles.py`
- Create: `src/videoscope/privacy/metadata.py`
- Create: `src/videoscope/privacy/artifacts.py`
- Create: `tests/privacy/test_profiles.py`
- Create: `tests/privacy/test_metadata.py`
- Create: `tests/privacy/test_artifacts.py`
- Modify: `src/videoscope/video/probe.py`

**Interfaces:**
- Consumes: Task 1 `PrivacyRisk`, `PrivacyRiskMap`, and deterministic ID helper.
- Produces: `ShareAudienceProfile`, `PrivateProbeSummary`, `list_share_audience_profiles() -> tuple[ShareAudienceProfile, ...]`, `get_share_audience_profile(profile_id: str) -> ShareAudienceProfile`, `MetadataPrivacyScanner.scan(metadata: PrivateProbeSummary, input_hash: str, profile: ShareAudienceProfile) -> list[PrivacyRisk]`, and `PrivacyArtifactLayout.create(root: Path) -> PrivacyArtifactLayout`.

- [ ] **Step 1: Write profile and metadata RED tests**

Assert exact profile IDs and metadata policies:

```python
def test_profile_catalog_is_versioned_and_deterministic() -> None:
    profiles = list_share_audience_profiles()
    assert [profile.id for profile in profiles] == [
        "public",
        "work_client",
        "school",
        "family",
        "external_ai",
    ]
    assert all(profile.version == "1" for profile in profiles)


def test_metadata_scanner_reports_private_global_stream_and_chapter_tags() -> None:
    risks = MetadataPrivacyScanner().scan(
        metadata=make_tagged_probe_summary(),
        input_hash="a" * 64,
        profile=get_share_audience_profile("public"),
    )
    observed = {(risk.metadata_scope, risk.metadata_key) for risk in risks}
    assert ("global", "location") in observed
    assert ("video_stream", "author") in observed
    assert ("chapter", "title") in observed
```

- [ ] **Step 2: Run focused tests and verify RED**

Run `python -m pytest tests/privacy/test_profiles.py tests/privacy/test_metadata.py -v`.

Expected: missing module failures.

- [ ] **Step 3: Implement immutable profiles and a sanitized probe summary**

Profiles declare forbidden metadata categories, required manual review categories,
default visual styles, QR handling, and whether a final human review is mandatory.
Extend probe internals to retain only structured tag keys and sanitized values in
the private job context. Do not add raw probe output to `VideoMetadata` or public
reports.

- [ ] **Step 4: Write artifact-isolation RED tests**

```python
def test_private_evidence_cannot_be_published(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    private = layout.private_root / "evidence" / "raw.png"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private")
    with pytest.raises(PrivacyArtifactError):
        layout.public_relative_path(private)


def test_share_manifest_rejects_absolute_and_sensitive_paths(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"evidence": str(tmp_path.resolve())})
```

- [ ] **Step 5: Run artifact tests and verify RED**

Run `python -m pytest tests/privacy/test_artifacts.py -v`.

Expected: missing artifact layout and error types.

- [ ] **Step 6: Implement physical private/share separation**

Create only these roots beneath a validated job root:

```text
privacy-review-private/
share-package/
```

Resolve paths before reads and writes, reject link-like escapes, allowlist public
filenames, and recursively scan JSON/text public artifacts for absolute path,
username, GPS, and raw private-field keys before publication.

- [ ] **Step 7: Verify**

Run:

```powershell
python -m pytest tests/privacy/test_profiles.py tests/privacy/test_metadata.py tests/privacy/test_artifacts.py -v
python scripts/validate.py
```

Expected: pass with no network access.

- [ ] **Step 8: Commit**

```powershell
git add src/videoscope/privacy src/videoscope/video/probe.py tests/privacy
git commit -m "feat: scan metadata privacy and isolate share artifacts"
```

---

### Task 3: Scanner protocol, failure isolation, and manual risks

**Files:**
- Create: `src/videoscope/privacy/scanners.py`
- Create: `src/videoscope/privacy/manual.py`
- Create: `tests/privacy/test_scanners.py`
- Create: `tests/privacy/test_manual.py`

**Interfaces:**
- Produces: `PrivacyScanContext`, `PrivacyScannerRequirements`, `PrivacyScannerStatus`, `PrivacyScanner` protocol, `PrivacyScannerExecution`, `PrivacyScannerRegistry`, `PrivacyScannerRunner`.
- Produces: `ManualVisualRegionInput`, `ManualAudioIntervalInput`, `build_manual_visual_risk(input_hash: str, value: ManualVisualRegionInput) -> PrivacyRisk`, and `build_manual_audio_risk(input_hash: str, value: ManualAudioIntervalInput) -> PrivacyRisk`.
- Consumes: Task 1 models and Task 2 profiles.

- [ ] **Step 1: Write scanner-runner RED tests**

```python
def test_one_scanner_failure_does_not_hide_other_risks() -> None:
    runner = PrivacyScannerRunner((FailingScanner(), OneRiskScanner()))
    result = runner.run(make_scan_context(), make_scanner_configs())
    assert [execution.status for execution in result.executions] == [
        PrivacyScannerStatus.SCANNER_ERROR,
        PrivacyScannerStatus.OK,
    ]
    assert [risk.scanner_id for risk in result.risks] == ["one_risk"]


def test_registry_rejects_duplicate_scanner_id() -> None:
    registry = PrivacyScannerRegistry()
    registry.register(OneRiskScanner())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(OneRiskScanner())
```

- [ ] **Step 2: Run scanner tests and verify RED**

Run `python -m pytest tests/privacy/test_scanners.py -v`.

Expected: missing scanner protocol.

- [ ] **Step 3: Implement sequential deterministic scanner orchestration**

The protocol is:

```python
class PrivacyScanner(Protocol):
    id: str
    display_name: str
    version: str
    description: str
    requirements: PrivacyScannerRequirements
    config_model: type[BaseModel]

    def scan(self, context: PrivacyScanContext, config: BaseModel) -> list[PrivacyRisk]:
        raise NotImplementedError
```

The runner catches ordinary scanner exceptions, sanitizes paths and private text,
records `scanner_error`, and continues. It does not catch `KeyboardInterrupt` or
`SystemExit`.

- [ ] **Step 4: Write manual-risk RED tests**

```python
def test_manual_visual_risk_preserves_reviewed_box_and_interval() -> None:
    risk = build_manual_visual_risk(
        input_hash="b" * 64,
        value=ManualVisualRegionInput(
            start_seconds=2.0,
            end_seconds=4.0,
            box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4),
            style=RedactionStyle.PIXELATE,
        ),
    )
    assert risk.decision is PrivacyDecision.REDACT
    assert risk.style is RedactionStyle.PIXELATE


def test_manual_audio_interval_rejects_visual_style() -> None:
    with pytest.raises(ValueError):
        ManualAudioIntervalInput(
            start_seconds=1.0,
            end_seconds=2.0,
            style=RedactionStyle.BLUR,
        )
```

- [ ] **Step 5: Run manual tests and verify RED**

Run `python -m pytest tests/privacy/test_manual.py -v`.

Expected: missing manual input types.

- [ ] **Step 6: Implement manual risk construction and merge rules**

Manual risks use deterministic IDs, explicit intervals, and normalized boxes.
Reject boxes outside the frame, zero-duration audio intervals, unsupported style
combinations, and crop actions that are not static full-duration rectangles.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
python -m pytest tests/privacy/test_scanners.py tests/privacy/test_manual.py -v
python scripts/validate.py
git add src/videoscope/privacy tests/privacy
git commit -m "feat: add privacy scanner protocol and manual risks"
```

---

### Task 4: Anonymous face and QR/barcode CPU proposals

**Files:**
- Create: `src/videoscope/privacy/visual.py`
- Create: `tests/privacy/test_visual.py`
- Modify: `src/videoscope/privacy/scanners.py`

**Interfaces:**
- Produces: `AnonymousFaceScanner`, `QrBarcodeScanner`, `VisualTrack`, and `track_regions(observations, max_gap_seconds, minimum_iou) -> tuple[VisualTrack, ...]`.
- Consumes: sampled frame paths and timestamps from `PrivacyScanContext`.

- [ ] **Step 1: Write pure tracking RED tests**

```python
def test_track_regions_keeps_anonymous_id_through_short_occlusion() -> None:
    tracks = track_regions(
        observations=make_moving_region_observations(with_one_missing_sample=True),
        max_gap_seconds=0.35,
        minimum_iou=0.25,
    )
    assert len(tracks) == 1
    assert tracks[0].anonymous_id == "face_track_01"
    assert tracks[0].has_gap is True


def test_scene_boundary_starts_a_new_visual_track() -> None:
    tracks = track_regions(
        observations=make_regions_across_scene_boundary(),
        max_gap_seconds=0.35,
        minimum_iou=0.25,
    )
    assert [track.anonymous_id for track in tracks] == [
        "face_track_01",
        "face_track_02",
    ]
```

- [ ] **Step 2: Run tracking tests and verify RED**

Run `python -m pytest tests/privacy/test_visual.py -k track -v`.

Expected: missing tracking function.

- [ ] **Step 3: Implement deterministic scene-local tracking**

Match observations by time continuity, intersection-over-union, and center
distance. Do not use embeddings or identity attributes. Mark gaps explicitly so
the planner can expand protection or require review.

- [ ] **Step 4: Write scanner RED tests**

Use injected fake OpenCV adapters for deterministic unit tests and one optional
real OpenCV QR integration test:

```python
def test_qr_scanner_returns_decoded_region_without_public_payload() -> None:
    scanner = QrBarcodeScanner(adapter=FakeQrAdapter(payload="https://private.example"))
    risks = scanner.scan(make_visual_context(), QrBarcodeConfig())
    assert risks[0].risk_type is PrivacyRiskType.QR_CODE
    assert risks[0].private_evidence["decoded_payload"] == "https://private.example"
    assert "private.example" not in risks[0].public_description


def test_face_scanner_uses_anonymous_track_names() -> None:
    scanner = AnonymousFaceScanner(adapter=FakeFaceAdapter())
    risks = scanner.scan(make_visual_context(), AnonymousFaceConfig())
    assert risks[0].track_id == "face_track_01"
    assert "identity" not in risks[0].model_dump_json().lower()
```

- [ ] **Step 5: Run scanner tests and verify RED**

Run `python -m pytest tests/privacy/test_visual.py -v`.

Expected: missing scanner classes.

- [ ] **Step 6: Implement real local adapters and conservative limits**

Use `cv2.CascadeClassifier` with the OpenCV-packaged frontal-face cascade and
`cv2.QRCodeDetector` for QR proposals. Do not download cascades or weights.
Configurations include minimum size, scale factor, neighbor count, tracking IoU,
maximum gap, guard seconds, and maximum risks. Decode payloads are stored only in
private evidence.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
python -m pytest tests/privacy/test_visual.py -v
python scripts/validate.py
git add src/videoscope/privacy tests/privacy
git commit -m "feat: propose anonymous face and QR privacy regions"
```

---

### Task 5: Optional OCR suspicious-text proposals

**Files:**
- Create: `src/videoscope/privacy/text.py`
- Create: `tests/privacy/test_text.py`
- Modify: `src/videoscope/ai/ocr.py`
- Create: `docs/detectors/privacy-text.md`

**Interfaces:**
- Produces: `SuspiciousTextScanner`, `SuspiciousTextConfig`, `classify_private_text(text, locale)`, and scene-local OCR region tracks.
- Consumes: existing shared `OCRProvider` and `ModelRuntimeManager`; tests use `FakeOCRProvider` only.

- [ ] **Step 1: Write classifier and scanner RED tests**

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Call 13800138000", SuspiciousTextKind.PHONE),
        ("person@example.com", SuspiciousTextKind.EMAIL),
        ("Order total 128.00", None),
    ],
)
def test_private_text_classifier(
    text: str, expected: SuspiciousTextKind | None
) -> None:
    assert classify_private_text(text, locale="zh-CN") is expected


def test_scanner_keeps_raw_ocr_text_private() -> None:
    scanner = SuspiciousTextScanner(FakeOCRProvider.with_text("person@example.com"))
    risk = scanner.scan(make_text_context(), SuspiciousTextConfig())[0]
    assert risk.private_evidence["ocr_text"] == "person@example.com"
    assert "person@example.com" not in risk.public_description
```

- [ ] **Step 2: Run and verify RED**

Run `python -m pytest tests/privacy/test_text.py -v`.

Expected: missing privacy text scanner.

- [ ] **Step 3: Implement conservative pattern classification and tracking**

Normalize Unicode, reject isolated low-confidence OCR, and classify explicit
phone, email, address, account, verification-code, path, and URL patterns.
Track regions scene-locally by box overlap, normalized text similarity, and time
continuity. Public descriptions name the category, not the raw text.

- [ ] **Step 4: Test missing OCR and provider failure**

```python
def test_missing_ocr_is_skipped_with_manual_fallback() -> None:
    result = run_text_scanner_without_ocr()
    assert result.execution.status is PrivacyScannerStatus.SKIPPED
    assert result.execution.fallback == "manual_visual_region"


def test_ocr_failure_does_not_remove_metadata_or_manual_risks() -> None:
    result = run_all_scanners_with_failing_ocr()
    assert any(item.risk_type is PrivacyRiskType.METADATA for item in result.risks)
    assert any(item.risk_type is PrivacyRiskType.MANUAL_VISUAL for item in result.risks)
```

- [ ] **Step 5: Run full text tests and verify GREEN**

Run `python -m pytest tests/privacy/test_text.py tests/ai/test_ocr_runtime.py -v`.

Expected: pass without network, PaddleOCR, or model download.

- [ ] **Step 6: Document limitations and commit**

Document OCR errors, language limitations, false positives, missed text, manual
review, and the explicit optional dependency boundary.

```powershell
python scripts/validate.py
git add src/videoscope/privacy src/videoscope/ai tests/privacy docs/detectors/privacy-text.md
git commit -m "feat: propose optional OCR privacy risks"
```

---

### Task 6: Reviewed-risk planner, digest, and preview contract

**Files:**
- Create: `src/videoscope/privacy/planner.py`
- Create: `src/videoscope/privacy/commands.py`
- Create: `tests/privacy/test_planner.py`
- Create: `tests/privacy/test_commands.py`
- Modify: `src/videoscope/privacy/models.py`
- Modify: `docs/privacy-schema.md`

**Interfaces:**
- Produces: `build_privacy_plan(risk_map, reviews, profile, config) -> PrivacyPlan`.
- Produces: `build_privacy_preview_arguments(plan, source, output, ffmpeg) -> list[str]`.
- Consumes: Task 1 models and Task 2 profiles.

- [ ] **Step 1: Write planner RED tests**

```python
def test_plan_rejects_unreviewed_high_risk() -> None:
    with pytest.raises(PrivacyPlanError, match="unreviewed high-risk"):
        build_privacy_plan(
            risk_map=make_risk_map_with_unreviewed_high_risk(),
            reviews=(),
            profile=get_share_audience_profile("public"),
            config=PrivacyEffectiveConfig(),
        )


def test_plan_digest_covers_reviewed_regions_and_effective_config() -> None:
    baseline = make_reviewed_plan()
    changed_box = baseline.model_copy(update={"actions": changed_box_actions(baseline)})
    changed_config = baseline.model_copy(
        update={
            "effective_config": baseline.effective_config.model_copy(
                update={"guard_pixels": 24}
            )
        }
    )
    assert baseline.digest != recompute_digest(changed_box)
    assert baseline.digest != recompute_digest(changed_config)
```

- [ ] **Step 2: Run planner tests and verify RED**

Run `python -m pytest tests/privacy/test_planner.py -v`.

Expected: missing planner.

- [ ] **Step 3: Implement deterministic action planning**

Action order is metadata removal, crop, visual tracks ordered by interval and ID,
audio intervals, remux, verification. Merge only adjacent actions with identical
style and compatible regions. Require confirmation for every content-changing
action. The digest includes every confirmation-relevant field named in the
design specification.

- [ ] **Step 4: Write command-builder RED tests**

```python
def test_preview_command_uses_argument_array_and_exact_duration(tmp_path: Path) -> None:
    arguments = build_privacy_preview_arguments(
        plan=make_reviewed_plan(preview_seconds=5.0),
        source=tmp_path / "中文 source.mp4",
        output=tmp_path / "preview output.mp4",
        ffmpeg="ffmpeg",
    )
    assert arguments[0] == "ffmpeg"
    assert "-t" in arguments
    assert arguments[arguments.index("-t") + 1] == "5"
    assert "shell=True" not in arguments
```

- [ ] **Step 5: Run command tests and verify RED**

Run `python -m pytest tests/privacy/test_commands.py -v`.

Expected: missing command builder.

- [ ] **Step 6: Implement preview/remux/audio argument builders**

Builders return complete argument arrays, never quoted shell strings. They add
`-map_metadata -1`, per-stream metadata suppression, `-map_chapters -1`, bounded
preview duration, explicit mappings, and safe output paths. Audio mute uses a
deterministically ordered `volume=enable='between(t,start,end)':volume=0` chain.

- [ ] **Step 7: Verify and commit**

```powershell
python -m pytest tests/privacy/test_planner.py tests/privacy/test_commands.py -v
python scripts/validate.py
git add src/videoscope/privacy tests/privacy docs/privacy-schema.md
git commit -m "feat: plan and preview privacy redactions"
```

---

### Task 7: Streaming visual redaction renderer

**Files:**
- Create: `src/videoscope/privacy/renderer.py`
- Create: `tests/privacy/test_renderer.py`
- Modify: `src/videoscope/privacy/commands.py`

**Interfaces:**
- Produces: `VisualRedactionRenderer.render(source, output, plan, cancellation) -> VisualRenderResult`.
- Produces pure functions `interpolate_box(before, after, timestamp_seconds, guard_ratio, gap_requires_expansion) -> NormalizedBox`, `expand_box(box, guard_ratio) -> NormalizedBox`, and frame operations `apply_blur(frame, box, kernel_size)`, `apply_pixelate(frame, box, block_size)`, and `apply_solid_fill(frame, box, color)` returning NumPy arrays.
- Consumes: Task 6 ordered visual actions.

- [ ] **Step 1: Write pure image RED tests**

```python
def test_interpolated_box_expands_across_track_gap() -> None:
    box = interpolate_box(
        before=key_box(1.0, 0.1, 0.1, 0.2, 0.2),
        after=key_box(2.0, 0.2, 0.1, 0.3, 0.2),
        timestamp_seconds=1.5,
        guard_ratio=0.05,
        gap_requires_expansion=True,
    )
    assert box.x_min < 0.15
    assert box.x_max > 0.25


def test_pixelate_changes_only_the_selected_region() -> None:
    source = make_checkerboard_frame()
    output = apply_pixelate(source.copy(), pixel_box(8, 8, 24, 24), block_size=8)
    assert np.array_equal(output[:8, :8], source[:8, :8])
    assert not np.array_equal(output[8:24, 8:24], source[8:24, 8:24])
```

- [ ] **Step 2: Run pure tests and verify RED**

Run `python -m pytest tests/privacy/test_renderer.py -k "interpolated or pixelate" -v`.

Expected: missing renderer functions.

- [ ] **Step 3: Implement deterministic box and frame operations**

Convert normalized boxes only after reading actual frame dimensions. Clamp
expanded boxes to frame bounds, reject empty boxes, use named interpolation and
kernel rules from `PrivacyEffectiveConfig`, and preserve pixels outside selected
regions exactly in unit tests.

- [ ] **Step 4: Write bounded-streaming RED test**

```python
def test_renderer_streams_frames_without_collecting_video(tmp_path: Path) -> None:
    reader = CountingFrameReader(total_frames=120)
    writer = CountingFrameWriter()
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader, writer_factory=lambda _: writer
    )
    result = renderer.render(
        Path("source.mp4"), tmp_path / "visual.mp4", make_visual_plan(), never_cancel
    )
    assert result.frames_read == 120
    assert result.maximum_buffered_frames <= 2
    assert writer.frames_written == 120
```

- [ ] **Step 5: Run streaming test and verify RED**

Run `python -m pytest tests/privacy/test_renderer.py -k streams -v`.

Expected: missing renderer orchestration.

- [ ] **Step 6: Implement FFmpeg-backed bounded frame streaming**

Use one FFmpeg rawvideo decoder process and one encoder process connected through
bounded Python frame buffers. Never read the full video or all decoded frames
into memory. Check cancellation between frames, bound stderr, terminate child
processes safely, and delete incomplete output on failure. Crop is accepted only
as one prevalidated static full-duration rectangle and is applied before region
redactions.

- [ ] **Step 7: Verify and commit**

```powershell
python -m pytest tests/privacy/test_renderer.py -v
python scripts/validate.py
git add src/videoscope/privacy tests/privacy
git commit -m "feat: stream visual privacy redactions"
```

---

### Task 8: Staged executor, audio muting, metadata removal, and artifacts

**Files:**
- Create: `src/videoscope/privacy/executor.py`
- Create: `tests/privacy/test_executor.py`
- Modify: `src/videoscope/privacy/renderer.py`
- Modify: `src/videoscope/privacy/artifacts.py`

**Interfaces:**
- Produces: `NativePrivacyExecutor.execute(plan, source, workspace, cancellation) -> PrivacyNativeResult`.
- Consumes: Task 6 command builders, Task 7 visual renderer, and Task 2 artifact layout.

- [ ] **Step 1: Write executor RED tests**

```python
def test_executor_never_writes_source_and_publishes_expected_artifacts(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path / "原始 source.mp4")
    before = sha256_file(source)
    result = make_executor().execute(
        make_reviewed_plan(), source, tmp_path / "job", never_cancel
    )
    assert sha256_file(source) == before
    assert result.staged_video.name == "share-safe.mp4"
    assert result.change_log.source_modified is False


def test_executor_cancellation_removes_incomplete_public_video(tmp_path: Path) -> None:
    with pytest.raises(PrivacyCancelledError):
        cancelling_executor().execute(
            make_reviewed_plan(), Path("source.mp4"), tmp_path, cancel_after_first_frame
        )
    assert not (tmp_path / "share-package" / "share-safe.mp4").exists()
```

- [ ] **Step 2: Run executor tests and verify RED**

Run `python -m pytest tests/privacy/test_executor.py -v`.

Expected: missing executor.

- [ ] **Step 3: Implement staged execution**

Use a unique staging directory inside the job root. Render visual video first,
then remux reviewed audio, strip metadata, add fast-start, and probe the candidate.
Capture executable name, FFmpeg version, action versions, exact public parameters,
and affected intervals in `changes.json`; never serialize source absolute paths.

- [ ] **Step 4: Add real audio/metadata behavior tests**

Generate a short tagged audio/video fixture at test time and assert:

```python
def test_real_executor_mutes_only_reviewed_interval_and_strips_tags(
    generated_privacy_av: Path,
) -> None:
    result = real_executor().execute(
        make_audio_metadata_plan(), generated_privacy_av, fresh_job_root(), never_cancel
    )
    probe = probe_private_test_output(result.staged_video)
    assert probe.global_tags.get("location") is None
    assert probe.stream_tags.get("author") is None
    assert probe.chapter_tags == ()
    assert rms_energy(result.staged_video, 1.0, 2.0) < 0.01
    assert rms_energy(result.staged_video, 2.5, 3.5) > 0.05
```

- [ ] **Step 5: Run real tests and verify GREEN**

Run `python -m pytest tests/privacy/test_executor.py -m ffmpeg -v`.

Expected: pass when FFmpeg is available; otherwise skip with one explicit reason.

- [ ] **Step 6: Verify and commit**

```powershell
python scripts/validate.py
git add src/videoscope/privacy tests/privacy
git commit -m "feat: execute staged privacy transformations"
```

---

### Task 9: Privacy verification gate and conservative status

**Files:**
- Create: `src/videoscope/privacy/verification.py`
- Create: `tests/privacy/test_verification.py`
- Modify: `src/videoscope/privacy/models.py`
- Modify: `src/videoscope/privacy/artifacts.py`

**Interfaces:**
- Produces: `PrivacyVerifier.verify(source, candidate, plan, private_context) -> PrivacyVerificationReport`.
- Consumes: existing `AnalysisPipeline` for black/freeze regression, privacy scanners for applicable rescans, and Task 8 output.

- [ ] **Step 1: Write verification RED tests**

```python
def test_unverified_required_check_never_completes() -> None:
    report = PrivacyVerifier(qr_rescanner=UnavailableQrRescanner()).verify(
        make_source(),
        make_candidate(),
        make_plan_with_qr_redaction(),
        make_private_context(),
    )
    assert report.status is PrivacyJobOutcome.NEEDS_REVIEW
    assert check(report, "qr_redaction").status is VerificationStatus.NEEDS_REVIEW


def test_share_manifest_with_private_text_fails() -> None:
    report = verify_public_manifest({"ocr_text": "person@example.com"})
    assert report.status is PrivacyJobOutcome.FAILED
    assert check(report, "public_artifact_privacy").status is VerificationStatus.FAILED
```

- [ ] **Step 2: Run verification tests and verify RED**

Run `python -m pytest tests/privacy/test_verification.py -v`.

Expected: missing verifier.

- [ ] **Step 3: Implement independent checks and aggregate policy**

Checks are `decodable`, `duration`, `streams`, `profile`, `metadata`,
`visual_coverage`, `qr_redaction`, `text_redaction`, `audio_mute`,
`black_regression`, `freeze_regression`, and `public_artifact_privacy`.
Aggregate status rules are exact:

```python
if any(check.status is FAILED for check in required_checks):
    return FAILED
if any(check.status is NEEDS_REVIEW for check in required_checks):
    return NEEDS_REVIEW
return COMPLETED
```

An optional scanner error cannot erase successful CPU checks but makes the result
`partial` or `needs_review` according to the active audience profile.

- [ ] **Step 4: Add temporal coverage and rescan tests**

Assert coverage at every sampled timestamp in the selected interval, not only at
the midpoint. Inject one-frame gaps, still-decodable QR output, OCR recovery,
unexpected sound energy, and source/output detector regressions.

- [ ] **Step 5: Verify and commit**

```powershell
python -m pytest tests/privacy/test_verification.py -v
python scripts/validate.py
git add src/videoscope/privacy tests/privacy
git commit -m "feat: verify privacy share outputs conservatively"
```

---

### Task 10: End-to-end Safe Sharing pipeline and CLI

**Files:**
- Create: `src/videoscope/privacy/pipeline.py`
- Create: `tests/privacy/test_pipeline.py`
- Modify: `src/videoscope/cli.py`
- Modify: `tests/test_cli.py`
- Create: `docs/safe-sharing.md`

**Interfaces:**
- Produces: `SafeSharingConfig`, `PrivacyScanResult`, `PrivacyPreparation`, `PrivacyResult`, and `SafeSharingPipeline`.
- Produces CLI: `videoscope privacy INPUT --output OUTPUT`.
- Consumes Tasks 1–9.

- [ ] **Step 1: Write pipeline lifecycle RED tests**

```python
def test_pipeline_requires_review_then_exact_confirmation(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    scan = pipeline.scan(source=fixture_video(), config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, redact_all_high_risks(scan.risk_map))
    preparation = pipeline.prepare(reviewed.review_id)
    with pytest.raises(PrivacyConfirmationError):
        pipeline.confirm(preparation.preparation_id, "0" * 64)
    result = pipeline.confirm(preparation.preparation_id, preparation.plan.digest)
    assert result.status in {
        PrivacyJobOutcome.COMPLETED,
        PrivacyJobOutcome.NEEDS_REVIEW,
    }


def test_pipeline_does_not_execute_confirmation_twice(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    preparation = make_preparation(pipeline)
    first = pipeline.confirm(preparation.preparation_id, preparation.plan.digest)
    with pytest.raises(PrivacyConfirmationError, match="already consumed"):
        pipeline.confirm(preparation.preparation_id, preparation.plan.digest)
    assert first.execution_count == 1
```

- [ ] **Step 2: Run pipeline tests and verify RED**

Run `python -m pytest tests/privacy/test_pipeline.py -v`.

Expected: missing pipeline.

- [ ] **Step 3: Implement scan/review/prepare/confirm/discard orchestration**

Hash and probe once, sample once, persist risk map and decisions, issue opaque
preparation IDs, compare digests with `hmac.compare_digest`, clean unclaimed
staging, and atomically claim only verified public artifacts. Catch ordinary
scanner failures independently; do not catch `KeyboardInterrupt` or `SystemExit`.

- [ ] **Step 4: Write CLI RED tests**

Cover help, invalid config exit 2, media failure exit 3, internal failure exit 4,
scan-only non-interactive behavior, stable preview path, declined confirmation
cleanup, exact digest confirmation, Unicode path, and absence of absolute paths.

```python
def test_privacy_cli_scan_only_writes_private_risk_map(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["privacy", str(fixture_video()), "--output", str(tmp_path), "--scan-only"]
    )
    assert result.exit_code == 0
    assert (tmp_path / "privacy-review-private" / "risk-map.json").is_file()
    assert not (tmp_path / "share-package" / "share-safe.mp4").exists()
```

- [ ] **Step 5: Run CLI tests and verify RED**

Run `python -m pytest tests/test_cli.py -k privacy -v`.

Expected: command is missing.

- [ ] **Step 6: Implement CLI without duplicating pipeline logic**

Options include `--output`, `--audience`, `--config`, `--scan-only`,
`--review-file`, `--confirm-digest`, `--preview-only`, `--keep-workspace`,
`--quiet`, and optional OCR flags already governed by the shared model runtime.
No content-changing action occurs without a matching review and confirmation.

- [ ] **Step 7: Verify and commit**

```powershell
python -m pytest tests/privacy/test_pipeline.py tests/test_cli.py -k "privacy or SafeSharing" -v
python scripts/validate.py
git add src/videoscope/privacy src/videoscope/cli.py tests docs/safe-sharing.md
git commit -m "feat: add Safe Sharing pipeline and CLI"
```

---

### Task 11: Local Web API, persisted privacy jobs, SSE, and storage security

**Files:**
- Create: `src/videoscope/web/privacy_jobs.py`
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/app.py`
- Modify: `src/videoscope/web/storage.py`
- Create: `tests/web/test_privacy_api.py`
- Modify: `tests/web/test_storage.py`
- Create: `docs/privacy-api.md`

**Interfaces:**
- Produces: `PrivacyJobManager`, `PrivacyJobRecord`, `PrivacyJobEvent`, and API endpoints under `/api/privacy`.
- Consumes: Task 10 `SafeSharingPipeline`.

- [ ] **Step 1: Write API lifecycle RED tests**

Define and test these endpoints:

```text
GET    /api/privacy/profiles
POST   /api/privacy/jobs
GET    /api/privacy/jobs/{job_id}
GET    /api/privacy/jobs/{job_id}/events
GET    /api/privacy/jobs/{job_id}/risk-map
PUT    /api/privacy/jobs/{job_id}/review
POST   /api/privacy/jobs/{job_id}/prepare
GET    /api/privacy/jobs/{job_id}/plan
POST   /api/privacy/jobs/{job_id}/confirm
GET    /api/privacy/jobs/{job_id}/artifacts/{path}
GET    /api/privacy/jobs/{job_id}/private-artifacts/{path}
DELETE /api/privacy/jobs/{job_id}
```

```python
def test_privacy_job_reaches_review_then_confirmation(client: TestClient) -> None:
    created = upload_privacy_fixture(client)
    awaiting_review = wait_for_privacy_status(
        client, created["job_id"], "awaiting_review"
    )
    review_all(client, awaiting_review)
    prepared = client.post(f"/api/privacy/jobs/{created['job_id']}/prepare").json()
    confirmed = client.post(
        f"/api/privacy/jobs/{created['job_id']}/confirm",
        json={"plan_digest": prepared["plan_digest"]},
    )
    assert confirmed.status_code == 202
```

- [ ] **Step 2: Run API tests and verify RED**

Run `python -m pytest tests/web/test_privacy_api.py -v`.

Expected: endpoints return 404.

- [ ] **Step 3: Implement persisted monotonic job manager**

Use random 32-hex job IDs, the existing upload limit, CPU limiter, background
worker pattern, ordered sequence numbers, cancellation callback, retention, and
restart recovery. The manager submits execution exactly once. Error payloads are
sanitized and never contain private OCR evidence.

- [ ] **Step 4: Add path traversal and private/public authorization tests**

```python
def test_public_artifact_route_cannot_read_private_evidence(client: TestClient) -> None:
    job_id = completed_privacy_job(client)
    response = client.get(
        f"/api/privacy/jobs/{job_id}/artifacts/../privacy-review-private/evidence/raw.png"
    )
    assert response.status_code in {400, 404}


def test_private_artifact_route_allows_only_review_evidence(client: TestClient) -> None:
    job_id = awaiting_review_privacy_job(client)
    response = client.get(
        f"/api/privacy/jobs/{job_id}/private-artifacts/evidence/risk_01.png"
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_privacy_cleanup_cannot_follow_job_shaped_junction(tmp_path: Path) -> None:
    sentinel = create_outside_junction_sentinel(tmp_path)
    store = make_job_store(tmp_path)
    store.cleanup_orphans(now=future_time())
    assert sentinel.read_text(encoding="utf-8") == "keep"
```

- [ ] **Step 5: Run security tests and verify RED then GREEN**

Run:

```powershell
python -m pytest tests/web/test_privacy_api.py tests/web/test_storage.py -v
```

Expected after implementation: pass; unsupported symlink creation skips with one
explicit platform reason, while Windows junction coverage runs on Windows.

- [ ] **Step 6: Document API and commit**

```powershell
python scripts/validate.py
git add src/videoscope/web tests/web docs/privacy-api.md
git commit -m "feat: add local Safe Sharing web jobs"
```

---

### Task 12: React Safe Sharing workbench and recovery

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/api.test.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/App.test.tsx`
- Create: `web/src/components/PrivacyView.tsx`
- Create: `web/src/components/PrivacyView.test.tsx`
- Create: `web/src/components/PrivacyRiskList.tsx`
- Create: `web/src/components/PrivacyTimeline.tsx`
- Create: `web/src/components/PrivacyOverlayEditor.tsx`
- Create: `web/src/components/PrivacyPlanReview.tsx`
- Create: `web/src/components/PrivacyResult.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**
- Produces workbench mode `privacy`, query parameter `privacyJob`, typed API methods, and bilingual English/Simplified Chinese copy.
- Consumes Task 11 API and keeps literal `what912` invariant across locale switches.

- [ ] **Step 1: Write typed API and SSE RED tests**

```typescript
it("keeps one privacy event stream and ignores stale sequences", () => {
  const source = new FakeSource();
  const events: PrivacyJobEvent[] = [];
  const subscription = subscribeToPrivacyEvents("job", (event) => events.push(event), {
    sourceFactory: () => source,
  });
  source.emit({ sequence: 3, status: "processing", progress: 60 });
  source.emit({ sequence: 2, status: "planning", progress: 40 });
  expect(events.map((event) => event.sequence)).toEqual([3]);
  subscription.close();
});
```

- [ ] **Step 2: Run API tests and verify RED**

Run `cd web; npm test -- src/api.test.ts`.

Expected: missing privacy API functions.

- [ ] **Step 3: Implement strict TypeScript types and API client**

Add typed methods for all Task 11 endpoints. Normalize FastAPI string and
validation-array errors. One subscription lives for one job ID, retains the last
sequence through reconnects, and does not let delayed snapshots regress status.

- [ ] **Step 4: Write workbench interaction RED tests**

Test upload, audience selection, risk seek/highlight, allow/redact decisions,
manual region creation, region resize, audio interval, preview, digest confirm,
terminal artifacts, new task without DELETE, explicit delete, locale persistence,
recovery from `privacyJob`, detector error, no-risk state, and keyboard actions.

```tsx
it("reviews a risk, edits its box, previews and confirms the exact plan", async () => {
  render(<PrivacyView api={fakePrivacyApi()} locale="en" onJobChange={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Review face region" }));
  await userEvent.click(screen.getByRole("button", { name: "Redact" }));
  await userEvent.click(screen.getByRole("button", { name: "Generate preview" }));
  expect(await screen.findByText("Review the redaction preview")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Confirm and create share copy" }));
  expect(fakePrivacyApi().confirm).toHaveBeenCalledWith(JOB_ID, PLAN_DIGEST);
});
```

- [ ] **Step 5: Run component tests and verify RED**

Run `cd web; npm test -- src/components/PrivacyView.test.tsx src/App.test.tsx`.

Expected: privacy workbench is missing.

- [ ] **Step 6: Implement accessible responsive workbench**

Use semantic buttons, labels, focus-visible states, keyboard seeking, text plus
icons for severity, and reduced-motion support. Overlay rectangles are DOM
elements positioned over the video; pointer and keyboard edits update normalized
coordinates. Mobile uses a bottom drawer and states that precise frame editing is
better on desktop. Do not use Canvas for routine controls or duplicate scanner
logic in TypeScript.

- [ ] **Step 7: Verify frontend and sync package assets**

Run:

```powershell
cd web
npm test
npm run build
cd ..
git status --porcelain --untracked-files=all -- src/videoscope/web/static
```

Expected: all tests and build pass; the final status command lists only the
intentional index and hashed-asset replacement. Stage every listed static asset
in Step 8. A clean checkout of the resulting commit must produce an empty status
after the same build command.

- [ ] **Step 8: Commit**

```powershell
git add web src/videoscope/web/static
git commit -m "feat: add Safe Sharing review workbench"
```

---

### Task 13: Deterministic privacy fixtures and real end-to-end acceptance

**Files:**
- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`
- Create: `tests/privacy/test_fixture_privacy.py`
- Modify: `tests/test_fixture_factory.py`

**Interfaces:**
- Produces fixtures `privacy_tags_av.mp4`, `privacy_manual_visual.mp4`, `privacy_qr.mp4`, `privacy_text.mp4`, and `privacy_clean.mp4`.
- Consumes Tasks 8–12 public workflows.

- [ ] **Step 1: Write fixture-manifest RED tests**

```python
def test_privacy_fixtures_are_declared() -> None:
    manifest = load_manifest()
    assert set(manifest["privacy"]) == {
        "privacy_tags_av.mp4",
        "privacy_manual_visual.mp4",
        "privacy_qr.mp4",
        "privacy_text.mp4",
        "privacy_clean.mp4",
    }
```

- [ ] **Step 2: Run and verify RED**

Run `python -m pytest tests/test_fixture_factory.py -k privacy -v`.

Expected: privacy fixture section is absent.

- [ ] **Step 3: Implement deterministic generators**

Generate videos at no more than `320x180`, `10` or `12` fps, and no more than
`6` seconds. Use FFmpeg lavfi, Pillow, NumPy, and OpenCV only. Generate QR content
with the locally installed OpenCV QR encoder; if an unsupported OpenCV build lacks
the encoder, fail the generator with an actionable version message rather than
download an asset. Record exact risk intervals, boxes, text categories, audio
mute intervals, and timing tolerance in the manifest. Generated MP4 files remain
ignored by Git.

- [ ] **Step 4: Write real end-to-end RED tests**

```python
def test_real_manual_redaction_delivers_verified_share_package(
    privacy_manual_visual: Path,
    tmp_path: Path,
) -> None:
    source_hash = sha256_file(privacy_manual_visual)
    result = run_real_safe_sharing(
        source=privacy_manual_visual,
        output=tmp_path,
        reviews=manifest_reviews("privacy_manual_visual.mp4"),
    )
    assert result.verification.status is PrivacyJobOutcome.COMPLETED
    assert sha256_file(privacy_manual_visual) == source_hash
    assert (tmp_path / "share-package" / "share-safe.mp4").is_file()
    assert_public_package_has_no_private_fields(tmp_path / "share-package")
```

- [ ] **Step 5: Run real E2E and verify GREEN**

Run:

```powershell
python scripts/generate_test_videos.py --force
python -m pytest tests/privacy/test_fixture_privacy.py -v
```

Expected: all real FFmpeg paths pass; missing FFmpeg produces explicit skips in
pytest and an actionable generator error.

- [ ] **Step 6: Commit**

```powershell
git add scripts/generate_test_videos.py tests/fixtures/manifest.json tests/privacy/test_fixture_privacy.py tests/test_fixture_factory.py
git commit -m "test: add deterministic Safe Sharing fixtures"
```

---

### Task 14: Documentation, packaging, CI, smoke test, and release audit

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
- Create: `examples/safe_sharing.ps1`
- Create: `examples/safe_sharing.sh`
- Create: `examples/privacy-review.example.json`

**Interfaces:**
- Produces documented local CLI/Web workflow, packaging audit, clean-wheel smoke path, and a v0.4 development-line audit.
- Consumes every prior task.

- [ ] **Step 1: Write distribution and smoke RED tests**

Extend distribution tests to require privacy docs and current dashboard assets,
and forbid generated videos, private job roots, public packages, caches, absolute
personal paths, and unredacted evidence. Extend smoke tests to install the wheel,
run `videoscope --version`, `videoscope doctor`, generate one local fixture, run a
manual-region privacy workflow, and assert a verified `share-safe.mp4`.

- [ ] **Step 2: Run audit tests and verify RED**

Run:

```powershell
python -m pytest tests/test_distribution_audit.py tests/test_smoke_test.py -v
```

Expected: new privacy package requirements are absent.

- [ ] **Step 3: Update versions and public documentation**

Move the development line to Python `0.4.0.dev0`, CFF `0.4.0-dev0`, and npm
`0.4.0-dev.0`. State that Safe Sharing is local, opt-in, heuristic, and requires
human review. Include installation, CLI, Web, manual fallback, optional OCR,
private/share artifact separation, limitations, and deletion guidance. Do not
claim real-world accuracy or absolute safety.

- [ ] **Step 4: Extend CI with privacy fixtures and asset gates**

Linux and Windows, Python 3.11 and 3.12, install FFmpeg, generate deterministic
fixtures, run `scripts/validate.py`, run real privacy E2E, run frontend tests/build,
fail on tracked or untracked static drift, build wheel/sdist, audit archives, and
run clean-wheel smoke. Base jobs do not install AI/OCR or download models.

- [ ] **Step 5: Run full local release verification**

Run:

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

Expected: validation, frontend, build, archive audit, and clean-wheel smoke pass;
only intentional tracked changes remain before commit. If isolated build or wheel
installation needs uncached network dependencies, record it as a local blocker
and rely on the network-enabled packaging CI rather than weakening the smoke test.

- [ ] **Step 6: Manual browser acceptance**

Start the local server and verify upload, audience profile, risk timeline, manual
region, audio interval, preview comparison, digest confirmation, progress, result
download, refresh recovery, cancellation, new task, explicit delete, Simplified
Chinese/English switching, keyboard navigation, mobile layout, no console errors,
and the invariant `what912` mark. Record Firefox as a separate manual gate if the
available browser controller cannot launch it.

- [ ] **Step 7: Complete release audit and commit**

The audit separates passed items, human-review items, unverified items, risks,
and blockers. It records whether network access, model download, publication, or
deployment occurred.

```powershell
git add .
git commit -m "release: prepare Safe Sharing CPU MVP"
```

Do not push, tag, create a Pull Request, create a GitHub Release, publish PyPI,
or deploy without a separate explicit user authorization.

---

## Plan completion criteria

- All 14 tasks have independent implementer reports and scoped reviews.
- Every production behavior was preceded by a focused failing test and a recorded
  RED result.
- The broad final review reports no unresolved Critical or Important findings.
- Source hashes remain unchanged in real end-to-end tests.
- Public artifacts are proven separate from private evidence.
- The final branch is left local unless the user explicitly selects another
  integration option.
