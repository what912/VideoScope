"""Deterministic ContentMap construction from structural evidence."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import cast

from pydantic import JsonValue

from videoscope.content.features import ContentFeatureBundle, ContentObservation
from videoscope.content.models import (
    ContentConfig,
    ContentMap,
    ContentSegment,
    ContentSelectionEligibility,
    ContentSignal,
    ContentSignalType,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    make_content_map_digest,
    make_segment_id,
    make_user_range_id,
)
from videoscope.content.transcript import NormalizedTranscript, TranscriptCue

_BOUNDARY_TOLERANCE = 1e-9
_LOW_INFORMATION_SIGNALS = frozenset(
    {
        ContentSignalType.SILENCE,
        ContentSignalType.LOW_VISUAL_CHANGE,
        ContentSignalType.NEAR_BLACK,
        ContentSignalType.REPEATED_FRAMES,
    }
)


def build_content_map(
    bundle: ContentFeatureBundle,
    *,
    input_hash: str,
    effective_config: ContentConfig,
    transcript: NormalizedTranscript | None = None,
    user_ranges: tuple[ContentUserRange, ...] = (),
) -> ContentMap:
    """Combine read-only evidence into continuous, deterministic source segments."""
    duration = bundle.metadata.duration_seconds
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("content map requires a finite positive media duration")
    _validate_scenes(bundle, duration)
    _validate_observations(bundle.observations, duration)
    _validate_user_ranges(input_hash, user_ranges, duration)
    if transcript is not None:
        _validate_transcript(transcript, duration)
    boundaries = _collect_boundaries(
        duration=duration,
        bundle=bundle,
        transcript=transcript,
        user_ranges=user_ranges,
    )
    segments: list[ContentSegment] = []
    for source_order_index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        source_range = ContentTimeRange(start_seconds=start, end_seconds=end)
        overlapping_observations = tuple(
            item
            for item in bundle.observations
            if _overlaps(item.source_range, source_range)
        )
        overlapping_cues = (
            tuple(cue for cue in transcript.cues if _cue_overlaps(cue, source_range))
            if transcript is not None
            else ()
        )
        overlapping_user_ranges = tuple(
            item for item in user_ranges if _overlaps(item.source_range, source_range)
        )
        scene_index = _scene_index(bundle, source_range)
        signals = _segment_signals(
            overlapping_observations,
            overlapping_cues=overlapping_cues,
            overlapping_user_ranges=overlapping_user_ranges,
            scene_index=scene_index,
        )
        eligibility, reason, limitations = _selection_status(
            source_range,
            signals=signals,
            user_ranges=overlapping_user_ranges,
            config=effective_config,
        )
        signal_types = tuple(item.signal_type for item in signals)
        segments.append(
            ContentSegment(
                id=make_segment_id(input_hash, source_range, signal_types),
                source_range=source_range,
                source_order_index=source_order_index,
                signals=signals,
                transcript_cue_ids=tuple(cue.id for cue in overlapping_cues),
                selection_eligibility=eligibility,
                reason=reason,
                limitations=limitations,
                private_evidence_paths=(),
                user_range_ids=tuple(item.id for item in overlapping_user_ranges),
            )
        )
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "transcript_hash": transcript.transcript_hash if transcript else None,
        "duration_seconds": duration,
        "effective_config": effective_config.model_dump(mode="json"),
        "provider_executions": [
            item.model_dump(mode="json")
            for item in sorted(bundle.executions, key=lambda value: value.provider_id)
        ],
        "segments": [item.model_dump(mode="json") for item in segments],
        "user_ranges": [
            item.model_dump(mode="json")
            for item in sorted(
                user_ranges,
                key=lambda value: (
                    value.source_range.start_seconds,
                    value.source_range.end_seconds,
                    value.kind.value,
                    value.id,
                ),
            )
        ],
        "warnings": cast(JsonValue, sorted(set(bundle.warnings))),
    }
    payload["map_digest"] = make_content_map_digest(payload)
    return ContentMap.model_validate(payload)


def _collect_boundaries(
    *,
    duration: float,
    bundle: ContentFeatureBundle,
    transcript: NormalizedTranscript | None,
    user_ranges: tuple[ContentUserRange, ...],
) -> tuple[float, ...]:
    values: list[float] = [0.0, duration]
    for scene in bundle.scenes:
        values.extend((scene.start_seconds, scene.end_seconds))
    for observation in bundle.observations:
        values.extend(
            (
                observation.source_range.start_seconds,
                observation.source_range.end_seconds,
            )
        )
    if transcript is not None:
        for cue in transcript.cues:
            values.extend((cue.start_seconds, cue.end_seconds))
    for item in user_ranges:
        values.extend((item.source_range.start_seconds, item.source_range.end_seconds))
    ordered = sorted(values)
    normalized: list[float] = []
    for value in ordered:
        if normalized and math.isclose(
            value, normalized[-1], rel_tol=0, abs_tol=_BOUNDARY_TOLERANCE
        ):
            continue
        normalized.append(value)
    if len(normalized) < 2 or normalized[0] != 0.0 or normalized[-1] != duration:
        raise ValueError("content map boundaries do not cover the source")
    if any(
        right - left <= _BOUNDARY_TOLERANCE
        for left, right in zip(normalized, normalized[1:])
    ):
        raise ValueError("content map contains a non-positive segment")
    return tuple(normalized)


def _segment_signals(
    observations: tuple[ContentObservation, ...],
    *,
    overlapping_cues: tuple[TranscriptCue, ...],
    overlapping_user_ranges: tuple[ContentUserRange, ...],
    scene_index: int,
) -> tuple[ContentSignal, ...]:
    by_type: defaultdict[ContentSignalType, list[ContentObservation]] = defaultdict(
        list
    )
    for observation in observations:
        by_type[observation.signal_type].append(observation)
    signals: list[ContentSignal] = [
        ContentSignal(
            signal_type=ContentSignalType.SCENE,
            provider_id="scenes",
            provider_version="1",
            measurements={"scene_index": scene_index},
        )
    ]
    for signal_type, values in sorted(by_type.items(), key=lambda item: item[0].value):
        ordered = sorted(values, key=lambda value: value.id)
        signals.append(
            ContentSignal(
                signal_type=signal_type,
                provider_id=ordered[0].provider_id,
                provider_version=ordered[0].provider_version,
                measurements={
                    "observation_count": len(ordered),
                    "observation_ids": [item.id for item in ordered],
                    "providers": cast(
                        JsonValue,
                        sorted({item.provider_id for item in ordered}),
                    ),
                },
                parameters={
                    "observations": [
                        {
                            "id": item.id,
                            "parameters": dict(item.parameters),
                            "provider_id": item.provider_id,
                            "provider_version": item.provider_version,
                        }
                        for item in ordered
                    ]
                },
                limitations=tuple(
                    sorted({text for item in ordered for text in item.limitations})
                ),
            )
        )
    if overlapping_cues:
        signals.append(
            ContentSignal(
                signal_type=ContentSignalType.TRANSCRIPT,
                provider_id="transcript",
                provider_version="1",
                measurements={
                    "cue_count": len(overlapping_cues),
                    "cue_ids": [item.id for item in overlapping_cues],
                },
                limitations=(
                    "Timed text is user-supplied private evidence, not an "
                    "invented quote.",
                ),
            )
        )
    if overlapping_user_ranges:
        signals.append(
            ContentSignal(
                signal_type=ContentSignalType.USER_RANGE,
                provider_id="user_range",
                provider_version="1",
                measurements={
                    "range_ids": [item.id for item in overlapping_user_ranges],
                    "range_kinds": [
                        item.kind.value for item in overlapping_user_ranges
                    ],
                },
            )
        )
    return tuple(sorted(signals, key=lambda value: value.signal_type.value))


def _selection_status(
    source_range: ContentTimeRange,
    *,
    signals: tuple[ContentSignal, ...],
    user_ranges: tuple[ContentUserRange, ...],
    config: ContentConfig,
) -> tuple[ContentSelectionEligibility, str, tuple[str, ...]]:
    kinds = {item.kind for item in user_ranges}
    if ContentUserRangeKind.LOCKED_KEEP in kinds:
        return (
            ContentSelectionEligibility.INELIGIBLE,
            "A locked keep range protects this source interval.",
            ("Locked keep ranges always override automatic removal proposals.",),
        )
    if ContentUserRangeKind.LOCKED_EXCLUDE in kinds:
        return (
            ContentSelectionEligibility.ELIGIBLE,
            "A locked exclude range explicitly marks this interval for removal.",
            ("The exact locked exclude still requires a matching confirmed plan.",),
        )
    if ContentUserRangeKind.EXCLUDE in kinds:
        return (
            ContentSelectionEligibility.ELIGIBLE,
            "The user explicitly selected this interval for removal review.",
            ("The exact user selection still requires preview and confirmation.",),
        )
    if ContentUserRangeKind.KEEP in kinds:
        return (
            ContentSelectionEligibility.INELIGIBLE,
            "The user explicitly selected this interval to keep.",
            (),
        )
    low_information = {
        signal.signal_type
        for signal in signals
        if signal.signal_type in _LOW_INFORMATION_SIGNALS
    }
    if (
        len(low_information) >= config.minimum_corrobating_signals
        and source_range.duration_seconds >= config.minimum_candidate_duration_seconds
    ):
        return (
            ContentSelectionEligibility.ELIGIBLE,
            "Multiple configured observable low-information signals overlap "
            "this interval.",
            (
                "The signals are heuristic observations; removal requires an "
                "exact preview and confirmation.",
            ),
        )
    limitations = (
        (
            "Silence alone can be meaningful and is never sufficient for "
            "automatic removal."
        )
        if low_information == {ContentSignalType.SILENCE}
        else "Available evidence is insufficient for an automatic removal proposal."
    )
    return (
        ContentSelectionEligibility.MANUAL_ONLY,
        "The interval remains available for explicit manual review.",
        (limitations,),
    )


def _validate_scenes(bundle: ContentFeatureBundle, duration: float) -> None:
    if not bundle.scenes:
        raise ValueError("content map requires at least one scene")
    if not math.isclose(bundle.scenes[0].start_seconds, 0.0, abs_tol=1e-9):
        raise ValueError("first scene must start at zero")
    if not math.isclose(bundle.scenes[-1].end_seconds, duration, abs_tol=1e-6):
        raise ValueError("last scene must end at source duration")


def _validate_observations(
    observations: tuple[ContentObservation, ...], duration: float
) -> None:
    identifiers: set[str] = set()
    for item in observations:
        if item.id in identifiers:
            raise ValueError("duplicate content observation ID")
        identifiers.add(item.id)
        if item.source_range.end_seconds > duration:
            raise ValueError("content observation exceeds source duration")


def _validate_user_ranges(
    input_hash: str,
    ranges: tuple[ContentUserRange, ...],
    duration: float,
) -> None:
    identifiers: set[str] = set()
    for item in ranges:
        if item.id in identifiers:
            raise ValueError("duplicate user range ID")
        identifiers.add(item.id)
        expected = make_user_range_id(input_hash, item.kind, item.source_range)
        if item.id != expected:
            raise ValueError("user range ID does not match its source inputs")
        if item.source_range.end_seconds > duration:
            raise ValueError("user range exceeds source duration")


def _validate_transcript(transcript: NormalizedTranscript, duration: float) -> None:
    if any(cue.end_seconds > duration for cue in transcript.cues):
        raise ValueError("transcript cue exceeds source duration")


def _scene_index(bundle: ContentFeatureBundle, source_range: ContentTimeRange) -> int:
    midpoint = (source_range.start_seconds + source_range.end_seconds) / 2.0
    for scene in bundle.scenes:
        if scene.start_seconds <= midpoint < scene.end_seconds:
            return scene.scene_index
    return bundle.scenes[-1].scene_index


def _overlaps(left: ContentTimeRange, right: ContentTimeRange) -> bool:
    return (
        left.start_seconds < right.end_seconds
        and right.start_seconds < left.end_seconds
    )


def _cue_overlaps(cue: TranscriptCue, source_range: ContentTimeRange) -> bool:
    return (
        cue.start_seconds < source_range.end_seconds
        and source_range.start_seconds < cue.end_seconds
    )


__all__ = ["build_content_map"]
