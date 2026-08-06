"""Neutral symptom assessments derived only from observed damage intervals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from videoscope.rescue.models import DamageKind, MediaDamageMap, RescueSymptom


class RescueSymptomStatus(StrEnum):
    """Whether the requested symptom has matching observable evidence."""

    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"


@dataclass(frozen=True, slots=True)
class RescueSymptomAssessment:
    """A path-free symptom result that avoids statements about root cause."""

    symptom: RescueSymptom
    status: RescueSymptomStatus
    interval_ids: tuple[str, ...]
    summary: str
    limitations: tuple[str, ...]


_SYMPTOM_KINDS: dict[RescueSymptom, frozenset[DamageKind]] = {
    RescueSymptom.UNPLAYABLE: frozenset({DamageKind.UNDECODABLE}),
    RescueSymptom.TIMELINE_DISCONTINUITY: frozenset(
        {DamageKind.TIMESTAMP_DISCONTINUITY}
    ),
    RescueSymptom.MISSING_AUDIO: frozenset({DamageKind.MISSING_STREAM}),
    RescueSymptom.AUDIO_VIDEO_OFFSET: frozenset({DamageKind.FIXED_AV_OFFSET}),
    RescueSymptom.DARK: frozenset({DamageKind.DARK}),
    RescueSymptom.VIDEO_NOISE: frozenset({DamageKind.VIDEO_NOISE}),
    RescueSymptom.SOFT_DETAIL: frozenset({DamageKind.SOFT_DETAIL}),
    RescueSymptom.FLICKER: frozenset({DamageKind.FLICKER}),
    RescueSymptom.SHAKE: frozenset({DamageKind.SHAKE}),
    RescueSymptom.LOW_LOUDNESS: frozenset({DamageKind.LOW_LOUDNESS}),
    RescueSymptom.AUDIO_NOISE: frozenset({DamageKind.AUDIO_NOISE}),
    RescueSymptom.AUDIO_CLIPPING: frozenset({DamageKind.AUDIO_CLIPPING}),
}


def classify_symptoms(
    damage_map: MediaDamageMap,
    requested: tuple[RescueSymptom, ...],
) -> tuple[RescueSymptomAssessment, ...]:
    """Return one deterministic assessment per requested symptom."""
    assessments: list[RescueSymptomAssessment] = []
    seen: set[RescueSymptom] = set()
    for symptom in requested:
        if symptom in seen:
            continue
        seen.add(symptom)
        matching = tuple(
            interval
            for interval in damage_map.intervals
            if interval.kind in _SYMPTOM_KINDS[symptom]
            and (
                symptom is not RescueSymptom.MISSING_AUDIO
                or interval.stream_id == "audio"
                or interval.stream_id.startswith("audio:")
            )
        )
        if matching:
            assessments.append(
                RescueSymptomAssessment(
                    symptom=symptom,
                    status=RescueSymptomStatus.OBSERVED,
                    interval_ids=tuple(interval.id for interval in matching),
                    summary=(
                        "Observable scan intervals match this requested symptom; "
                        "the scan does not determine why it occurred."
                    ),
                    limitations=(
                        "This is a heuristic mapping from observed intervals, not a "
                        "statement of root cause.",
                    ),
                )
            )
        else:
            assessments.append(
                RescueSymptomAssessment(
                    symptom=symptom,
                    status=RescueSymptomStatus.NOT_OBSERVED,
                    interval_ids=(),
                    summary=(
                        "No matching observable scan interval was recorded for this "
                        "requested symptom."
                    ),
                    limitations=(
                        "Absence of a matching interval does not prove the symptom is "
                        "absent.",
                    ),
                )
            )
    return tuple(assessments)
