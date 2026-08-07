"""Private bounded preview creation and identity binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.content.errors import ContentCancelledError, ContentPreviewError
from videoscope.content.models import (
    ContentAction,
    ContentActionKind,
    ContentTimeRange,
    make_action_id,
)
from videoscope.content.preview import (
    ContentJoinPreview,
    ContentPreviewBuilder,
    RetainedContentSource,
    assess_previews,
    build_preview_extract_command,
    clear_private_previews,
    make_preview_identity,
    preview_context_ranges,
)
from videoscope.video.hashing import compute_file_sha256

INPUT_HASH = "a" * 64


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def action(
    start: float,
    end: float,
    *,
    kind: ContentActionKind = ContentActionKind.REMOVE,
) -> ContentAction:
    ranges = (time_range(start, end),)
    return ContentAction(
        id=make_action_id(INPUT_HASH, kind, ranges, 0),
        version="1",
        kind=kind,
        description="Review this exact content change.",
        source_ranges=ranges,
        changes_content=True,
        requires_confirmation=True,
    )


class WritingRunner:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.commands: list[list[str]] = []
        self.fail_at = fail_at

    def run(self, command: list[str]) -> None:
        self.commands.append(command)
        if self.fail_at == len(self.commands):
            raise ContentPreviewError("injected preview failure")
        Path(command[-1]).write_bytes(f"preview-{len(self.commands)}".encode())


def retained_source(tmp_path: Path) -> tuple[Path, RetainedContentSource]:
    source = tmp_path / "源 video with spaces.mp4"
    source.write_bytes(b"source-bytes")
    return source, RetainedContentSource(
        source,
        expected_hash=compute_file_sha256(source),
    )


def test_preview_ranges_are_bounded_at_source_edges_and_maximum() -> None:
    middle = preview_context_ranges(
        action(4, 6), duration_seconds=10, maximum_preview_seconds=4
    )
    first = preview_context_ranges(
        action(0, 2), duration_seconds=10, maximum_preview_seconds=4
    )
    last = preview_context_ranges(
        action(8, 10), duration_seconds=10, maximum_preview_seconds=4
    )

    assert middle == (time_range(2, 4), time_range(6, 8))
    assert first == (time_range(2, 4),)
    assert last == (time_range(6, 8),)
    assert sum(item.duration_seconds for item in middle) <= 4

    selected = action(2, 8, kind=ContentActionKind.CONCATENATE)
    assert preview_context_ranges(
        selected, duration_seconds=10, maximum_preview_seconds=4
    ) == (time_range(3, 7),)


def test_preview_command_is_argument_vector_and_preserves_unicode_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "输入 video.mp4"
    output = tmp_path / "输出 preview.mp4"
    command = build_preview_extract_command(
        "ffmpeg.exe", source, output, time_range(1, 2)
    )

    assert command[0] == "ffmpeg.exe"
    assert str(source) in command
    assert command[-1] == str(output)
    assert "shell=True" not in command


def test_builder_creates_only_private_paths_and_deterministic_identity(
    tmp_path: Path,
) -> None:
    _source_path, source = retained_source(tmp_path)
    runner = WritingRunner()
    review_root = tmp_path / "content-review-private"
    selected_action = action(4, 6)
    try:
        first = ContentPreviewBuilder(runner).build(
            source=source,
            transcript_hash="b" * 64,
            actions=(selected_action,),
            duration_seconds=10,
            private_review_root=review_root,
            maximum_preview_seconds=4,
            has_audio=False,
        )
        clear_private_previews(review_root)
        runner.commands.clear()
        second = ContentPreviewBuilder(runner).build(
            source=source,
            transcript_hash="b" * 64,
            actions=(selected_action,),
            duration_seconds=10,
            private_review_root=review_root,
            maximum_preview_seconds=4,
            has_audio=False,
        )
    finally:
        source.close()

    assert first[0].identity == second[0].identity
    assert all(path.startswith("preview/") for path in second[0].relative_paths)
    assert all("源" not in path for path in second[0].relative_paths)
    assert all((review_root / path).is_file() for path in second[0].relative_paths)


def test_preview_identity_changes_with_transcript_parameters_or_bytes() -> None:
    selected_action = action(4, 6)
    contexts = (time_range(2, 4), time_range(6, 8))

    def identity_for(
        *,
        transcript_hash: str = "b" * 64,
        artifact_hash: str = "c" * 64,
        codec: str = "h264",
    ) -> str:
        return make_preview_identity(
            input_hash=INPUT_HASH,
            transcript_hash=transcript_hash,
            action=selected_action,
            context_ranges=contexts,
            encoding_parameters={"codec": codec},
            artifact_hashes=(artifact_hash,),
        )

    identity = identity_for()
    assert identity != identity_for(transcript_hash="d" * 64)
    assert identity != identity_for(artifact_hash="e" * 64)
    assert identity != identity_for(codec="av1")


def test_missing_or_stale_preview_blocks_only_affected_action() -> None:
    first = action(2, 3)
    second_ranges = (time_range(7, 8),)
    second = ContentAction(
        id=make_action_id(INPUT_HASH, ContentActionKind.REMOVE, second_ranges, 1),
        version="1",
        kind=ContentActionKind.REMOVE,
        description="Second exact change.",
        source_ranges=second_ranges,
        changes_content=True,
        requires_confirmation=True,
    )
    valid = ContentJoinPreview(
        action_id=first.id,
        action_ranges=first.source_ranges,
        context_ranges=(time_range(1, 2), time_range(3, 4)),
        relative_paths=("preview/action.mp4",),
        artifact_hashes=("f" * 64,),
        encoding_parameters={},
        identity="preview-valid",
    )

    assessment = assess_previews((first, second), (valid,))

    assert assessment.identities == {first.id: "preview-valid"}
    assert assessment.blocked_action_ids == (second.id,)

    stale = ContentJoinPreview(
        action_id=first.id,
        action_ranges=(time_range(2, 4),),
        context_ranges=valid.context_ranges,
        relative_paths=valid.relative_paths,
        artifact_hashes=valid.artifact_hashes,
        encoding_parameters={},
        identity="preview-stale",
    )
    assert assess_previews((first,), (stale,)).blocked_action_ids == (first.id,)


def test_failure_and_cancellation_clean_private_outputs(tmp_path: Path) -> None:
    _source_path, source = retained_source(tmp_path)
    review_root = tmp_path / "content-review-private"
    try:
        with pytest.raises(ContentPreviewError):
            ContentPreviewBuilder(WritingRunner(fail_at=2)).build(
                source=source,
                transcript_hash=None,
                actions=(action(4, 6),),
                duration_seconds=10,
                private_review_root=review_root,
                maximum_preview_seconds=4,
                has_audio=False,
            )
        assert not (review_root / "preview").exists()

        with pytest.raises(ContentCancelledError):
            ContentPreviewBuilder(WritingRunner()).build(
                source=source,
                transcript_hash=None,
                actions=(action(4, 6),),
                duration_seconds=10,
                private_review_root=review_root,
                maximum_preview_seconds=4,
                has_audio=False,
                cancelled=lambda: True,
            )
        assert not (review_root / "preview").exists()
    finally:
        source.close()


def test_retained_source_owns_and_releases_descriptor(tmp_path: Path) -> None:
    _path, source = retained_source(tmp_path)
    assert not source.closed
    assert source.input_path

    source.close()
    source.close()

    assert source.closed
    with pytest.raises(ContentPreviewError):
        _ = source.input_path


def test_preview_root_must_be_private(tmp_path: Path) -> None:
    _source_path, source = retained_source(tmp_path)
    try:
        with pytest.raises(ContentPreviewError):
            ContentPreviewBuilder(WritingRunner()).build(
                source=source,
                transcript_hash=None,
                actions=(action(4, 6),),
                duration_seconds=10,
                private_review_root=tmp_path / "public",
                maximum_preview_seconds=4,
                has_audio=False,
            )
    finally:
        source.close()
