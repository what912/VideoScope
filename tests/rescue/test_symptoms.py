"""Tests for deterministic, neutral Rescue symptom classification."""

from __future__ import annotations

from videoscope.rescue import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueSymptom,
    make_damage_id,
)
from videoscope.rescue.symptoms import RescueSymptomStatus, classify_symptoms


def interval(
    kind: DamageKind, start_seconds: float, end_seconds: float
) -> DamageInterval:
    return DamageInterval(
        id=make_damage_id("d" * 64, "video:0", kind, start_seconds, end_seconds),
        stream_id="video:0",
        kind=kind,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def test_classify_symptoms_preserves_request_order_and_uses_observable_evidence() -> (
    None
):
    damage_map = MediaDamageMap(
        input_hash="d" * 64,
        duration_seconds=6.0,
        intervals=(
            interval(DamageKind.UNDECODABLE, 2.0, 3.0),
            interval(DamageKind.FLICKER, 4.0, 5.0),
        ),
    )

    assessments = classify_symptoms(
        damage_map,
        (RescueSymptom.FLICKER, RescueSymptom.UNPLAYABLE, RescueSymptom.SHAKE),
    )

    assert [assessment.symptom for assessment in assessments] == [
        RescueSymptom.FLICKER,
        RescueSymptom.UNPLAYABLE,
        RescueSymptom.SHAKE,
    ]
    assert [assessment.status for assessment in assessments] == [
        RescueSymptomStatus.OBSERVED,
        RescueSymptomStatus.OBSERVED,
        RescueSymptomStatus.NOT_OBSERVED,
    ]
    assert assessments[0].interval_ids == (damage_map.intervals[1].id,)
    assert "observable" in assessments[1].summary.casefold()
    assert "cause" not in assessments[1].summary.casefold()


def test_classify_symptoms_does_not_duplicate_a_repeated_request() -> None:
    damage_map = MediaDamageMap(input_hash="d" * 64, duration_seconds=1.0)

    assessments = classify_symptoms(
        damage_map,
        (RescueSymptom.DARK, RescueSymptom.DARK),
    )

    assert len(assessments) == 1
    assert assessments[0].symptom is RescueSymptom.DARK


def test_missing_audio_requires_an_audio_stream_interval() -> None:
    video_missing = interval(DamageKind.MISSING_STREAM, 0.0, 1.0).model_copy(
        update={"stream_id": "video:0"}
    )
    video_missing = video_missing.model_copy(
        update={
            "id": make_damage_id(
                "d" * 64, "video:0", DamageKind.MISSING_STREAM, 0.0, 1.0
            )
        }
    )
    audio_missing = DamageInterval(
        id=make_damage_id("d" * 64, "audio", DamageKind.MISSING_STREAM, 1.0, 2.0),
        stream_id="audio",
        kind=DamageKind.MISSING_STREAM,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    only_video = MediaDamageMap(
        input_hash="d" * 64, duration_seconds=2.0, intervals=(video_missing,)
    )
    assert (
        classify_symptoms(only_video, (RescueSymptom.MISSING_AUDIO,))[0].status
        is RescueSymptomStatus.NOT_OBSERVED
    )
    with_audio = MediaDamageMap(
        input_hash="d" * 64, duration_seconds=2.0, intervals=(audio_missing,)
    )
    assert (
        classify_symptoms(with_audio, (RescueSymptom.MISSING_AUDIO,))[0].status
        is RescueSymptomStatus.OBSERVED
    )
