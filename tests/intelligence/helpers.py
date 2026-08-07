from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from videoscope.content import (
    ContentConfig,
    ContentMap,
    ContentSegment,
    ContentSelectionEligibility,
    ContentTimeRange,
    make_content_map_digest,
    make_segment_id,
)

INPUT_HASH = "a" * 64


def content_map(*, duration: float = 10.0) -> ContentMap:
    source_range = ContentTimeRange(start_seconds=0, end_seconds=duration)
    segment = ContentSegment(
        id=make_segment_id(INPUT_HASH, source_range, ()),
        source_range=source_range,
        source_order_index=0,
        selection_eligibility=ContentSelectionEligibility.MANUAL_ONLY,
        reason="Visible source range for human review.",
    )
    payload = {
        "schema_version": "0.1",
        "input_hash": INPUT_HASH,
        "transcript_hash": None,
        "duration_seconds": duration,
        "effective_config": ContentConfig().model_dump(mode="json"),
        "provider_executions": [],
        "segments": [segment.model_dump(mode="json")],
        "user_ranges": [],
        "warnings": [],
    }
    return ContentMap(
        input_hash=INPUT_HASH,
        duration_seconds=duration,
        effective_config=ContentConfig(),
        segments=(segment,),
        map_digest=make_content_map_digest(cast(Mapping[str, JsonValue], payload)),
    )
