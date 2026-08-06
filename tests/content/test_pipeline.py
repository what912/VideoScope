"""Core useful-content lifecycle shared by CLI and Web adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from videoscope.content.errors import (
    ContentCancelledError,
    ContentConfirmationError,
    ContentMediaError,
    ContentPreviewError,
)
from videoscope.content.executor import NativeContentResult
from videoscope.content.features import ContentFeatureBundle, StructuralFeatureConfig
from videoscope.content.models import (
    ContentAction,
    ContentActionExecution,
    ContentConfig,
    ContentConfirmation,
    ContentExecutionStatus,
    ContentGoal,
    ContentMappingState,
    ContentPlan,
    ContentProviderExecution,
    ContentProviderStatus,
    ContentSourceMapping,
    ContentTimeRange,
    ContentTransition,
    ContentUserRange,
    ContentUserRangeKind,
    make_mapping_id,
    make_user_range_id,
)
from videoscope.content.pipeline import (
    ContentPipelineConfig,
    ContentPipelineDependencies,
    ContentResult,
    ContentStatus,
    LongVideoContentPipeline,
)
from videoscope.content.preview import ContentJoinPreview, RetainedContentSource
from videoscope.content.verification import ContentVerificationEvidence
from videoscope.domain import VideoMetadata
from videoscope.scenes import VideoScene
from videoscope.video.hashing import compute_file_sha256


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def source_file(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "长 video source.mp4"
    path.write_bytes(b"stable-pipeline-source")
    return path, compute_file_sha256(path)


def selected_range(input_hash: str, start: float, end: float) -> ContentUserRange:
    source_range = time_range(start, end)
    return ContentUserRange(
        id=make_user_range_id(
            input_hash,
            ContentUserRangeKind.KEEP,
            source_range,
        ),
        kind=ContentUserRangeKind.KEEP,
        source_range=source_range,
        label="Selected moment",
    )


def fake_features(
    input_path: Path,
    *,
    input_hash: str,
    workspace: Path,
    config: StructuralFeatureConfig,
    cancellation_callback: Callable[[], bool] | None = None,
) -> ContentFeatureBundle:
    del input_path, input_hash, config
    if cancellation_callback is not None and cancellation_callback():
        raise ContentCancelledError("cancelled feature collection")
    workspace.mkdir(parents=True, exist_ok=True)
    metadata = VideoMetadata(
        filename="source.mp4",
        container_format="mp4",
        codec="h264",
        width=320,
        height=180,
        duration_seconds=10,
        average_frame_rate=10,
        estimated_frame_count=100,
        has_audio=False,
        file_size_bytes=100,
        raw_probe={},
    )
    return ContentFeatureBundle(
        metadata=metadata,
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0,
                end_seconds=5,
                duration_seconds=5,
                representative_timestamp=2.5,
            ),
            VideoScene(
                scene_index=1,
                start_seconds=5,
                end_seconds=10,
                duration_seconds=5,
                representative_timestamp=7.5,
            ),
        ),
        frame_samples=(),
        frame_workspace=workspace,
        observations=(),
        executions=(
            ContentProviderExecution(
                provider_id="metadata",
                provider_version="1",
                status=ContentProviderStatus.OK,
            ),
        ),
        warnings=(),
    )


def failing_features(
    input_path: Path,
    *,
    input_hash: str,
    workspace: Path,
    config: StructuralFeatureConfig,
    cancellation_callback: Callable[[], bool] | None = None,
) -> ContentFeatureBundle:
    del input_path, input_hash, workspace, config, cancellation_callback
    raise ContentMediaError("injected provider failure")


class FakePreviewBuilder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

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
    ) -> tuple[ContentJoinPreview, ...]:
        del (
            source,
            transcript_hash,
            duration_seconds,
            private_review_root,
            maximum_preview_seconds,
            has_audio,
            cancelled,
        )
        if self.fail:
            raise ContentPreviewError("injected preview failure")
        return tuple(
            ContentJoinPreview(
                action_id=action.id,
                action_ranges=action.source_ranges,
                context_ranges=action.source_ranges,
                relative_paths=(f"preview/{action.id}.mp4",),
                artifact_hashes=("f" * 64,),
                encoding_parameters={"codec": "fake"},
                identity=f"preview-{action.id}",
            )
            for action in actions
            if action.changes_content and action.requires_confirmation
        )


class FakeExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def execute(
        self,
        *,
        plan: ContentPlan,
        confirmation: ContentConfirmation,
        source: RetainedContentSource,
        transcript_hash: str | None,
        work_root: Path,
        has_audio: bool,
    ) -> NativeContentResult:
        del confirmation, transcript_hash, has_audio
        if self.fail:
            raise ContentMediaError("injected render failure")
        pending = work_root / "content-pending"
        pending.mkdir(parents=True, exist_ok=True)
        video = pending / "useful-content.mp4"
        source_map = pending / "source-map.json"
        video.write_bytes(b"rendered-media")
        kept = sorted(
            (
                item
                for item in plan.storyboard.items
                if item.output_order_index is not None
            ),
            key=lambda item: (
                item.output_order_index if item.output_order_index is not None else -1
            ),
        )
        mappings: list[ContentSourceMapping] = []
        cursor = 0.0
        for output_index, item in enumerate(kept):
            output_range = time_range(
                cursor,
                cursor + item.source_range.duration_seconds,
            )
            mappings.append(
                ContentSourceMapping(
                    id=make_mapping_id(
                        plan.input_hash,
                        output_range,
                        item.source_range,
                        output_index,
                    ),
                    output_range=output_range,
                    source_range=item.source_range,
                    source_order_index=item.source_order_index,
                    output_order_index=output_index,
                    transition=ContentTransition.HARD_JOIN,
                    state=ContentMappingState.UNCHANGED,
                    storyboard_item_id=item.id,
                )
            )
            cursor = output_range.end_seconds
        source_map.write_text(
            json.dumps(
                {"mappings": [item.model_dump(mode="json") for item in mappings]}
            ),
            encoding="utf-8",
        )
        return NativeContentResult(
            pending_root=pending,
            video_path=video,
            source_map_path=source_map,
            source_mappings=tuple(mappings),
            clip_paths=(),
            action_executions=tuple(
                ContentActionExecution(
                    action_id=action.id,
                    status=ContentExecutionStatus.SUCCEEDED,
                )
                for action in plan.actions
            ),
            source_hash_before=source.source_hash,
            source_hash_after=source.source_hash,
        )


class FakeInspector:
    def __init__(self, *, decodable: bool = True) -> None:
        self.decodable = decodable

    def inspect(
        self,
        *,
        plan: ContentPlan,
        native: NativeContentResult,
        source_metadata: VideoMetadata,
        transcript: object | None,
        private_root: Path,
        feature_config: StructuralFeatureConfig,
    ) -> ContentVerificationEvidence:
        del transcript, private_root, feature_config
        duration = native.source_mappings[-1].output_range.end_seconds
        return ContentVerificationEvidence(
            decodable=self.decodable,
            output_duration_seconds=duration,
            has_video=True,
            has_audio=source_metadata.has_audio,
            expected_has_audio=source_metadata.has_audio,
            black_interval_regression=False,
            repeated_frame_regression=False,
            audio_continuity_ok=True,
            av_sync_residual_seconds=None,
            chapter_timing_ok=True if plan.storyboard.chapters else None,
            subtitle_timing_ok=None,
            public_relative_paths=plan.public_artifacts,
            source_hash_after=native.source_hash_after,
            source_modified=False,
        )


def dependencies(
    *,
    provider_fail: bool = False,
    preview_fail: bool = False,
    render_fail: bool = False,
    decodable: bool = True,
) -> ContentPipelineDependencies:
    return ContentPipelineDependencies(
        feature_collector=failing_features if provider_fail else fake_features,
        preview_builder=FakePreviewBuilder(fail=preview_fail),
        executor=FakeExecutor(fail=render_fail),
        evidence_inspector=FakeInspector(decodable=decodable),
    )


def run_reviewed(
    pipeline: LongVideoContentPipeline,
    source: Path,
) -> ContentResult:
    prepared = pipeline.prepare(source)
    reviewed = pipeline.preview(prepared)
    accepted = tuple(
        action.id
        for action in reviewed.plan.actions
        if action.changes_content and action.requires_confirmation
    )
    confirmation = pipeline.confirm(reviewed, accepted_action_ids=accepted)
    return pipeline.execute(reviewed, confirmation)


def assert_goal_completes_with_deterministic_progress(
    tmp_path: Path,
    goal: ContentGoal,
) -> None:
    source, _digest = source_file(tmp_path)
    events: list[ContentStatus] = []
    pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(
            output_directory=tmp_path / f"job-{goal.value}",
            content=ContentConfig(
                goal=goal,
                minimum_chapter_duration_seconds=2,
            ),
        ),
        dependencies=dependencies(),
        progress=events.append,
    )

    result = run_reviewed(pipeline, source)

    assert result.status is ContentStatus.COMPLETED
    assert result.public_root is not None and result.public_root.is_dir()
    assert events == [
        ContentStatus.CREATED,
        ContentStatus.PROBING,
        ContentStatus.MAPPING,
        ContentStatus.PLANNING,
        ContentStatus.AWAITING_REVIEW,
        ContentStatus.PREVIEWING,
        ContentStatus.READY_TO_CONFIRM,
        ContentStatus.RENDERING,
        ContentStatus.VERIFYING,
        ContentStatus.COMPLETED,
    ]


def test_faithful_clean_completes_with_deterministic_progress(
    tmp_path: Path,
) -> None:
    assert_goal_completes_with_deterministic_progress(
        tmp_path,
        ContentGoal.FAITHFUL_CLEAN,
    )


def test_chaptered_full_completes_with_deterministic_progress(
    tmp_path: Path,
) -> None:
    assert_goal_completes_with_deterministic_progress(
        tmp_path,
        ContentGoal.CHAPTERED_FULL,
    )


def test_selected_clips_uses_only_explicit_range(tmp_path: Path) -> None:
    source, digest = source_file(tmp_path)
    selected = selected_range(digest, 2, 4)
    pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(
            output_directory=tmp_path / "selected-job",
            content=ContentConfig(goal=ContentGoal.SELECTED_CLIPS),
            user_ranges=(selected,),
        ),
        dependencies=dependencies(),
    )

    result = run_reviewed(pipeline, source)

    assert result.status is ContentStatus.COMPLETED
    assert [item.source_range for item in result.technical_report.source_mappings] == [
        time_range(2, 4)
    ]


def test_no_safe_removal_keeps_full_source(tmp_path: Path) -> None:
    source, _digest = source_file(tmp_path)
    pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "fallback"),
        dependencies=dependencies(),
    )

    review = pipeline.preview(pipeline.prepare(source))

    assert not any(action.changes_content for action in review.plan.actions)
    assert review.plan.storyboard.estimated_output_duration_seconds == 10
    confirmation = pipeline.confirm(review, accepted_action_ids=())
    result = pipeline.execute(review, confirmation)
    assert [item.source_range for item in result.technical_report.source_mappings] == [
        time_range(0, 10)
    ]


def test_stale_review_and_inexact_confirmation_are_rejected(tmp_path: Path) -> None:
    source, _digest = source_file(tmp_path)
    first = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "one"),
        dependencies=dependencies(),
    )
    second = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "two"),
        dependencies=dependencies(),
    )
    review = first.preview(first.prepare(source))
    with pytest.raises(ContentConfirmationError):
        second.confirm(review, accepted_action_ids=())
    with pytest.raises(ContentConfirmationError):
        first.confirm(review, accepted_action_ids=("action_" + "0" * 64,))
    first.close()
    second.close()


def test_preview_render_verification_and_cancel_fail_without_publication(
    tmp_path: Path,
) -> None:
    source, _digest = source_file(tmp_path)
    preview_pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "preview-fail"),
        dependencies=dependencies(preview_fail=True),
    )
    prepared = preview_pipeline.prepare(source)
    with pytest.raises(ContentPreviewError):
        preview_pipeline.preview(prepared)
    preview_pipeline.close()

    render_pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "render-fail"),
        dependencies=dependencies(render_fail=True),
    )
    with pytest.raises(ContentMediaError):
        run_reviewed(render_pipeline, source)
    assert not (tmp_path / "render-fail" / "content-output").exists()

    verify_pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "verify-fail"),
        dependencies=dependencies(decodable=False),
    )
    result = run_reviewed(verify_pipeline, source)
    assert result.status is ContentStatus.FAILED
    assert result.public_root is None

    cancelled = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "cancelled"),
        dependencies=dependencies(),
    )
    cancelled.cancel()
    with pytest.raises(ContentCancelledError):
        cancelled.prepare(source)


def test_provider_failure_and_retry_cleanup_are_deterministic(tmp_path: Path) -> None:
    source, _digest = source_file(tmp_path)
    failed_output = tmp_path / "provider-fail"
    failed = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=failed_output),
        dependencies=dependencies(provider_fail=True),
    )
    with pytest.raises(ContentMediaError):
        failed.prepare(source)
    assert not (failed_output / "content-review-private").exists()

    retry_output = tmp_path / "retry"
    first = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=retry_output),
        dependencies=dependencies(render_fail=True),
    )
    with pytest.raises(ContentMediaError):
        run_reviewed(first, source)
    assert not (retry_output / "content-pending").exists()
    second = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=retry_output),
        dependencies=dependencies(),
    )
    assert run_reviewed(second, source).status is ContentStatus.COMPLETED


def test_keep_workspace_preserves_private_review_and_default_cleans(
    tmp_path: Path,
) -> None:
    source, _digest = source_file(tmp_path)
    kept_output = tmp_path / "kept"
    kept = LongVideoContentPipeline(
        ContentPipelineConfig(
            output_directory=kept_output,
            keep_workspace=True,
        ),
        dependencies=dependencies(),
    )
    assert run_reviewed(kept, source).status is ContentStatus.COMPLETED
    assert (kept_output / "content-review-private" / "plan.json").is_file()
    assert (kept_output / "content-pending").is_dir()

    clean_output = tmp_path / "clean"
    clean = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=clean_output),
        dependencies=dependencies(),
    )
    assert run_reviewed(clean, source).status is ContentStatus.COMPLETED
    assert not (clean_output / "content-review-private").exists()
    assert not (clean_output / "content-pending").exists()


def test_cancellation_is_honored_at_every_nonterminal_stage(tmp_path: Path) -> None:
    stages = (
        ContentStatus.CREATED,
        ContentStatus.PROBING,
        ContentStatus.MAPPING,
        ContentStatus.PLANNING,
        ContentStatus.AWAITING_REVIEW,
        ContentStatus.PREVIEWING,
        ContentStatus.READY_TO_CONFIRM,
        ContentStatus.RENDERING,
        ContentStatus.VERIFYING,
    )
    source, _digest = source_file(tmp_path)
    for stage in stages:
        output = tmp_path / stage.value
        holder: list[LongVideoContentPipeline] = []

        def progress(status: ContentStatus) -> None:
            if status is stage:
                holder[0].cancel()

        pipeline = LongVideoContentPipeline(
            ContentPipelineConfig(output_directory=output),
            dependencies=dependencies(),
            progress=progress,
        )
        holder.append(pipeline)
        with pytest.raises(ContentCancelledError):
            run_reviewed(pipeline, source)
        assert not (output / "content-output").exists()
        assert not (output / "content-review-private").exists()
        assert not (output / "content-pending").exists()


def test_rerun_plan_is_deterministic_except_private_job_state(tmp_path: Path) -> None:
    source, _digest = source_file(tmp_path)
    first = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "first"),
        dependencies=dependencies(),
    )
    second = LongVideoContentPipeline(
        ContentPipelineConfig(output_directory=tmp_path / "second"),
        dependencies=dependencies(),
    )
    first_review = first.preview(first.prepare(source))
    second_review = second.preview(second.prepare(source))
    assert first_review.plan.plan_digest == second_review.plan.plan_digest
    first.close()
    second.close()
