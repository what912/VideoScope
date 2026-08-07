"""Deterministic ContentMap construction from read-only structural evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.content.features import (
    ContentFeatureBundle,
    ContentObservation,
)
from videoscope.content.mapping import build_content_map
from videoscope.content.models import (
    ContentConfig,
    ContentProviderExecution,
    ContentProviderStatus,
    ContentSelectionEligibility,
    ContentSignalType,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    make_user_range_id,
)
from videoscope.content.transcript import parse_timed_transcript
from videoscope.domain import VideoMetadata
from videoscope.scenes import VideoScene

INPUT_HASH = "a" * 64


def make_metadata() -> VideoMetadata:
    return VideoMetadata(
        filename="long video.mp4",
        container_format="mov,mp4",
        codec="h264",
        width=320,
        height=180,
        duration_seconds=10.0,
        average_frame_rate=10.0,
        estimated_frame_count=100,
        has_audio=True,
        file_size_bytes=100,
        raw_probe={},
    )


def observation(
    kind: ContentSignalType, start: float, end: float, provider_id: str
) -> ContentObservation:
    return ContentObservation.create(
        input_hash=INPUT_HASH,
        signal_type=kind,
        source_range=ContentTimeRange(start_seconds=start, end_seconds=end),
        provider_id=provider_id,
        provider_version="1",
        measurements={"value": 1},
        parameters={"threshold": 0.5},
        limitations=("This is observable structural evidence.",),
    )


def make_bundle(
    tmp_path: Path,
    *,
    observations: tuple[ContentObservation, ...],
    failed_provider: bool = False,
) -> ContentFeatureBundle:
    executions = [
        ContentProviderExecution(
            provider_id="metadata",
            provider_version="1",
            status=ContentProviderStatus.OK,
        ),
        ContentProviderExecution(
            provider_id="scenes",
            provider_version="1",
            status=ContentProviderStatus.OK,
        ),
    ]
    warnings: tuple[str, ...] = ()
    if failed_provider:
        executions.append(
            ContentProviderExecution(
                provider_id="silence",
                provider_version="1",
                status=ContentProviderStatus.FAILED,
                warning="silence feature provider failed (RuntimeError).",
            )
        )
        warnings = ("silence feature provider failed (RuntimeError).",)
    return ContentFeatureBundle(
        metadata=make_metadata(),
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=5.0,
                duration_seconds=5.0,
                representative_timestamp=2.5,
            ),
            VideoScene(
                scene_index=1,
                start_seconds=5.0,
                end_seconds=10.0,
                duration_seconds=5.0,
                representative_timestamp=7.5,
            ),
        ),
        frame_samples=(),
        frame_workspace=tmp_path,
        observations=observations,
        executions=tuple(executions),
        warnings=warnings,
    )


def test_boundary_union_is_contiguous_and_aligns_scenes_and_observations(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(
        tmp_path,
        observations=(
            observation(ContentSignalType.SILENCE, 2.0, 6.0, "silence"),
            observation(
                ContentSignalType.LOW_VISUAL_CHANGE, 3.0, 4.0, "visual_structure"
            ),
        ),
    )

    content_map = build_content_map(
        bundle,
        input_hash=INPUT_HASH,
        effective_config=ContentConfig(
            minimum_candidate_duration_seconds=0.5,
        ),
    )

    assert [
        (segment.source_range.start_seconds, segment.source_range.end_seconds)
        for segment in content_map.segments
    ] == [
        (0.0, 2.0),
        (2.0, 3.0),
        (3.0, 4.0),
        (4.0, 5.0),
        (5.0, 6.0),
        (6.0, 10.0),
    ]
    assert (
        content_map.segments[2].selection_eligibility
        is ContentSelectionEligibility.ELIGIBLE
    )
    assert (
        content_map.segments[1].selection_eligibility
        is ContentSelectionEligibility.MANUAL_ONLY
    )


def test_observation_input_permutation_does_not_change_map(tmp_path: Path) -> None:
    observations = (
        observation(ContentSignalType.SILENCE, 2.0, 6.0, "silence"),
        observation(ContentSignalType.NEAR_BLACK, 3.0, 4.0, "visual_structure"),
        observation(ContentSignalType.REPEATED_FRAMES, 8.0, 9.0, "visual_structure"),
    )

    first = build_content_map(
        make_bundle(tmp_path, observations=observations),
        input_hash=INPUT_HASH,
        effective_config=ContentConfig(minimum_candidate_duration_seconds=0.5),
    )
    second = build_content_map(
        make_bundle(tmp_path, observations=tuple(reversed(observations))),
        input_hash=INPUT_HASH,
        effective_config=ContentConfig(minimum_candidate_duration_seconds=0.5),
    )

    assert first == second
    assert first.map_digest == second.map_digest


def test_transcript_references_are_private_ids_not_copied_text(tmp_path: Path) -> None:
    transcript = parse_timed_transcript(
        "1\n00:00:01,000 --> 00:00:02,000\nPrivate spoken words\n",
        duration_seconds=10.0,
    )

    content_map = build_content_map(
        make_bundle(tmp_path, observations=()),
        input_hash=INPUT_HASH,
        effective_config=ContentConfig(),
        transcript=transcript,
    )
    serialized = content_map.model_dump_json()

    assert transcript.cues[0].id in {
        cue_id
        for segment in content_map.segments
        for cue_id in segment.transcript_cue_ids
    }
    assert "Private spoken words" not in serialized
    assert content_map.transcript_hash == transcript.transcript_hash


def test_locked_keep_overrides_corrobated_removal_eligibility(tmp_path: Path) -> None:
    locked_range = ContentTimeRange(start_seconds=3.25, end_seconds=3.75)
    locked = ContentUserRange(
        id=make_user_range_id(
            INPUT_HASH, ContentUserRangeKind.LOCKED_KEEP, locked_range
        ),
        kind=ContentUserRangeKind.LOCKED_KEEP,
        source_range=locked_range,
        label="Keep this explanation",
    )
    bundle = make_bundle(
        tmp_path,
        observations=(
            observation(ContentSignalType.SILENCE, 3.0, 4.0, "silence"),
            observation(ContentSignalType.NEAR_BLACK, 3.0, 4.0, "visual_structure"),
        ),
    )

    content_map = build_content_map(
        bundle,
        input_hash=INPUT_HASH,
        effective_config=ContentConfig(minimum_candidate_duration_seconds=0.1),
        user_ranges=(locked,),
    )
    protected = next(
        segment
        for segment in content_map.segments
        if segment.source_range.start_seconds == 3.25
    )

    assert protected.selection_eligibility is ContentSelectionEligibility.INELIGIBLE
    assert "locked keep" in protected.reason.lower()


def test_out_of_bounds_or_forged_user_range_is_rejected(tmp_path: Path) -> None:
    source_range = ContentTimeRange(start_seconds=9.0, end_seconds=11.0)
    forged = ContentUserRange(
        id="range_" + "0" * 64,
        kind=ContentUserRangeKind.KEEP,
        source_range=source_range,
    )

    with pytest.raises(ValueError):
        build_content_map(
            make_bundle(tmp_path, observations=()),
            input_hash=INPUT_HASH,
            effective_config=ContentConfig(),
            user_ranges=(forged,),
        )


def test_provider_failure_remains_visible_without_dropping_other_evidence(
    tmp_path: Path,
) -> None:
    content_map = build_content_map(
        make_bundle(
            tmp_path,
            observations=(
                observation(ContentSignalType.NEAR_BLACK, 2.0, 3.0, "visual_structure"),
            ),
            failed_provider=True,
        ),
        input_hash=INPUT_HASH,
        effective_config=ContentConfig(minimum_candidate_duration_seconds=0.5),
    )

    assert any(
        item.status is ContentProviderStatus.FAILED
        for item in content_map.provider_executions
    )
    assert content_map.warnings == ("silence feature provider failed (RuntimeError).",)
    assert any(
        signal.signal_type is ContentSignalType.NEAR_BLACK
        for segment in content_map.segments
        for signal in segment.signals
    )
