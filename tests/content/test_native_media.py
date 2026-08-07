"""Real FFmpeg gates for the three useful-content goals."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from videoscope.content import (
    ContentConfig,
    ContentGoal,
    ContentPipelineConfig,
    ContentPipelineDependencies,
    ContentResult,
    ContentStatus,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    LongVideoContentPipeline,
    StructuralFeatureConfig,
    make_user_range_id,
)
from videoscope.content.executor import NativeContentExecutor, probe_content_duration
from videoscope.content.preview import ContentPreviewBuilder
from videoscope.video import compute_file_sha256, probe_video

GENERATED = Path(__file__).resolve().parents[1] / "fixtures" / "generated"


def _tools() -> tuple[str, str]:
    ffmpeg = os.environ.get("VIDEOSCOPE_TEST_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_TEST_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required for native content gates")
    assert ffmpeg is not None and ffprobe is not None
    return ffmpeg, ffprobe


def _range(
    input_hash: str,
    kind: ContentUserRangeKind,
    start: float,
    end: float,
    label: str,
) -> ContentUserRange:
    source_range = ContentTimeRange(start_seconds=start, end_seconds=end)
    return ContentUserRange(
        id=make_user_range_id(input_hash, kind, source_range),
        kind=kind,
        source_range=source_range,
        label=label,
    )


def _pipeline(
    tmp_path: Path,
    source: Path,
    goal: ContentGoal,
    ranges: tuple[ContentUserRange, ...],
    *,
    transcript: Path | None = None,
    allow_reorder: bool = False,
) -> LongVideoContentPipeline:
    ffmpeg, ffprobe = _tools()
    return LongVideoContentPipeline(
        ContentPipelineConfig(
            output_directory=tmp_path,
            content=ContentConfig(
                goal=goal,
                minimum_chapter_duration_seconds=2,
                export_clips=goal is ContentGoal.SELECTED_CLIPS,
                export_subtitles=transcript is not None,
                allow_reorder=allow_reorder,
            ),
            features=StructuralFeatureConfig(
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                sample_fps=2,
                maximum_samples=100,
            ),
            transcript_path=transcript,
            user_ranges=ranges,
        ),
        dependencies=ContentPipelineDependencies(
            preview_builder=ContentPreviewBuilder(ffmpeg_executable=ffmpeg),
            executor=NativeContentExecutor(
                ffmpeg=ffmpeg,
                duration_probe=lambda path: probe_content_duration(
                    path, ffprobe=ffprobe
                ),
            ),
        ),
    )


def _execute(
    pipeline: LongVideoContentPipeline, source: Path
) -> tuple[object, ContentResult]:
    prepared = pipeline.prepare(source)
    review = pipeline.preview(prepared)
    accepted = tuple(
        action.id
        for action in review.plan.actions
        if action.changes_content and action.requires_confirmation
    )
    confirmation = pipeline.confirm(review, accepted_action_ids=accepted)
    return review, pipeline.execute(review, confirmation)


def _pairs(result: ContentResult) -> list[tuple[float, float]]:
    return [
        (mapping.source_range.start_seconds, mapping.source_range.end_seconds)
        for mapping in result.technical_report.source_mappings
    ]


def test_faithful_clean_honors_locked_context_and_preserves_source(
    tmp_path: Path,
) -> None:
    source = GENERATED / "content_locked_context.mp4"
    if not source.is_file():
        pytest.skip("generate content fixtures before native media gates")
    source_hash = compute_file_sha256(source)
    ranges = (
        _range(source_hash, ContentUserRangeKind.EXCLUDE, 4, 8, "Remove gap"),
        _range(source_hash, ContentUserRangeKind.LOCKED_KEEP, 5, 7, "Keep context"),
    )
    pipeline = _pipeline(
        tmp_path / "faithful", source, ContentGoal.FAITHFUL_CLEAN, ranges
    )

    _review, result = _execute(pipeline, source)

    assert result.status is ContentStatus.COMPLETED, [
        (item.check_id, item.status, item.message, item.measured)
        for item in result.technical_report.verification.checks
        if item.status.value != "passed"
    ]
    assert _pairs(result) == [(0.0, 4.0), (5.0, 7.0), (8.0, 12.0)]
    assert compute_file_sha256(source) == source_hash
    assert result.public_root is not None
    media = next(
        item for item in result.artifacts if item.relative_path.endswith(".mp4")
    )
    output = tmp_path / "faithful" / media.relative_path
    assert probe_video(output, ffprobe=_tools()[1]).duration_seconds == pytest.approx(
        10, abs=0.25
    )
    assert all(check.status.value == "passed" for check in result.verification.checks)


def test_chaptered_full_retains_complete_timeline_and_unicode_subtitles(
    tmp_path: Path,
) -> None:
    source = GENERATED / "content_tutorial_chapters.mp4"
    transcript = GENERATED / "content_tutorial_zh.vtt"
    if not source.is_file() or not transcript.is_file():
        pytest.skip("generate content fixtures before native media gates")
    source_hash = compute_file_sha256(source)
    ranges = tuple(
        _range(source_hash, ContentUserRangeKind.CHAPTER, start, end, title)
        for start, end, title in ((0, 4, "准备"), (4, 8, "操作"), (8, 12, "复盘"))
    )
    pipeline = _pipeline(
        tmp_path / "chaptered",
        source,
        ContentGoal.CHAPTERED_FULL,
        ranges,
        transcript=transcript,
    )

    _review, result = _execute(pipeline, source)

    assert result.status is ContentStatus.COMPLETED, [
        (item.check_id, item.status, item.message, item.measured)
        for item in result.technical_report.verification.checks
        if item.status.value != "passed"
    ]
    assert _pairs(result) == [(0.0, 12.0)]
    assert [
        (item.source_range.start_seconds, item.source_range.end_seconds)
        for item in result.technical_report.chapters
    ] == [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)]
    assert any(item.role.value == "subtitle" for item in result.artifacts)


@pytest.mark.parametrize("reordered", [False, True])  # type: ignore[untyped-decorator]
def test_selected_clips_export_exact_ranges_and_explicit_order(
    tmp_path: Path, reordered: bool
) -> None:
    source = GENERATED / "content_join_regression.mp4"
    if not source.is_file():
        pytest.skip("generate content fixtures before native media gates")
    source_hash = compute_file_sha256(source)
    ranges = tuple(
        _range(source_hash, ContentUserRangeKind.KEEP, start, end, f"Clip {index}")
        for index, (start, end) in enumerate(((1, 3), (5, 7), (9, 11)), start=1)
    )
    pipeline = _pipeline(
        tmp_path / ("reordered" if reordered else "ordered"),
        source,
        ContentGoal.SELECTED_CLIPS,
        ranges,
        allow_reorder=reordered,
    )
    prepared = pipeline.prepare(source)
    if reordered:
        prepared = pipeline.revise(
            prepared,
            selected_range_order=(ranges[2].id, ranges[0].id, ranges[1].id),
            reorder_acknowledged=True,
        )
    review = pipeline.preview(prepared)
    accepted = tuple(
        action.id for action in review.plan.actions if action.changes_content
    )
    result = pipeline.execute(
        review, pipeline.confirm(review, accepted_action_ids=accepted)
    )

    expected = (
        [(9.0, 11.0), (1.0, 3.0), (5.0, 7.0)]
        if reordered
        else [(1.0, 3.0), (5.0, 7.0), (9.0, 11.0)]
    )
    assert result.status is ContentStatus.COMPLETED, [
        (item.check_id, item.status, item.message, item.measured)
        for item in result.technical_report.verification.checks
        if item.status.value != "passed"
    ]
    assert _pairs(result) == expected
    assert len([item for item in result.artifacts if item.role.value == "clip"]) == 3
