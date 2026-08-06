"""One core lifecycle for Long Video to Useful Content."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from videoscope.content.artifacts import (
    ContentArtifactLayout,
    publish_verified_content,
)
from videoscope.content.errors import (
    ContentArtifactError,
    ContentCancelledError,
    ContentConfirmationError,
    ContentError,
    ContentInputError,
    ContentPreviewError,
)
from videoscope.content.executor import NativeContentExecutor, NativeContentResult
from videoscope.content.features import (
    ContentFeatureBundle,
    StructuralFeatureConfig,
    collect_content_features,
)
from videoscope.content.mapping import build_content_map
from videoscope.content.models import (
    ContentAction,
    ContentArtifact,
    ContentArtifactRole,
    ContentChangeLog,
    ContentConfig,
    ContentConfirmation,
    ContentMap,
    ContentOutcome,
    ContentPlan,
    ContentSourceMapping,
    ContentTechnicalReport,
    ContentUserRange,
    ContentVerificationReport,
    Storyboard,
)
from videoscope.content.planner import (
    build_content_actions,
    build_content_plan,
    build_storyboard,
    revise_storyboard,
)
from videoscope.content.preview import (
    ContentJoinPreview,
    ContentPreviewBuilder,
    RetainedContentSource,
    assess_previews,
)
from videoscope.content.report import (
    build_content_change_log,
    build_content_technical_report,
    render_content_report,
)
from videoscope.content.serialization import (
    content_change_log_to_json,
    content_technical_report_to_json,
    write_content_map_json,
    write_content_plan_json,
    write_storyboard_json,
)
from videoscope.content.transcript import (
    NormalizedTranscript,
    load_timed_transcript,
)
from videoscope.content.verification import (
    ContentVerificationEvidence,
    verify_content_result,
)
from videoscope.detectors.image_features import (
    average_hash,
    compute_luma_metrics,
    hash_distance,
    load_luma_image,
    mean_absolute_difference,
)
from videoscope.domain import VideoMetadata
from videoscope.processes import pinned_subprocess_options
from videoscope.video.probe import probe_video
from videoscope.video.sampling import sample_frames


class ContentStatus(StrEnum):
    CREATED = "created"
    PROBING = "probing"
    MAPPING = "mapping"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    PREVIEWING = "previewing"
    READY_TO_CONFIRM = "ready_to_confirm"
    RENDERING = "rendering"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContentPipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    output_directory: Path = Path("videoscope-content-output")
    content: ContentConfig = Field(default_factory=ContentConfig)
    features: StructuralFeatureConfig = Field(default_factory=StructuralFeatureConfig)
    transcript_path: Path | None = None
    user_ranges: tuple[ContentUserRange, ...] = ()
    keep_workspace: bool = False


@dataclass(frozen=True, slots=True)
class ContentPreparation:
    content_map: ContentMap
    storyboard: Storyboard
    actions: tuple[ContentAction, ...]
    metadata: VideoMetadata
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentReview:
    preparation: ContentPreparation
    previews: tuple[ContentJoinPreview, ...]
    plan: ContentPlan


@dataclass(frozen=True, slots=True)
class ContentResult:
    status: ContentStatus
    public_root: Path | None
    technical_report: ContentTechnicalReport
    verification: ContentVerificationReport
    artifacts: tuple[ContentArtifact, ...]


class ContentEvidenceInspector(Protocol):
    def inspect(
        self,
        *,
        plan: ContentPlan,
        native: NativeContentResult,
        source_metadata: VideoMetadata,
        transcript: NormalizedTranscript | None,
        private_root: Path,
        feature_config: StructuralFeatureConfig,
    ) -> ContentVerificationEvidence: ...


class ContentPreviewService(Protocol):
    def build(
        self,
        *,
        source: RetainedContentSource,
        transcript_hash: str | None,
        actions: Sequence[ContentAction],
        duration_seconds: float,
        private_review_root: Path,
        maximum_preview_seconds: float,
        has_audio: bool,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[ContentJoinPreview, ...]: ...


class ContentExecutorService(Protocol):
    def execute(
        self,
        *,
        plan: ContentPlan,
        confirmation: ContentConfirmation,
        source: RetainedContentSource,
        transcript_hash: str | None,
        work_root: Path,
        has_audio: bool,
    ) -> NativeContentResult: ...


@dataclass(slots=True)
class ContentPipelineDependencies:
    feature_collector: Callable[..., ContentFeatureBundle] = collect_content_features
    preview_builder: ContentPreviewService = field(
        default_factory=ContentPreviewBuilder
    )
    executor: ContentExecutorService = field(default_factory=NativeContentExecutor)
    evidence_inspector: ContentEvidenceInspector = field(
        default_factory=lambda: LocalContentEvidenceInspector()
    )
    publisher: Callable[..., tuple[ContentArtifact, ...]] = publish_verified_content
    report_renderer: Callable[[ContentPlan, ContentTechnicalReport], str] = (
        render_content_report
    )


@dataclass(slots=True)
class _IssuedContent:
    preparation: ContentPreparation
    source: RetainedContentSource
    layout: ContentArtifactLayout
    transcript: NormalizedTranscript | None
    review: ContentReview | None = None
    confirmation: ContentConfirmation | None = None
    active: bool = False


class LongVideoContentPipeline:
    """Prepare, review, confirm, render, verify, and publish one local job."""

    def __init__(
        self,
        config: ContentPipelineConfig,
        *,
        dependencies: ContentPipelineDependencies | None = None,
        progress: Callable[[ContentStatus], None] | None = None,
    ) -> None:
        self.config = config
        self._progress = progress
        self._issued: dict[int, _IssuedContent] = {}
        self._cancelled = False
        self._dependencies = dependencies or ContentPipelineDependencies(
            executor=NativeContentExecutor(is_cancelled=self._is_cancelled)
        )

    def prepare(self, input_path: Path) -> ContentPreparation:
        self._emit(ContentStatus.CREATED)
        path = Path(input_path)
        if not path.is_file():
            raise ContentInputError("source video does not exist")
        layout = ContentArtifactLayout.create(self.config.output_directory)
        source: RetainedContentSource | None = None
        try:
            source_hash = _hash_path(path)
            source = RetainedContentSource(path, expected_hash=source_hash)
            self._check_cancelled()
            self._emit(ContentStatus.PROBING)
            bundle = self._dependencies.feature_collector(
                source.input_path,
                input_hash=source_hash,
                workspace=layout.private_root / "evidence",
                config=self.config.features,
                cancellation_callback=self._is_cancelled,
            )
            transcript = self._load_transcript(bundle.metadata.duration_seconds)
            self._emit(ContentStatus.MAPPING)
            self._check_cancelled()
            content_map = build_content_map(
                bundle,
                input_hash=source_hash,
                effective_config=self.config.content,
                transcript=transcript,
                user_ranges=self.config.user_ranges,
            )
            self._emit(ContentStatus.PLANNING)
            self._check_cancelled()
            storyboard = build_storyboard(content_map)
            actions = build_content_actions(content_map, storyboard)
            write_content_map_json(
                content_map,
                layout.private_root / "content-map.json",
            )
            write_storyboard_json(
                storyboard,
                layout.private_root / "storyboard.json",
            )
            preparation = ContentPreparation(
                content_map=content_map,
                storyboard=storyboard,
                actions=actions,
                metadata=bundle.metadata,
                warnings=bundle.warnings,
            )
            self._issued[id(preparation)] = _IssuedContent(
                preparation=preparation,
                source=source,
                layout=layout,
                transcript=transcript,
            )
            source = None
            self._emit(ContentStatus.AWAITING_REVIEW)
            self._check_cancelled()
            return preparation
        except BaseException:
            if source is not None:
                source.close()
            if not self.config.keep_workspace:
                layout.cleanup_private()
            raise

    def preview(self, preparation: ContentPreparation) -> ContentReview:
        issued = self._require_preparation(preparation)
        self._check_cancelled()
        issued.active = True
        try:
            self._emit(ContentStatus.PREVIEWING)
            self._check_cancelled()
            previews = self._dependencies.preview_builder.build(
                source=issued.source,
                transcript_hash=preparation.content_map.transcript_hash,
                actions=preparation.actions,
                duration_seconds=preparation.content_map.duration_seconds,
                private_review_root=issued.layout.private_root,
                maximum_preview_seconds=self.config.content.maximum_preview_seconds,
                has_audio=preparation.metadata.has_audio,
                cancelled=self._is_cancelled,
            )
            assessment = assess_previews(preparation.actions, previews)
            if assessment.blocked_action_ids:
                raise ContentPreviewError(
                    "one or more required previews are unavailable"
                )
            plan = build_content_plan(
                preparation.content_map,
                preparation.storyboard,
                preview_identities=assessment.identities,
            )
            write_content_plan_json(plan, issued.layout.private_root / "plan.json")
            review = ContentReview(preparation, previews, plan)
            issued.review = review
            self._emit(ContentStatus.READY_TO_CONFIRM)
            self._check_cancelled()
            return review
        finally:
            issued.active = False
            if self._cancelled:
                self._release(preparation)

    def revise(
        self,
        preparation: ContentPreparation,
        *,
        selected_range_order: tuple[str, ...] = (),
        reorder_acknowledged: bool = False,
        chapter_titles: dict[str, str] | None = None,
    ) -> ContentPreparation:
        """Replace a draft with one deterministic revision before previewing."""
        issued = self._require_preparation(preparation)
        self._check_cancelled()
        if issued.review is not None or issued.confirmation is not None:
            raise ContentConfirmationError(
                "a previewed or confirmed preparation cannot be revised"
            )
        storyboard = revise_storyboard(
            preparation.content_map,
            selected_range_order=selected_range_order,
            reorder_acknowledged=reorder_acknowledged,
            chapter_titles=chapter_titles,
        )
        revised = ContentPreparation(
            content_map=preparation.content_map,
            storyboard=storyboard,
            actions=build_content_actions(preparation.content_map, storyboard),
            metadata=preparation.metadata,
            warnings=preparation.warnings,
        )
        write_storyboard_json(
            storyboard,
            issued.layout.private_root / "storyboard.json",
        )
        self._issued.pop(id(preparation))
        issued.preparation = revised
        self._issued[id(revised)] = issued
        return revised

    def confirm(
        self,
        review: ContentReview,
        *,
        accepted_action_ids: tuple[str, ...],
    ) -> ContentConfirmation:
        issued = self._require_review(review)
        required = tuple(
            action.id
            for action in review.plan.actions
            if action.changes_content and action.requires_confirmation
        )
        if accepted_action_ids != required:
            raise ContentConfirmationError(
                "confirmation must accept the exact action set"
            )
        confirmation = ContentConfirmation(
            input_hash=review.plan.input_hash,
            transcript_hash=review.plan.transcript_hash,
            plan_digest=review.plan.plan_digest,
            storyboard_digest=review.plan.storyboard.storyboard_digest,
            accepted_action_ids=accepted_action_ids,
            preview_identities=review.plan.preview_identities,
            locked_range_ids=tuple(item.id for item in review.plan.locked_ranges),
            verification_policy=review.plan.verification_policy,
            reorder_acknowledged=review.plan.storyboard.reorder_acknowledged,
        )
        review.plan.validate_confirmation(confirmation)
        issued.confirmation = confirmation
        return confirmation

    def execute(
        self,
        review: ContentReview,
        confirmation: ContentConfirmation,
    ) -> ContentResult:
        issued = self._require_review(review)
        if issued.confirmation != confirmation:
            raise ContentConfirmationError(
                "confirmation was not issued by this pipeline"
            )
        issued.active = True
        try:
            self._check_cancelled()
            self._emit(ContentStatus.RENDERING)
            self._check_cancelled()
            native = self._dependencies.executor.execute(
                plan=review.plan,
                confirmation=confirmation,
                source=issued.source,
                transcript_hash=(
                    issued.transcript.transcript_hash if issued.transcript else None
                ),
                work_root=issued.layout.job_root,
                has_audio=review.preparation.metadata.has_audio,
            )
            self._emit(ContentStatus.VERIFYING)
            self._check_cancelled()
            evidence = self._dependencies.evidence_inspector.inspect(
                plan=review.plan,
                native=native,
                source_metadata=review.preparation.metadata,
                transcript=issued.transcript,
                private_root=issued.layout.private_root,
                feature_config=self.config.features,
            )
            verification = verify_content_result(
                plan=review.plan,
                mappings=native.source_mappings,
                evidence=evidence,
            )
            result = self._finalize(
                issued,
                review,
                native,
                verification,
            )
            return result
        except ContentCancelledError:
            self._emit(ContentStatus.CANCELLED)
            raise
        except ContentError:
            self._emit(ContentStatus.FAILED)
            raise
        finally:
            issued.active = False
            self._release(review.preparation)

    def cancel(self) -> None:
        self._cancelled = True
        self._emit(ContentStatus.CANCELLED)
        for preparation_id in tuple(self._issued):
            issued = self._issued[preparation_id]
            if not issued.active:
                self._release(issued.preparation)

    def close(self) -> None:
        for preparation_id in tuple(self._issued):
            self._release(self._issued[preparation_id].preparation)

    def _finalize(
        self,
        issued: _IssuedContent,
        review: ContentReview,
        native: NativeContentResult,
        verification: ContentVerificationReport,
    ) -> ContentResult:
        status = _status_for_outcome(verification.outcome)
        if verification.outcome not in {
            ContentOutcome.COMPLETED,
            ContentOutcome.PARTIAL,
        }:
            report = build_content_technical_report(
                plan=review.plan,
                verification=verification,
                mappings=native.source_mappings,
                change_log=None,
                artifacts=(),
                warnings=review.preparation.warnings,
                limitations=(
                    "No public bundle was created because verification did not pass.",
                ),
            )
            self._emit(status)
            return ContentResult(status, None, report, verification, ())
        file_sources, preliminary_artifacts = _staged_file_sources(review.plan, native)
        change_log = build_content_change_log(
            plan=review.plan,
            executions=native.action_executions,
            artifacts=preliminary_artifacts,
        )
        technical = build_content_technical_report(
            plan=review.plan,
            verification=verification,
            mappings=native.source_mappings,
            change_log=change_log,
            artifacts=preliminary_artifacts,
            warnings=review.preparation.warnings,
            limitations=(
                "Structural heuristics can miss meaningful context; the exact "
                "source map remains authoritative.",
            ),
        )
        documents = _public_documents(
            review.plan,
            technical,
            change_log,
            self._dependencies.report_renderer(review.plan, technical),
            issued.transcript,
            native.source_mappings,
        )
        artifacts = self._dependencies.publisher(
            issued.layout,
            plan=review.plan,
            verification=verification,
            file_sources=file_sources,
            text_documents=documents,
            cancellation_callback=self._is_cancelled,
        )
        self._emit(status)
        return ContentResult(
            status,
            issued.layout.public_root,
            technical,
            verification,
            artifacts,
        )

    def _load_transcript(self, duration_seconds: float) -> NormalizedTranscript | None:
        if self.config.transcript_path is None:
            return None
        return load_timed_transcript(
            self.config.transcript_path,
            duration_seconds=duration_seconds,
            maximum_cues=self.config.content.maximum_transcript_cues,
        )

    def _require_preparation(self, preparation: ContentPreparation) -> _IssuedContent:
        issued = self._issued.get(id(preparation))
        if issued is None or issued.preparation is not preparation:
            raise ContentConfirmationError(
                "preparation was not issued by this pipeline"
            )
        return issued

    def _require_review(self, review: ContentReview) -> _IssuedContent:
        issued = self._require_preparation(review.preparation)
        if issued.review is not review:
            raise ContentConfirmationError("review was not issued by this pipeline")
        return issued

    def _release(self, preparation: ContentPreparation) -> None:
        issued = self._issued.pop(id(preparation), None)
        if issued is None:
            return
        issued.source.close()
        if not self.config.keep_workspace:
            issued.layout.cleanup_private()

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise ContentCancelledError("content pipeline cancelled")

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def _emit(self, status: ContentStatus) -> None:
        if self._progress is not None:
            self._progress(status)


class LocalContentEvidenceInspector:
    """Measure pending output independently with ffprobe, decode, and join samples."""

    def inspect(
        self,
        *,
        plan: ContentPlan,
        native: NativeContentResult,
        source_metadata: VideoMetadata,
        transcript: NormalizedTranscript | None,
        private_root: Path,
        feature_config: StructuralFeatureConfig,
    ) -> ContentVerificationEvidence:
        metadata = probe_video(
            native.video_path,
            ffprobe=feature_config.ffprobe,
            timeout_seconds=feature_config.command_timeout_seconds,
        )
        decodable = _decode_ok(
            native.video_path,
            ffmpeg=feature_config.ffmpeg,
            timeout_seconds=feature_config.command_timeout_seconds,
        )
        black_regression, repeat_regression = _measure_join_regressions(
            native,
            private_root,
            feature_config,
        )
        av_residual = (
            _measure_stream_start_residual(
                native.video_path,
                ffprobe=feature_config.ffprobe,
                timeout_seconds=feature_config.command_timeout_seconds,
            )
            if source_metadata.has_audio
            else None
        )
        return ContentVerificationEvidence(
            decodable=decodable,
            output_duration_seconds=metadata.duration_seconds,
            has_video=True,
            has_audio=metadata.has_audio,
            expected_has_audio=source_metadata.has_audio,
            black_interval_regression=black_regression,
            repeated_frame_regression=repeat_regression,
            audio_continuity_ok=decodable if source_metadata.has_audio else True,
            av_sync_residual_seconds=av_residual,
            chapter_timing_ok=(
                _measure_chapter_timing(
                    native.video_path,
                    plan,
                    ffprobe=feature_config.ffprobe,
                    timeout_seconds=feature_config.command_timeout_seconds,
                )
                if plan.storyboard.chapters
                else None
            ),
            subtitle_timing_ok=(
                _mapped_transcript_is_valid(transcript, native.source_mappings)
                if plan.effective_config.export_subtitles
                else None
            ),
            public_relative_paths=plan.public_artifacts,
            source_hash_after=native.source_hash_after,
            source_modified=native.source_hash_before != native.source_hash_after,
        )


def _decode_ok(path: Path, *, ffmpeg: str, timeout_seconds: float) -> bool:
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            shell=False,
            capture_output=True,
            timeout=timeout_seconds,
            **pinned_subprocess_options(command),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _measure_join_regressions(
    native: NativeContentResult,
    private_root: Path,
    config: StructuralFeatureConfig,
) -> tuple[bool, bool]:
    if len(native.source_mappings) < 2:
        return False, False
    result = sample_frames(
        native.video_path,
        sample_rate=min(config.sample_fps, 4.0),
        max_edge=config.thumbnail_max_edge,
        image_format="jpeg",
        workspace_parent=private_root / "evidence" / "verification",
        ffmpeg=config.ffmpeg,
        ffprobe=config.ffprobe,
        timeout_seconds=config.command_timeout_seconds,
        max_samples=config.maximum_samples,
        timeline_duration_seconds=native.source_mappings[-1].output_range.end_seconds,
    )
    loaded = tuple(
        (
            sample.timestamp_seconds,
            load_luma_image(result.work_directory, sample),
        )
        for sample in result.samples
    )
    black = False
    repeated = False
    for mapping in native.source_mappings[:-1]:
        join = mapping.output_range.end_seconds
        neighbors = _bracketing_join_samples(loaded, join)
        if neighbors and all(
            compute_luma_metrics(
                frame,
                dark_pixel_threshold=config.near_black_dark_pixel_threshold,
            ).mean_luma
            <= config.near_black_mean_luma_threshold
            for _timestamp, frame in neighbors
        ):
            black = True
        if _has_sustained_repeated_run_at_join(loaded, join, config):
            repeated = True
    return black, repeated


def _bracketing_join_samples(
    loaded: tuple[tuple[float, NDArray[np.uint8]], ...],
    join_seconds: float,
) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
    """Return the nearest sample on each side of a join, never two from one side."""

    left = max(
        (item for item in loaded if item[0] < join_seconds),
        key=lambda item: item[0],
        default=None,
    )
    right = min(
        (item for item in loaded if item[0] >= join_seconds),
        key=lambda item: item[0],
        default=None,
    )
    return tuple(item for item in (left, right) if item is not None)


def _has_sustained_repeated_run_at_join(
    loaded: tuple[tuple[float, NDArray[np.uint8]], ...],
    join_seconds: float,
    config: StructuralFeatureConfig,
) -> bool:
    """Require a configured-duration similar-frame run that crosses the join."""

    ordered = tuple(sorted(loaded, key=lambda item: item[0]))
    boundary_guard = 0.5 / config.sample_fps
    run_start = 0
    for index in range(1, len(ordered)):
        previous = ordered[index - 1][1]
        current = ordered[index][1]
        similar = (
            mean_absolute_difference(previous, current)
            <= config.repeated_max_pixel_difference
            and hash_distance(average_hash(previous), average_hash(current))
            <= config.repeated_max_hash_distance
        )
        if not similar:
            run_start = index
            continue
        start = ordered[run_start][0]
        end = ordered[index][0]
        if (
            start <= join_seconds - boundary_guard
            and end >= join_seconds + boundary_guard
            and end - start >= config.minimum_observation_duration_seconds
        ):
            return True
    return False


def _measure_stream_start_residual(
    path: Path,
    *,
    ffprobe: str,
    timeout_seconds: float,
) -> float | None:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,start_time",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            **pinned_subprocess_options(command),
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        starts = {
            item.get("codec_type"): float(item.get("start_time", 0.0))
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") in {"audio", "video"}
        }
        if "audio" not in starts or "video" not in starts:
            return None
        return starts["audio"] - starts["video"]
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return None


def _measure_chapter_timing(
    path: Path,
    plan: ContentPlan,
    *,
    ffprobe: str,
    timeout_seconds: float,
) -> bool | None:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "chapter=start_time,end_time:chapter_tags=title",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            **pinned_subprocess_options(command),
        )
        if completed.returncode != 0:
            return None
        payload = json.loads(completed.stdout)
        chapters = payload.get("chapters", []) if isinstance(payload, dict) else []
        if len(chapters) != len(plan.storyboard.chapters):
            return False
        for actual, expected in zip(chapters, plan.storyboard.chapters):
            if not isinstance(actual, dict):
                return False
            expected_range = expected.output_range
            if expected_range is None:
                return False
            if not (
                abs(float(actual["start_time"]) - expected_range.start_seconds) <= 0.002
                and abs(float(actual["end_time"]) - expected_range.end_seconds) <= 0.002
            ):
                return False
        return True
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return None


def _staged_file_sources(
    plan: ContentPlan,
    native: NativeContentResult,
) -> tuple[dict[str, Path], tuple[ContentArtifact, ...]]:
    sources: dict[str, Path] = {
        "content-output/useful-content.mp4": native.video_path,
        "content-output/source-map.json": native.source_map_path,
    }
    for declared, path in zip(
        (
            item
            for item in plan.public_artifacts
            if item.startswith("content-output/clips/clip-")
        ),
        native.clip_paths,
        strict=True,
    ):
        sources[declared] = path
    artifacts = tuple(
        _artifact_from_file(declared, path) for declared, path in sources.items()
    )
    return sources, artifacts


def _artifact_from_file(declared: str, path: Path) -> ContentArtifact:
    role = (
        ContentArtifactRole.CLIP
        if "/clips/" in declared
        else ContentArtifactRole.MEDIA
        if declared.endswith(".mp4")
        else ContentArtifactRole.DOCUMENT
    )
    return ContentArtifact(
        role=role,
        relative_path=declared,
        sha256=_hash_path(path),
        description=f"Verified useful-content {role.value} artifact.",
    )


def _public_documents(
    plan: ContentPlan,
    technical: ContentTechnicalReport,
    change_log: ContentChangeLog,
    html: str,
    transcript: NormalizedTranscript | None,
    mappings: tuple[ContentSourceMapping, ...],
) -> dict[str, str]:
    documents: dict[str, str] = {
        "content-output/changes.json": content_change_log_to_json(change_log),
        "content-output/technical-report.json": content_technical_report_to_json(
            technical
        ),
    }
    if "content-output/report.html" in plan.public_artifacts:
        documents["content-output/report.html"] = html
    if "content-output/chapters.json" in plan.public_artifacts:
        documents["content-output/chapters.json"] = json.dumps(
            {
                "schema_version": "0.1",
                "chapters": [
                    item.model_dump(mode="json") for item in plan.storyboard.chapters
                ],
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    if "content-output/subtitles.srt" in plan.public_artifacts:
        if transcript is None:
            raise ContentArtifactError(
                "subtitle export requires a validated transcript"
            )
        documents["content-output/subtitles.srt"] = _transcript_to_srt(
            transcript,
            mappings,
        )
    if "content-output/clips/manifest.json" in plan.public_artifacts:
        documents["content-output/clips/manifest.json"] = json.dumps(
            {
                "schema_version": "0.1",
                "clips": [
                    item
                    for item in plan.public_artifacts
                    if item.startswith("content-output/clips/clip-")
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    return documents


def _transcript_to_srt(
    transcript: NormalizedTranscript,
    mappings: tuple[ContentSourceMapping, ...],
) -> str:
    lines: list[str] = []
    for index, (start, end, text) in enumerate(
        _mapped_transcript_cues(transcript, mappings),
        start=1,
    ):
        lines.extend(
            (
                str(index),
                f"{_srt_time(start)} --> {_srt_time(end)}",
                text,
                "",
            )
        )
    return "\n".join(lines)


def _mapped_transcript_cues(
    transcript: NormalizedTranscript,
    mappings: tuple[ContentSourceMapping, ...],
) -> tuple[tuple[float, float, str], ...]:
    cues: list[tuple[float, float, str]] = []
    for mapping in mappings:
        offset = mapping.output_range.start_seconds - mapping.source_range.start_seconds
        for cue in transcript.cues:
            start = max(cue.start_seconds, mapping.source_range.start_seconds)
            end = min(cue.end_seconds, mapping.source_range.end_seconds)
            if end > start:
                cues.append((start + offset, end + offset, cue.text))
    return tuple(sorted(cues, key=lambda item: (item[0], item[1], item[2])))


def _mapped_transcript_is_valid(
    transcript: NormalizedTranscript | None,
    mappings: tuple[ContentSourceMapping, ...],
) -> bool:
    if transcript is None:
        return False
    cues = _mapped_transcript_cues(transcript, mappings)
    if not cues:
        return False
    output_end = mappings[-1].output_range.end_seconds if mappings else 0.0
    return all(
        0 <= start < end <= output_end + 1e-6
        and (index == 0 or start >= cues[index - 1][0])
        for index, (start, end, _text) in enumerate(cues)
    )


def _srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def _hash_path(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _status_for_outcome(outcome: ContentOutcome) -> ContentStatus:
    return {
        ContentOutcome.COMPLETED: ContentStatus.COMPLETED,
        ContentOutcome.PARTIAL: ContentStatus.PARTIAL,
        ContentOutcome.NEEDS_REVIEW: ContentStatus.NEEDS_REVIEW,
        ContentOutcome.FAILED: ContentStatus.FAILED,
        ContentOutcome.CANCELLED: ContentStatus.CANCELLED,
    }[outcome]


__all__ = [
    "ContentEvidenceInspector",
    "ContentExecutorService",
    "ContentPipelineConfig",
    "ContentPipelineDependencies",
    "ContentPreparation",
    "ContentPreviewService",
    "ContentResult",
    "ContentReview",
    "ContentStatus",
    "LocalContentEvidenceInspector",
    "LongVideoContentPipeline",
]
