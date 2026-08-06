"""Review-gated orchestration for the local Safe Sharing workflow."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import perf_counter
from types import MappingProxyType
from typing import Any, Protocol, Self, cast
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    model_validator,
)

from videoscope.ai.runtime import ModelRuntimeManager
from videoscope.privacy.artifacts import (
    PRIVATE_ROOT_NAME,
    PUBLIC_ROOT_NAME,
    PrivacyArtifactLayout,
    private_artifact_identity,
    resolve_existing_job_root,
    resolve_private_artifact,
    validate_public_manifest,
)
from videoscope.privacy.errors import (
    PrivacyArtifactError,
    PrivacyConfirmationError,
    PrivacyInputError,
    PrivacyMediaError,
    PrivacyPlanError,
)
from videoscope.privacy.executor import NativePrivacyExecutor, PrivacyNativeResult
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.metadata import MetadataPrivacyScanner, PrivateProbeSummary
from videoscope.privacy.models import (
    PrivacyEffectiveConfig,
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyReviewDecision,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyTechnicalReport,
    PrivacyVerificationReport,
    privacy_risk_sort_key,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import ShareAudienceProfile, get_share_audience_profile
from videoscope.privacy.scanners import (
    PrivacyScanContext,
    PrivacyScannerExecution,
    PrivacyScannerRunner,
    PrivacyScannerRunResult,
    PrivacyScannerStatus,
)
from videoscope.privacy.serialization import (
    read_privacy_plan_json,
    read_privacy_risk_map_json,
    write_privacy_plan_json,
    write_privacy_risk_map_json,
    write_privacy_technical_report_json,
)
from videoscope.privacy.text import SuspiciousTextScanner
from videoscope.privacy.verification import (
    PrivacyVerificationContext,
    PrivacyVerifier,
    ScannerVerificationIssue,
)
from videoscope.privacy.visual import AnonymousFaceScanner, QrBarcodeScanner
from videoscope.scenes import PySceneDetectAdapter, VideoScene
from videoscope.video import FrameSamplingResult, compute_file_sha256, sample_frames
from videoscope.video.errors import VideoProcessingError
from videoscope.video.probe import probe_video_with_private_summary

_STATE_NAME = "pipeline-state.json"
_RISK_MAP_NAME = "risk-map.json"
_REVIEW_NAME = "review.json"
_PLAN_NAME = "plan.json"
_CONFIRMATION_CLAIM_PREFIX = "confirmation-claim-"
_LIFECYCLE_GATE_NAME = ".privacy-lifecycle-transition"
_LIFECYCLE_DISCARDED_NAME = ".privacy-lifecycle-discarded"


class SafeSharingConfig(BaseModel):
    """Validated CPU-first settings for one Safe Sharing job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audience: str = Field(default="public", pattern=r"^[a-z][a-z0-9_]*$")
    sample_fps: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    thumbnail_max_size: int = Field(default=640, ge=32, le=4096)
    scanner_configurations: Mapping[str, Mapping[str, JsonValue]] = Field(
        default_factory=dict
    )
    enable_ocr: bool = False
    keep_workspace: bool = False
    effective_config: PrivacyEffectiveConfig = Field(
        default_factory=PrivacyEffectiveConfig
    )

    @model_validator(mode="after")
    def freeze_scanner_configurations(self) -> Self:
        """Detach nested scanner settings from caller-owned mutable dictionaries."""
        frozen = MappingProxyType(
            {
                scanner_id: MappingProxyType(dict(settings))
                for scanner_id, settings in self.scanner_configurations.items()
            }
        )
        object.__setattr__(self, "scanner_configurations", frozen)
        return self

    @field_serializer("scanner_configurations")
    def serialize_scanner_configurations(
        self,
        value: Mapping[str, Mapping[str, JsonValue]],
    ) -> dict[str, dict[str, JsonValue]]:
        return {
            scanner_id: dict(settings) for scanner_id, settings in sorted(value.items())
        }


class PrivacyScanResult(BaseModel):
    """Private review state produced without changing the source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    risk_map: PrivacyRiskMap
    scanner_executions: tuple[PrivacyScannerExecution, ...] = ()
    warnings: tuple[str, ...] = ()


class PrivacyReviewedResult(BaseModel):
    """Persisted human decisions for exactly one scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    scan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    reviews: tuple[PrivacyReviewDecision, ...]
    manual_risks: tuple[PrivacyRisk, ...] = ()


class PrivacyPreparation(BaseModel):
    """Immutable plan awaiting exact digest confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preparation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    review_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan: PrivacyPlan
    preview_relative_path: str | None = None


class PrivacyResult(BaseModel):
    """Terminal, public-safe result of one consumed confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PrivacyJobOutcome
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_count: int = Field(ge=1)
    video_relative_path: str = "share-package/share-safe.mp4"
    verification_relative_path: str = "share-package/verification.json"
    technical_report_relative_path: str = "share-package/technical-report.json"
    verification: PrivacyVerificationReport


class PrivacyExecutor(Protocol):
    def execute(
        self,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: Callable[[], bool],
    ) -> PrivacyNativeResult: ...

    def publish_pending(
        self,
        pending_root: Path,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: Callable[[], bool],
    ) -> Path: ...


class PrivacyPreviewExecutor(Protocol):
    def preview(
        self,
        plan: PrivacyPlan,
        source: Path,
        output: Path,
        cancellation: Callable[[], bool],
    ) -> Path: ...


class PrivacyVerificationService(Protocol):
    def verify(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        private_context: PrivacyVerificationContext,
    ) -> PrivacyVerificationReport: ...


@dataclass(slots=True)
class _LifecycleState:
    source: Path
    config: SafeSharingConfig
    scan: PrivacyScanResult
    sampling_work_identity: str | None
    reviewed: PrivacyReviewedResult | None = None
    preparation: PrivacyPreparation | None = None
    consumed: bool = False
    execution_count: int = 0


ProbeFunction = Callable[[Path], tuple[object, PrivateProbeSummary]]
SamplerFunction = Callable[..., FrameSamplingResult]
SceneDetectorFunction = Callable[[Path, float], Sequence[VideoScene]]
MetadataScannerFunction = Callable[
    [PrivateProbeSummary, str, ShareAudienceProfile], list[PrivacyRisk]
]


class SafeSharingPipeline:
    """Coordinate scan, review, plan, confirmation, execution, and verification."""

    def __init__(
        self,
        output_directory: Path,
        *,
        probe: ProbeFunction = probe_video_with_private_summary,
        sampler: SamplerFunction = sample_frames,
        scene_detector: SceneDetectorFunction | object | None = None,
        scanner_runner: PrivacyScannerRunner | object | None = None,
        metadata_scanner: MetadataScannerFunction | object | None = None,
        executor: PrivacyExecutor | None = None,
        preview_executor: PrivacyPreviewExecutor | None = None,
        verifier: PrivacyVerificationService | None = None,
        model_runtime: ModelRuntimeManager | None = None,
        hasher: Callable[[Path], str] = compute_file_sha256,
        cancellation: Callable[[], bool] | None = None,
    ) -> None:
        self._output = Path(output_directory)
        self._probe = probe
        self._sampler = sampler
        self._scene_detector = scene_detector or PySceneDetectAdapter()
        self._scanner_runner = scanner_runner
        self._metadata_scanner = metadata_scanner or MetadataPrivacyScanner()
        self._executor = executor or NativePrivacyExecutor()
        self._preview_executor = preview_executor or NativePrivacyExecutor()
        self._verifier = verifier or PrivacyVerifier()
        self._model_runtime = model_runtime
        self._hasher = hasher
        self._cancellation = cancellation or (lambda: False)
        self._states: dict[str, _LifecycleState] = {}
        self._review_index: dict[str, str] = {}
        self._preparation_index: dict[str, str] = {}
        self._confirmation_lock = Lock()

    def scan(self, *, source: Path, config: SafeSharingConfig) -> PrivacyScanResult:
        """Probe and sample once, then persist a private reviewable risk map."""
        source = Path(source)
        if not source.is_file():
            raise PrivacyInputError("Safe Sharing source is not a readable file")
        try:
            profile = get_share_audience_profile(config.audience)
        except KeyError as exc:
            raise PrivacyInputError("unknown Safe Sharing audience profile") from exc
        with self._lifecycle_transition(
            confirmation=False,
            create_job=True,
        ) as job_root:
            layout = PrivacyArtifactLayout.create(job_root)
            state_path = layout.private_root / _STATE_NAME
            if state_path.exists():
                raise PrivacyInputError(
                    "Safe Sharing output already contains an unfinished review"
                )
            sampling: FrameSamplingResult | None = None
            try:
                input_hash = self._hasher(source)
                metadata, private_probe = self._probe(source)
                duration = float(getattr(metadata, "duration_seconds"))
                sampling = self._sampler(
                    source,
                    sample_rate=config.sample_fps,
                    max_edge=config.thumbnail_max_size,
                    image_format="jpeg",
                    workspace_parent=layout.private_root,
                )
                scenes, scene_warnings = self._detect_scenes(source, duration)
                context = PrivacyScanContext(
                    input_path=source,
                    input_hash=input_hash,
                    duration_seconds=duration,
                    profile=profile,
                    workspace=sampling.work_directory,
                    frame_samples=sampling.samples,
                    scenes=scenes,
                    private_probe_summary=private_probe,
                    cancellation_callback=self._cancellation,
                )
                scanner_result = self._run_scanners(context, config)
                metadata_risks, metadata_execution = self._scan_metadata(
                    private_probe,
                    input_hash,
                    profile,
                )
                executions = (
                    *((metadata_execution,) if metadata_execution is not None else ()),
                    *scanner_result.executions,
                )
                risks = tuple(
                    sorted(
                        (*metadata_risks, *scanner_result.risks),
                        key=privacy_risk_sort_key,
                    )
                )
                risk_map = PrivacyRiskMap(
                    input_hash=input_hash,
                    profile=profile.id,
                    duration_seconds=duration,
                    risks=risks,
                )
                scan = PrivacyScanResult(
                    scan_id=uuid4().hex,
                    risk_map=risk_map,
                    scanner_executions=executions,
                    warnings=scene_warnings,
                )
                state = _LifecycleState(
                    source=source.resolve(),
                    config=config,
                    scan=scan,
                    sampling_work_identity=private_artifact_identity(
                        layout.private_root,
                        sampling.work_directory,
                    ),
                )
                self._states[scan.scan_id] = state
                write_privacy_risk_map_json(
                    risk_map,
                    layout.private_root / _RISK_MAP_NAME,
                )
                self._persist_state(state, layout.private_root)
                return scan
            except (KeyboardInterrupt, SystemExit):
                raise
            except PrivacyInputError:
                raise
            except VideoProcessingError as exc:
                self._cleanup_failed_scan(layout, sampling)
                raise PrivacyMediaError("local video preparation failed") from exc
            except (OSError, ValueError) as exc:
                self._cleanup_failed_scan(layout, sampling)
                raise PrivacyMediaError("local video preparation failed") from exc

    def resume(self, *, source: Path, config: SafeSharingConfig) -> PrivacyScanResult:
        """Restore one private lifecycle after validating source and configuration."""
        with self._lifecycle_transition(confirmation=False) as job_root:
            return self._resume_under_transition(
                job_root=job_root,
                source=source,
                config=config,
            )

    def _resume_under_transition(
        self,
        *,
        job_root: Path,
        source: Path,
        config: SafeSharingConfig,
    ) -> PrivacyScanResult:
        layout = self._existing_layout(job_root)
        state_path = layout.private_root / _STATE_NAME
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("privacy pipeline state must be an object")
            persisted_config = SafeSharingConfig.model_validate(payload["config"])
            if persisted_config != config:
                raise PrivacyInputError(
                    "Safe Sharing configuration changed after the private scan"
                )
            resolved_source = Path(source).resolve(strict=True)
            if self._hasher(resolved_source) != payload["source_hash"]:
                raise PrivacyConfirmationError(
                    "Safe Sharing source changed after the private scan"
                )
            risk_map = read_privacy_risk_map_json(layout.private_root / _RISK_MAP_NAME)
            executions = tuple(
                PrivacyScannerExecution.model_validate(item)
                for item in payload.get("scanner_executions", ())
            )
            scan = PrivacyScanResult(
                scan_id=payload["scan_id"],
                risk_map=risk_map,
                scanner_executions=executions,
                warnings=tuple(payload.get("warnings", ())),
            )
            sampling_path = payload.get("sampling_work_directory")
            sampling_identity = (
                sampling_path if isinstance(sampling_path, str) else None
            )
            if sampling_identity is not None:
                resolve_private_artifact(
                    layout.private_root,
                    sampling_identity,
                    require_exists=False,
                )
            state = _LifecycleState(
                source=resolved_source,
                config=persisted_config,
                scan=scan,
                sampling_work_identity=sampling_identity,
                consumed=bool(payload.get("consumed", False)),
                execution_count=int(payload.get("execution_count", 0)),
            )
            review_path = layout.private_root / _REVIEW_NAME
            if review_path.is_file():
                state.reviewed = PrivacyReviewedResult.model_validate_json(
                    review_path.read_bytes()
                )
                self._review_index[state.reviewed.review_id] = scan.scan_id
            plan_path = layout.private_root / _PLAN_NAME
            preparation_id = payload.get("preparation_id")
            if isinstance(preparation_id, str) and plan_path.is_file():
                if state.reviewed is None:
                    raise ValueError("persisted preparation has no review")
                state.preparation = PrivacyPreparation(
                    preparation_id=preparation_id,
                    review_id=state.reviewed.review_id,
                    plan=read_privacy_plan_json(plan_path),
                    preview_relative_path=payload.get("preview_relative_path"),
                )
                self._preparation_index[preparation_id] = scan.scan_id
            self._states[scan.scan_id] = state
            return scan
        except (PrivacyInputError, PrivacyConfirmationError):
            raise
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise PrivacyArtifactError("private Safe Sharing state is invalid") from exc

    def review(
        self,
        scan_id: str,
        reviews: Sequence[PrivacyReviewDecision],
        *,
        manual_visual_regions: Sequence[ManualVisualRegionInput] = (),
        manual_audio_intervals: Sequence[ManualAudioIntervalInput] = (),
    ) -> PrivacyReviewedResult:
        """Validate and persist decisions without changing video content."""
        with self._lifecycle_transition(confirmation=False) as job_root:
            private_root = self._private_root(job_root)
            self._require_unclaimed_lifecycle(private_root)
            state = self._require_scan(scan_id)
            risk_map = state.scan.risk_map
            try:
                manual_risks = tuple(
                    build_manual_visual_risk(
                        risk_map.input_hash,
                        value.model_copy(
                            update={
                                "source_duration_seconds": risk_map.duration_seconds
                            }
                        ),
                    )
                    for value in manual_visual_regions
                ) + tuple(
                    build_manual_audio_risk(
                        risk_map.input_hash,
                        value.model_copy(
                            update={
                                "source_duration_seconds": risk_map.duration_seconds
                            }
                        ),
                    )
                    for value in manual_audio_intervals
                )
            except ValueError as exc:
                raise PrivacyInputError("manual privacy selection is invalid") from exc
            manual_ids = [risk.id for risk in manual_risks]
            existing_ids = {risk.id for risk in risk_map.risks}
            if len(manual_ids) != len(set(manual_ids)) or any(
                risk_id in existing_ids for risk_id in manual_ids
            ):
                raise PrivacyInputError(
                    "manual privacy selection contains duplicate risks"
                )
            known = existing_ids | set(manual_ids)
            ids = [review.risk_id for review in reviews]
            if len(ids) != len(set(ids)) or any(
                risk_id not in known for risk_id in ids
            ):
                raise PrivacyInputError("review references unknown or duplicate risks")
            reviewed = PrivacyReviewedResult(
                review_id=uuid4().hex,
                scan_id=scan_id,
                reviews=tuple(reviews),
                manual_risks=manual_risks,
            )
            state.reviewed = reviewed
            state.preparation = None
            state.consumed = False
            self._review_index[reviewed.review_id] = scan_id
            self._write_private_json(
                private_root / _REVIEW_NAME,
                reviewed.model_dump(mode="json"),
            )
            self._persist_state(state, private_root)
            return reviewed

    def prepare(self, review_id: str) -> PrivacyPreparation:
        """Create and persist one immutable confirmation-bound plan."""
        with self._lifecycle_transition(confirmation=False) as job_root:
            private_root = self._private_root(job_root)
            self._require_unclaimed_lifecycle(private_root)
            state = self._require_review(review_id)
            assert state.reviewed is not None
            try:
                profile = get_share_audience_profile(state.config.audience)
                risk_map = state.scan.risk_map
                if state.reviewed.manual_risks:
                    risk_map = risk_map.model_copy(
                        update={"risks": risk_map.risks + state.reviewed.manual_risks}
                    )
                plan = build_privacy_plan(
                    risk_map,
                    state.reviewed.reviews,
                    profile,
                    state.config.effective_config,
                )
            except (KeyError, PrivacyPlanError) as exc:
                raise PrivacyInputError(
                    "review cannot form a Safe Sharing plan"
                ) from exc
            preparation = PrivacyPreparation(
                preparation_id=uuid4().hex,
                review_id=review_id,
                plan=plan,
            )
            state.preparation = preparation
            state.consumed = False
            self._preparation_index[preparation.preparation_id] = state.scan.scan_id
            write_privacy_plan_json(plan, private_root / _PLAN_NAME)
            self._persist_state(state, private_root)
            return preparation

    def load_preparation(self, preparation_id: str) -> PrivacyPreparation:
        """Load a persisted preparation while preserving the exact digest."""
        state = self._require_preparation(preparation_id)
        assert state.preparation is not None
        persisted = read_privacy_plan_json(self._private_root() / _PLAN_NAME)
        if persisted != state.preparation.plan:
            raise PrivacyArtifactError("persisted Safe Sharing plan changed")
        return state.preparation

    def current_review(self, scan_id: str) -> PrivacyReviewedResult | None:
        """Return the current persisted review for one restored scan."""
        return self._require_scan(scan_id).reviewed

    def current_preparation(self, scan_id: str) -> PrivacyPreparation | None:
        """Return the current persisted preparation for one restored scan."""
        return self._require_scan(scan_id).preparation

    def preview(self, preparation_id: str) -> Path:
        """Create an exact private preview copy without consuming confirmation."""
        with self._lifecycle_transition(confirmation=False) as job_root:
            private_root = self._private_root(job_root)
            self._require_unclaimed_lifecycle(private_root)
            state = self._require_preparation(preparation_id)
            if state.consumed:
                raise PrivacyConfirmationError("confirmation was already consumed")
            assert state.preparation is not None
            preview_path = private_root / "preview" / "privacy-preview.mp4"
            if preview_path.is_file():
                return preview_path
            stage_root = private_root / "preview-stage"
            shutil.rmtree(stage_root, ignore_errors=True)
            try:
                staged_preview = stage_root / "privacy-preview.mp4"
                rendered_preview = self._preview_executor.preview(
                    state.preparation.plan,
                    state.source,
                    staged_preview,
                    self._cancellation,
                )
                if (
                    rendered_preview.resolve(strict=True)
                    != staged_preview.resolve(strict=True)
                    or not rendered_preview.is_file()
                    or rendered_preview.stat().st_size <= 0
                ):
                    raise PrivacyArtifactError(
                        "Safe Sharing preview executor returned an invalid artifact"
                    )
                preview_path.parent.mkdir(parents=True, exist_ok=True)
                if preview_path.exists():
                    raise PrivacyArtifactError(
                        "Safe Sharing refuses to overwrite preview"
                    )
                rendered_preview.replace(preview_path)
            finally:
                shutil.rmtree(stage_root, ignore_errors=True)
            state.preparation = state.preparation.model_copy(
                update={"preview_relative_path": "preview/privacy-preview.mp4"}
            )
            self._persist_state(state, private_root)
            return preview_path

    def confirm(self, preparation_id: str, plan_digest: str) -> PrivacyResult:
        """Consume one exact digest and execute it at most once."""
        with self._confirmation_lock:
            with self._lifecycle_transition(confirmation=True) as job_root:
                return self._confirm_under_transition(
                    job_root=job_root,
                    preparation_id=preparation_id,
                    plan_digest=plan_digest,
                )

    def _confirm_under_transition(
        self,
        *,
        job_root: Path,
        preparation_id: str,
        plan_digest: str,
    ) -> PrivacyResult:
        state = self._require_preparation(preparation_id)
        preparation = state.preparation
        assert preparation is not None
        if state.consumed:
            raise PrivacyConfirmationError("confirmation was already consumed")
        if not hmac.compare_digest(preparation.plan.digest, plan_digest):
            raise PrivacyConfirmationError("confirmation digest mismatch")
        layout = self._existing_layout(job_root)
        private_root = layout.private_root
        self._validate_persisted_confirmation(
            state,
            preparation,
            private_root,
        )
        self._claim_confirmation(preparation, private_root)
        state.consumed = True
        state.execution_count += 1
        self._persist_state(state, private_root)
        pending_root: Path | None = None
        try:
            native = self._executor.execute(
                preparation.plan,
                state.source,
                self._output,
                self._cancellation,
            )
            pending_root = native.pending_root
            if pending_root is None:
                raise PrivacyArtifactError(
                    "Safe Sharing executor did not retain a pending package"
                )
            pending_layout = PrivacyArtifactLayout(
                job_root=layout.job_root,
                private_root=layout.private_root,
                public_root=pending_root.resolve(strict=True),
            )
            verification = self._verifier.verify(
                state.source,
                native.staged_video,
                preparation.plan,
                PrivacyVerificationContext(
                    public_root=pending_layout.public_root,
                    expected_candidate_sha256=native.change_log.artifacts[0].sha256,
                    expected_artifacts=("share-safe.mp4", "changes.json"),
                    scanner_issues=self._scanner_issues(state.scan.scanner_executions),
                    sample_fps=state.config.sample_fps,
                ),
            )
            if verification.status is PrivacyJobOutcome.COMPLETED:
                self._publish_completed_package(
                    pending_layout=pending_layout,
                    preparation=preparation,
                    native=native,
                    state=state,
                    verification=verification,
                )
                pending_root = None
        finally:
            if pending_root is not None:
                shutil.rmtree(pending_root, ignore_errors=True)
        if not state.config.keep_workspace and state.sampling_work_identity is not None:
            sampling_work_directory = resolve_private_artifact(
                layout.private_root,
                state.sampling_work_identity,
                require_exists=False,
            )
            shutil.rmtree(sampling_work_directory, ignore_errors=True)
            state.sampling_work_identity = None
            self._persist_state(state, private_root)
        return PrivacyResult(
            status=verification.status,
            plan_digest=preparation.plan.digest,
            execution_count=state.execution_count,
            verification=verification,
        )

    def _publish_completed_package(
        self,
        *,
        pending_layout: PrivacyArtifactLayout,
        preparation: PrivacyPreparation,
        native: PrivacyNativeResult,
        state: _LifecycleState,
        verification: PrivacyVerificationReport,
    ) -> None:
        """Publish only a candidate whose required verification fully passed."""
        if verification.status is not PrivacyJobOutcome.COMPLETED:
            raise PrivacyArtifactError(
                "Safe Sharing refuses to publish an unverified candidate"
            )
        self._write_private_json(
            pending_layout.public_root / "verification.json",
            verification.model_dump(mode="json"),
            public=True,
        )
        technical = PrivacyTechnicalReport(
            plan_digest=preparation.plan.digest,
            verification=verification,
            artifacts=native.change_log.artifacts,
        )
        write_privacy_technical_report_json(
            technical,
            pending_layout.public_root / "technical-report.json",
        )
        summary: dict[str, JsonValue] = {
            "schema_version": "0.1",
            "plan_digest": preparation.plan.digest,
            "status": verification.status.value,
            "risk_count": len(preparation.plan.risks),
            "action_count": len(preparation.plan.actions),
            "human_review_required": False,
        }
        self._write_private_json(
            pending_layout.public_root / "privacy-summary.json",
            summary,
            public=True,
        )
        report_artifacts: list[JsonValue] = [
            {
                "relative_path": cast(JsonValue, path.name),
                "sha256": cast(JsonValue, compute_file_sha256(path)),
            }
            for path in sorted(pending_layout.public_root.iterdir())
            if path.is_file()
        ]
        manifest: dict[str, JsonValue] = {
            "schema_version": "0.1",
            "plan_digest": preparation.plan.digest,
            "artifacts": report_artifacts,
        }
        self._write_private_json(
            pending_layout.public_root / "manifest.json",
            manifest,
            public=True,
        )
        expected_package = {
            "changes.json",
            "manifest.json",
            "privacy-summary.json",
            "share-safe.mp4",
            "technical-report.json",
            "verification.json",
        }
        if set(pending_layout.validate_public_tree()) != expected_package:
            raise PrivacyArtifactError("pending Safe Sharing package is incomplete")
        self._executor.publish_pending(
            pending_layout.public_root,
            preparation.plan,
            state.source,
            self._output,
            self._cancellation,
        )

    def discard(self, lifecycle_id: str) -> None:
        """Delete unclaimed private state without touching source/public output."""
        with self._lifecycle_transition(
            confirmation=False,
            allow_discarded=True,
        ) as job_root:
            state = self._state_for_id(lifecycle_id)
            private_root = job_root / PRIVATE_ROOT_NAME
            if (
                state is not None
                and state.consumed
                or self._private_lifecycle_is_claimed(private_root)
            ):
                raise PrivacyInputError(
                    "claimed Safe Sharing state cannot be discarded"
                )
            self._mark_lifecycle_discarded(job_root)
            try:
                shutil.rmtree(private_root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise PrivacyArtifactError(
                    "private Safe Sharing state could not be discarded"
                ) from exc
            public_root = job_root / PUBLIC_ROOT_NAME
            try:
                public_root.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # A non-empty public directory is never recursively discarded.
                pass
            if state is not None:
                self._states.pop(state.scan.scan_id, None)
                if state.reviewed is not None:
                    self._review_index.pop(state.reviewed.review_id, None)
                if state.preparation is not None:
                    self._preparation_index.pop(
                        state.preparation.preparation_id,
                        None,
                    )

    def _detect_scenes(
        self,
        source: Path,
        duration_seconds: float,
    ) -> tuple[tuple[VideoScene, ...], tuple[str, ...]]:
        detector = self._scene_detector
        if callable(detector):
            result = detector(source, duration_seconds)
        else:
            result = cast(Any, detector).detect(
                source,
                duration_seconds=duration_seconds,
            )
        if hasattr(result, "scenes"):
            return tuple(result.scenes), tuple(getattr(result, "warnings", ()))
        return tuple(cast(Sequence[VideoScene], result)), ()

    def _run_scanners(
        self,
        context: PrivacyScanContext,
        config: SafeSharingConfig,
    ) -> PrivacyScannerRunResult:
        runner = self._scanner_runner
        if runner is None:
            scanners: list[object] = [AnonymousFaceScanner(), QrBarcodeScanner()]
            if config.enable_ocr:
                scanners.append(SuspiciousTextScanner(self._model_runtime))
            runner = PrivacyScannerRunner(cast(Sequence[Any], scanners))
        result = cast(
            PrivacyScannerRunResult,
            cast(Any, runner).run(context, config.scanner_configurations),
        )
        requires_text_review = (
            "text" in context.profile.required_manual_review_categories
        )
        has_text_execution = any(
            execution.scanner_id == "suspicious_text" for execution in result.executions
        )
        if not config.enable_ocr and requires_text_review and not has_text_execution:
            return PrivacyScannerRunResult(
                executions=(
                    *result.executions,
                    PrivacyScannerExecution(
                        scanner_id="suspicious_text",
                        status=PrivacyScannerStatus.SKIPPED,
                        elapsed_seconds=0.0,
                        risks_count=0,
                        fallback="manual_visual_region",
                    ),
                ),
                risks=result.risks,
            )
        return result

    def _scan_metadata(
        self,
        summary: PrivateProbeSummary,
        input_hash: str,
        profile: ShareAudienceProfile,
    ) -> tuple[tuple[PrivacyRisk, ...], PrivacyScannerExecution | None]:
        started = perf_counter()
        try:
            scanner = self._metadata_scanner
            if callable(scanner):
                risks = scanner(summary, input_hash, profile)
            else:
                risks = cast(Any, scanner).scan(summary, input_hash, profile)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            return (), PrivacyScannerExecution(
                scanner_id="metadata_privacy",
                status=PrivacyScannerStatus.SCANNER_ERROR,
                elapsed_seconds=max(0.0, perf_counter() - started),
                risks_count=0,
                error_type=type(exc).__name__,
                error_message=(
                    f"{type(exc).__name__} while running privacy scanner; "
                    "details redacted"
                ),
            )
        return tuple(risks), PrivacyScannerExecution(
            scanner_id="metadata_privacy",
            status=PrivacyScannerStatus.OK,
            elapsed_seconds=max(0.0, perf_counter() - started),
            risks_count=len(risks),
        )

    def _scanner_issues(
        self,
        executions: Sequence[PrivacyScannerExecution],
    ) -> tuple[ScannerVerificationIssue, ...]:
        categories = {
            "metadata_privacy": "metadata",
            "anonymous_face": "visual",
            "qr_barcode": "qr_barcode",
            "suspicious_text": "text",
        }
        return tuple(
            ScannerVerificationIssue(
                scanner_id=execution.scanner_id,
                category=cast(Any, categories[execution.scanner_id]),
            )
            for execution in executions
            if execution.status is not PrivacyScannerStatus.OK
            and execution.scanner_id in categories
        )

    def _require_scan(self, scan_id: str) -> _LifecycleState:
        try:
            return self._states[scan_id]
        except KeyError as exc:
            raise PrivacyInputError("unknown or expired Safe Sharing scan") from exc

    def _require_review(self, review_id: str) -> _LifecycleState:
        try:
            state = self._states[self._review_index[review_id]]
        except KeyError as exc:
            raise PrivacyInputError("unknown or expired Safe Sharing review") from exc
        if state.reviewed is None or state.reviewed.review_id != review_id:
            raise PrivacyInputError("Safe Sharing review state is inconsistent")
        return state

    def _require_preparation(self, preparation_id: str) -> _LifecycleState:
        try:
            state = self._states[self._preparation_index[preparation_id]]
        except KeyError as exc:
            raise PrivacyInputError(
                "unknown or expired Safe Sharing preparation"
            ) from exc
        if (
            state.preparation is None
            or state.preparation.preparation_id != preparation_id
        ):
            raise PrivacyInputError("Safe Sharing preparation state is inconsistent")
        return state

    def _state_for_id(self, lifecycle_id: str) -> _LifecycleState | None:
        if lifecycle_id in self._states:
            return self._states[lifecycle_id]
        scan_id = self._review_index.get(lifecycle_id) or self._preparation_index.get(
            lifecycle_id
        )
        return self._states.get(scan_id) if scan_id is not None else None

    def _private_root(self, job_root: Path | None = None) -> Path:
        root = (
            resolve_existing_job_root(self._output)
            if job_root is None
            else Path(job_root)
        )
        private_root = root / PRIVATE_ROOT_NAME
        try:
            resolve_private_artifact(
                private_root,
                ".lifecycle-root-check",
                require_exists=False,
            )
            resolved = private_root.resolve(strict=True)
        except (OSError, PrivacyArtifactError) as exc:
            raise PrivacyArtifactError(
                "private Safe Sharing lifecycle is unavailable"
            ) from exc
        if not resolved.is_dir():
            raise PrivacyArtifactError("private Safe Sharing lifecycle is unavailable")
        return resolved

    @staticmethod
    def _existing_layout(job_root: Path) -> PrivacyArtifactLayout:
        return PrivacyArtifactLayout.create(job_root)

    @contextmanager
    def _lifecycle_transition(
        self,
        *,
        confirmation: bool,
        create_job: bool = False,
        allow_discarded: bool = False,
    ) -> Iterator[Path]:
        """Hold one atomic cross-process gate for every lifecycle mutation."""
        if create_job:
            try:
                self._output.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PrivacyArtifactError(
                    "Safe Sharing job root could not be created"
                ) from exc
        try:
            job_root = resolve_existing_job_root(self._output)
        except PrivacyArtifactError as exc:
            if confirmation:
                raise PrivacyConfirmationError(
                    "Safe Sharing lifecycle is no longer available"
                ) from exc
            raise
        gate = job_root / _LIFECYCLE_GATE_NAME
        try:
            gate.mkdir()
        except FileExistsError as exc:
            if confirmation:
                raise PrivacyConfirmationError(
                    "Safe Sharing lifecycle transition is already in progress"
                ) from exc
            raise PrivacyInputError(
                "Safe Sharing lifecycle transition is already in progress"
            ) from exc
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing lifecycle transition could not be claimed"
            ) from exc
        try:
            if not allow_discarded and self._lifecycle_is_discarded(job_root):
                if confirmation:
                    raise PrivacyConfirmationError(
                        "Safe Sharing lifecycle is no longer available"
                    )
                raise PrivacyInputError(
                    "Safe Sharing lifecycle was permanently discarded"
                )
            yield job_root
        finally:
            try:
                gate.rmdir()
            except OSError:
                pass

    @staticmethod
    def _lifecycle_is_discarded(job_root: Path) -> bool:
        marker = Path(job_root) / _LIFECYCLE_DISCARDED_NAME
        try:
            os.lstat(marker)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing lifecycle terminal state could not be inspected"
            ) from exc
        return True

    @staticmethod
    def _mark_lifecycle_discarded(job_root: Path) -> None:
        marker = Path(job_root) / _LIFECYCLE_DISCARDED_NAME
        try:
            descriptor = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing lifecycle could not be discarded"
            ) from exc
        try:
            with os.fdopen(
                descriptor,
                mode="w",
                encoding="utf-8",
                newline="\n",
            ) as stream:
                stream.write("discarded\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing lifecycle discard could not be persisted"
            ) from exc

    @staticmethod
    def _validate_persisted_confirmation(
        state: _LifecycleState,
        preparation: PrivacyPreparation,
        private_root: Path,
    ) -> None:
        """Reject stale in-memory confirmation state after acquiring the gate."""
        try:
            payload = json.loads(
                (private_root / _STATE_NAME).read_text(encoding="utf-8")
            )
            persisted_plan = read_privacy_plan_json(private_root / _PLAN_NAME)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PrivacyConfirmationError(
                "persisted Safe Sharing preparation is no longer claimable"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("scan_id") != state.scan.scan_id
            or payload.get("preparation_id") != preparation.preparation_id
            or bool(payload.get("consumed", False))
            or persisted_plan != preparation.plan
        ):
            raise PrivacyConfirmationError(
                "persisted Safe Sharing preparation is no longer claimable"
            )

    @staticmethod
    def _private_lifecycle_is_claimed(private_root: Path) -> bool:
        """Fail closed when disk state shows a claim, pending work, or consumption."""
        if not private_root.is_dir():
            return False
        try:
            if any(private_root.glob(f"{_CONFIRMATION_CLAIM_PREFIX}*.lock")):
                return True
            if any(private_root.glob("pending-package-*")):
                return True
            state_path = private_root / _STATE_NAME
            if not state_path.is_file():
                return False
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PrivacyArtifactError(
                "private Safe Sharing lifecycle could not be inspected"
            ) from exc
        if not isinstance(payload, dict):
            raise PrivacyArtifactError("private Safe Sharing lifecycle is invalid")
        return bool(payload.get("consumed", False))

    def _require_unclaimed_lifecycle(self, private_root: Path) -> None:
        if self._private_lifecycle_is_claimed(private_root):
            raise PrivacyInputError("claimed Safe Sharing state cannot be changed")

    def _claim_confirmation(
        self,
        preparation: PrivacyPreparation,
        private_root: Path,
    ) -> None:
        """Atomically claim one preparation across processes and pipeline instances."""
        identity = f"{_CONFIRMATION_CLAIM_PREFIX}{preparation.preparation_id}.lock"
        claim_path = resolve_private_artifact(
            private_root,
            identity,
            require_exists=False,
        )
        try:
            descriptor = os.open(
                claim_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise PrivacyConfirmationError(
                "confirmation was already consumed or is in progress"
            ) from exc
        except OSError as exc:
            raise PrivacyArtifactError(
                "private Safe Sharing confirmation could not be claimed"
            ) from exc
        try:
            with os.fdopen(
                descriptor, mode="w", encoding="utf-8", newline="\n"
            ) as stream:
                stream.write(preparation.plan.digest + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise PrivacyArtifactError(
                "private Safe Sharing confirmation claim could not be persisted"
            ) from exc

    def _persist_state(self, state: _LifecycleState, private_root: Path) -> None:
        payload = {
            "schema_version": "0.1",
            "scan_id": state.scan.scan_id,
            "review_id": state.reviewed.review_id if state.reviewed else None,
            "preparation_id": (
                state.preparation.preparation_id if state.preparation else None
            ),
            "plan_digest": state.preparation.plan.digest if state.preparation else None,
            "preview_relative_path": (
                state.preparation.preview_relative_path
                if state.preparation is not None
                else None
            ),
            "consumed": state.consumed,
            "execution_count": state.execution_count,
            "source_path": str(state.source),
            "source_hash": state.scan.risk_map.input_hash,
            "config": state.config.model_dump(mode="json"),
            "scanner_executions": [
                execution.model_dump(mode="json")
                for execution in state.scan.scanner_executions
            ],
            "warnings": list(state.scan.warnings),
            "sampling_work_directory": (
                state.sampling_work_identity
                if state.sampling_work_identity is not None
                else None
            ),
        }
        self._write_private_json(Path(private_root) / _STATE_NAME, payload)

    @staticmethod
    def _write_private_json(
        path: Path,
        payload: Mapping[str, Any],
        *,
        public: bool = False,
    ) -> None:
        if public:
            validate_public_manifest(cast(Mapping[str, JsonValue], payload))
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content + "\n")
                stream.flush()
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _cleanup_failed_scan(
        layout: PrivacyArtifactLayout,
        sampling: FrameSamplingResult | None,
    ) -> None:
        if sampling is not None:
            try:
                identity = private_artifact_identity(
                    layout.private_root,
                    sampling.work_directory,
                )
                safe_sampling = resolve_private_artifact(
                    layout.private_root,
                    identity,
                    require_exists=False,
                )
                shutil.rmtree(safe_sampling, ignore_errors=True)
            except PrivacyArtifactError:
                pass
        shutil.rmtree(layout.private_root, ignore_errors=True)


__all__ = [
    "PrivacyPreparation",
    "PrivacyResult",
    "PrivacyReviewedResult",
    "PrivacyScanResult",
    "SafeSharingConfig",
    "SafeSharingPipeline",
]
