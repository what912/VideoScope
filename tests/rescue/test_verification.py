"""Independent, deterministic Rescue verification gates."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue import verification as verification_module
from videoscope.rescue.assessment import (
    LocalRescueAssessmentService,
    SyncEventMeasurements,
)
from videoscope.rescue.capabilities import require_executable_action_scopes
from videoscope.rescue.errors import RescueCancelledError, RescueMediaError
from videoscope.rescue.executor import CommandResult, SourceMapping
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueConfirmation,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    make_damage_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.rescue.verification import (
    MediaVerificationSnapshot,
    NativeMediaMeasurementProvider,
    ReferenceRenderOptions,
    RescueVerifier,
)

REAL_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "generated"
REAL_MANIFEST_PATH = Path(__file__).parents[1] / "fixtures" / "manifest.json"


def _real_fixture(filename: str) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip(
            "FFmpeg and ffprobe on PATH are required for real Rescue verification"
        )
    source = REAL_FIXTURE_ROOT / filename
    if not source.is_file():
        pytest.skip(
            "run `python scripts/generate_test_videos.py --force` before real "
            "Rescue verification"
        )
    return source


def _real_confirmation(preparation: Any) -> RescueConfirmation:
    accepted = tuple(
        action.id for action in preparation.plan.actions if action.requires_confirmation
    )
    improvement_kinds = {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
    }
    return RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=any(
            action.id in accepted and action.kind in improvement_kinds
            for action in preparation.plan.actions
        ),
        accepted_action_ids=accepted,
    )


def test_native_measurement_propagates_cancellation(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"candidate")

    def cancelled_probe(_path: Path) -> VideoMetadata:
        raise RescueCancelledError("cancelled")

    provider = NativeMediaMeasurementProvider(probe=cancelled_probe)
    with pytest.raises(RescueCancelledError):
        provider.measure(candidate, candidate.name, lambda: True)


def _native_provider_with_ffprobe_json(
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[NativeMediaMeasurementProvider, list[tuple[str, ...]]]:
    monkeypatch.setattr(
        verification_module,
        "_measure_visual_stream",
        lambda _path, _cancel: (0, 0, 0, 0.0, 0.0, 0.0),
    )
    calls: list[tuple[str, ...]] = []

    def probe(path: Path) -> VideoMetadata:
        return VideoMetadata(
            filename=path.name,
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=1.0,
            average_frame_rate=1.0,
            estimated_frame_count=1,
            has_audio=True,
            file_size_bytes=path.stat().st_size,
        )

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        if arguments[0] == "test-ffprobe" and arguments[1:] == ("-version",):
            return CommandResult(
                returncode=0,
                stderr_summary="",
                stdout_summary="ffprobe test-version\nconfiguration omitted",
            )
        if arguments[0] == "test-ffprobe":
            return CommandResult(
                returncode=0,
                stderr_summary="",
                stdout_summary=json.dumps(payload),
            )
        return CommandResult(returncode=0, stderr_summary="")

    return (
        NativeMediaMeasurementProvider(
            ffprobe="test-ffprobe", probe=probe, command_runner=runner
        ),
        calls,
    )


def _packet_payload(
    *,
    video_packet: dict[str, object] | None,
    audio_packet: dict[str, object] | None,
) -> dict[str, object]:
    packets: list[dict[str, object]] = []
    if audio_packet is not None:
        packets.append({"stream_index": 7, **audio_packet})
    if video_packet is not None:
        packets.append({"stream_index": 3, **video_packet})
    return {
        "streams": [
            {"index": 3, "codec_type": "video"},
            {"index": 7, "codec_type": "audio", "sample_rate": "48000"},
        ],
        "packets": packets,
    }


def test_packet_timestamps_determine_audio_video_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches using stream start metadata instead of first packet timestamps."""
    provider, calls = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"pts_time": "0.04"},
            audio_packet={"pts_time": "0.31"},
        ),
        monkeypatch,
    )
    media = tmp_path / "媒体 packet source.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, "faithful-rescue.mp4", lambda: False)
    provider.measure(media, "faithful-rescue.mp4", lambda: False)

    assert snapshot.av_offset_seconds == pytest.approx(0.27)
    assert snapshot.av_offset_method == "first_usable_packet_timestamp"
    assert snapshot.av_offset_tool_version == "ffprobe test-version"
    assert sum(call[1:] == ("-version",) for call in calls) == 1


def test_packet_timestamp_applies_aac_skip_samples_to_the_usable_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"pts_time": "0.000000"},
            audio_packet={
                "pts_time": "-0.042667",
                "side_data_list": [
                    {
                        "side_data_type": "Skip Samples",
                        "skip_samples": 2048,
                    }
                ],
            },
        ),
        monkeypatch,
    )
    media = tmp_path / "aac priming.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)

    assert snapshot.av_offset_seconds == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("sample_rate", [None, "invalid", "0"])
def test_packet_timestamp_fails_closed_when_skip_sample_rate_is_unusable(
    sample_rate: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _packet_payload(
        video_packet={"pts_time": "0.0"},
        audio_packet={
            "pts_time": "-0.042667",
            "side_data_list": [
                {"side_data_type": "Skip Samples", "skip_samples": 2048}
            ],
        },
    )
    audio_stream = cast(dict[str, object], cast(list[object], payload["streams"])[1])
    if sample_rate is None:
        audio_stream.pop("sample_rate")
    else:
        audio_stream["sample_rate"] = sample_rate
    provider, _ = _native_provider_with_ffprobe_json(payload, monkeypatch)
    media = tmp_path / "malformed rate.mp4"
    media.write_bytes(b"candidate")

    assert provider.measure(media, media.name, lambda: False).av_offset_seconds is None


def test_packet_timestamp_falls_back_to_dts_only_when_pts_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches treating malformed PTS as permission to substitute DTS."""
    valid, _ = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"dts_time": "0.04"},
            audio_packet={"dts_time": "0.31"},
        ),
        monkeypatch,
    )
    malformed, _ = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"pts_time": "invalid", "dts_time": "0.04"},
            audio_packet={"pts_time": "0.31"},
        ),
        monkeypatch,
    )
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    assert valid.measure(media, media.name, lambda: False).av_offset_seconds == (
        pytest.approx(0.27)
    )
    assert malformed.measure(media, media.name, lambda: False).av_offset_seconds is None


def test_packet_timestamp_uses_first_finite_non_negative_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches accepting negative or non-finite packet clock evidence."""
    provider, _ = _native_provider_with_ffprobe_json(
        {
            "streams": [
                {"index": 3, "codec_type": "video"},
                {"index": 7, "codec_type": "audio"},
            ],
            "packets": [
                {"stream_index": 3, "pts_time": "nan"},
                {"stream_index": 3, "pts_time": "-0.2"},
                {"stream_index": 7, "pts_time": "inf"},
                {"stream_index": 7, "pts_time": "0.31"},
                {"stream_index": 3, "pts_time": "0.04"},
            ],
        },
        monkeypatch,
    )
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)

    assert snapshot.av_offset_seconds == pytest.approx(0.27)


@pytest.mark.parametrize(
    ("packet_stream_index", "declared_video_index", "expected_status"),
    [
        (3.0, 3, RescueVerificationStatus.NEEDS_REVIEW),
        (True, 1, RescueVerificationStatus.NEEDS_REVIEW),
        (-1, 3, RescueVerificationStatus.NEEDS_REVIEW),
        (3, 3, RescueVerificationStatus.PASSED),
    ],
)
def test_packet_stream_index_requires_non_negative_non_boolean_integer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packet_stream_index: object,
    declared_video_index: int,
    expected_status: RescueVerificationStatus,
) -> None:
    """Catches Python equality accepting malformed packet stream indexes."""
    provider, _ = _native_provider_with_ffprobe_json(
        {
            "streams": [
                {"index": declared_video_index, "codec_type": "video"},
                {"index": 7, "codec_type": "audio"},
            ],
            "packets": [
                {"stream_index": 7, "pts_time": "0.31"},
                {"stream_index": packet_stream_index, "pts_time": "0.30"},
            ],
        },
        monkeypatch,
    )
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)
    report = _verify(
        tmp_path / "verification",
        faithful_updates={
            "av_offset_seconds": snapshot.av_offset_seconds,
            "av_offset_method": snapshot.av_offset_method,
            "av_offset_tool_version": snapshot.av_offset_tool_version,
        },
        actions=(_fixed_offset_action(),),
    )

    if expected_status is RescueVerificationStatus.PASSED:
        assert snapshot.av_offset_seconds == pytest.approx(0.01)
    else:
        assert snapshot.av_offset_seconds is None
    assert _check(report, "faithful", "fixed_av_offset").status is expected_status


@pytest.mark.parametrize(
    "payload",
    [
        _packet_payload(video_packet={"pts_time": "0.04"}, audio_packet=None),
        {
            "streams": [
                {"index": 3, "codec_type": "video"},
                {"index": 3, "codec_type": "audio"},
            ],
            "packets": [{"stream_index": 3, "pts_time": "0.04"}],
        },
        {"streams": "malformed", "packets": []},
    ],
)
def test_missing_or_ambiguous_packet_evidence_is_not_inferred(
    tmp_path: Path, payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches manufacturing an A/V pass from incomplete or ambiguous evidence."""
    provider, _ = _native_provider_with_ffprobe_json(payload, monkeypatch)
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)

    assert snapshot.av_offset_seconds is None
    assert snapshot.av_offset_method is None
    assert snapshot.av_offset_tool_version is None


class _FakeMeasurementProvider:
    """Explicit test-only measurements; production never selects this provider."""

    def __init__(
        self,
        snapshots: dict[Path, MediaVerificationSnapshot],
        *,
        mapped_reference_updates: dict[str, object] | Exception | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[Path, str]] = []
        self.mapped_reference_updates = mapped_reference_updates
        self.mapped_reference_calls: list[
            tuple[Path, tuple[SourceMapping, ...], ReferenceRenderOptions]
        ] = []

    def measure(
        self,
        path: Path,
        relative_path: str,
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> MediaVerificationSnapshot:
        self.calls.append((path, relative_path))
        return replace(self.snapshots[path], path=path)

    def measure_mapped_reference(
        self,
        path: Path,
        mappings: tuple[SourceMapping, ...],
        render_mode: str,
        reference_options: ReferenceRenderOptions,
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> MediaVerificationSnapshot:
        del render_mode, cancellation_callback
        self.mapped_reference_calls.append((path, mappings, reference_options))
        if isinstance(self.mapped_reference_updates, Exception):
            raise self.mapped_reference_updates
        return cast(
            MediaVerificationSnapshot,
            cast(Any, replace)(
                self.snapshots[path],
                relative_path="mapped-source-reference",
                duration_seconds=sum(
                    mapping.source_end - mapping.source_start for mapping in mappings
                ),
                **(self.mapped_reference_updates or {}),
            ),
        )


def _action(kind: RescueActionKind, parameters: dict[str, object]) -> RescueAction:
    return RescueAction(
        id=f"action_{kind.value}",
        version="1",
        kind=kind,
        description="Measured local operation.",
        source_ranges=((0.0, 4.0),),
        parameters=cast(dict[str, JsonValue], parameters),
        changes_content=kind is not RescueActionKind.REMUX,
        requires_confirmation=kind is not RescueActionKind.REMUX,
        strategy=RescueStrategy.BALANCED,
    )


def _plan(input_hash: str, *extra_actions: RescueAction) -> RescuePlan:
    actions = (_action(RescueActionKind.REMUX, {}), *extra_actions)
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "strategy": RescueStrategy.BALANCED,
        "effective_config": RescueEffectiveConfig().model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": ["faithful-rescue.mp4"],
        "damage_intervals": [],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _two_segment_plan(input_hash: str, *mapped_actions: RescueAction) -> RescuePlan:
    """Build a valid partial-salvage plan for mapping-boundary tests."""
    damaged = DamageInterval(
        id=make_damage_id(input_hash, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    actions = (
        _action(RescueActionKind.REMUX, {}),
        RescueAction(
            id="action_salvage_segments",
            version="1",
            kind=RescueActionKind.SALVAGE_SEGMENTS,
            description="Preserve the decodable source ranges.",
            source_ranges=((1.0, 2.0),),
            parameters={"damage_ids": [damaged.id]},
            changes_content=True,
            requires_confirmation=True,
            strategy=RescueStrategy.BALANCED,
        ),
        *mapped_actions,
    )
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "strategy": RescueStrategy.BALANCED,
        "effective_config": RescueEffectiveConfig().model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": ["faithful-rescue.mp4"],
        "damage_intervals": [damaged.model_dump(mode="json")],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _two_segment_fixed_offset_plan(input_hash: str) -> RescuePlan:
    return _two_segment_plan(input_hash, _fixed_offset_action())


def _two_segment_luma_plan(input_hash: str) -> RescuePlan:
    return _two_segment_plan(
        input_hash,
        _action(
            RescueActionKind.ADJUST_LUMA,
            {"brightness": 0.04, "contrast": 1.02},
        ),
    )


def _snapshot(path: Path, **updates: object) -> MediaVerificationSnapshot:
    values: dict[str, object] = {
        "path": path,
        "relative_path": path.name,
        "duration_seconds": 4.0,
        "video_stream_count": 1,
        "audio_stream_count": 1,
        "complete_decode": True,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }
    values.update(updates)
    return MediaVerificationSnapshot(**values)  # type: ignore[arg-type]


def _verify(
    tmp_path: Path,
    *,
    faithful_updates: dict[str, object] | None = None,
    improved_updates: dict[str, object] | None = None,
    actions: tuple[RescueAction, ...] = (),
    mappings: tuple[SourceMapping, ...] | None = None,
    mapped_reference_updates: dict[str, object] | Exception | None = None,
    faithful_render_mode: Literal[
        "stream_copy", "single_reencode", "segment_concat_reencode"
    ] = "stream_copy",
    plan: RescuePlan | None = None,
) -> RescueVerificationReport:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"faithful")
    improved_path = None
    snapshots = {
        source: _snapshot(source),
        faithful: _snapshot(faithful, **(faithful_updates or {})),
    }
    if improved_updates is not None:
        improved_path = tmp_path / "improved-viewing.mp4"
        improved_path.write_bytes(b"improved")
        snapshots[improved_path] = _snapshot(improved_path, **improved_updates)
    return RescueVerifier(
        measurement_provider=_FakeMeasurementProvider(
            snapshots, mapped_reference_updates=mapped_reference_updates
        )
    ).verify(
        source=source,
        faithful=faithful,
        improved=improved_path,
        plan=plan or _plan(sha256(source.read_bytes()).hexdigest(), *actions),
        mappings=mappings
        or (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        faithful_render_mode=faithful_render_mode,
    )


def _check(
    report: RescueVerificationReport,
    artifact: Literal["faithful", "improved"],
    check_id: str,
) -> RescueVerificationCheck:
    return next(
        check
        for check in report.checks
        if check.artifact == artifact and check.check_id == check_id
    )


def _json_number(value: JsonValue) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def test_check_ids_values_and_order_are_stable(tmp_path: Path) -> None:
    report = _verify(tmp_path, improved_updates={})
    expected_supplementary = (
        "artifact_integrity",
        "audio_loudness",
        "audio_peak",
        "black_regression",
        "fixed_av_offset",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
        "source_mapping",
        "stabilization_crop",
    )
    assert report.required_check_ids == RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    for artifact in ("faithful", "improved"):
        artifact_checks = tuple(c for c in report.checks if c.artifact == artifact)
        assert tuple(c.check_id for c in artifact_checks[:4]) == (
            "decodable",
            "duration",
            "streams",
            "source_read_only",
        )
        assert tuple(c.check_id for c in artifact_checks[4:]) == expected_supplementary
        assert all(c.measured for c in artifact_checks)


def test_failed_improved_copy_does_not_invalidate_faithful_copy(tmp_path: Path) -> None:
    report = _verify(tmp_path, improved_updates={"complete_decode": False})
    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert report.improved_status is RescueVerificationStatus.FAILED
    assert report.outcome is RescueOutcome.PARTIAL


def test_needs_review_improved_does_not_change_faithful_status(tmp_path: Path) -> None:
    report = _verify(tmp_path, improved_updates={"clipping_ratio": 0.01})
    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert report.improved_status is RescueVerificationStatus.NEEDS_REVIEW
    assert report.outcome is RescueOutcome.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("updates", "check_id"),
    [
        ({"complete_decode": False}, "decodable"),
        ({"video_stream_count": 0}, "streams"),
        ({"video_stream_count": 2}, "streams"),
        ({"audio_stream_count": 0}, "streams"),
        ({"audio_stream_count": 2}, "streams"),
        ({"duration_seconds": 4.251}, "duration"),
    ],
)
def test_required_output_failures_have_failed_precedence(
    tmp_path: Path, updates: dict[str, object], check_id: str
) -> None:
    report = _verify(tmp_path, faithful_updates=updates)
    assert (
        _check(report, "faithful", check_id).status is RescueVerificationStatus.FAILED
    )
    assert report.faithful_status is RescueVerificationStatus.FAILED
    assert report.outcome is RescueOutcome.FAILED


def test_duration_boundary_is_inclusive_at_configured_tolerance(tmp_path: Path) -> None:
    report = _verify(tmp_path, faithful_updates={"duration_seconds": 4.25})
    assert (
        _check(report, "faithful", "duration").status is RescueVerificationStatus.PASSED
    )


def test_source_hash_is_recomputed_and_not_trusted_from_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_snapshot = _snapshot(source)
    source.write_bytes(b"changed")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"faithful")
    provider = _FakeMeasurementProvider(
        {source: source_snapshot, faithful: _snapshot(faithful)}
    )
    report = RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=None,
        plan=_plan(source_snapshot.sha256),
        mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
    )
    assert (
        _check(report, "faithful", "source_read_only").status
        is RescueVerificationStatus.FAILED
    )


@pytest.mark.parametrize(
    ("updates", "check_id"),
    [
        ({"black_events": 1}, "black_regression"),
        ({"freeze_events": 1}, "freeze_regression"),
        ({"flicker_events": 1}, "flicker_regression"),
        ({"clipping_ratio": 0.01}, "luma_clipping"),
        ({"noise_residual": 0.1}, "noise_side_effects"),
        ({"sharpness": 0.5}, "sharpness_side_effects"),
    ],
)
def test_visual_regressions_require_review(
    tmp_path: Path, updates: dict[str, object], check_id: str
) -> None:
    source_updates = {"sharpness": 1.0} if check_id == "sharpness_side_effects" else {}
    report = _verify(
        tmp_path,
        faithful_updates={**source_updates, **updates},
    )
    # For sharpness, compare a source baseline of 1.0 against candidate 0.5.
    if check_id == "sharpness_side_effects":
        source = tmp_path / "source2.mp4"
        source.write_bytes(b"source")
        faithful = tmp_path / "faithful2.mp4"
        faithful.write_bytes(b"faithful")
        provider = _FakeMeasurementProvider(
            {
                source: _snapshot(source, sharpness=1.0),
                faithful: _snapshot(
                    faithful, relative_path="faithful-rescue.mp4", sharpness=0.5
                ),
            }
        )
        report = RescueVerifier(measurement_provider=provider).verify(
            source=source,
            faithful=faithful,
            improved=None,
            plan=_plan(sha256(source.read_bytes()).hexdigest()),
            mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        )
    assert (
        _check(report, "faithful", check_id).status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert _check(report, "faithful", check_id).measured["applicable"] is True


def test_visual_side_effect_comparison_is_not_applicable_across_removed_ranges(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={
            "duration_seconds": 3.0,
            "black_events": 1,
            "freeze_events": 1,
            "flicker_events": 1,
            "clipping_ratio": 0.5,
            "noise_residual": 0.5,
            "sharpness": 0.0,
        },
        mappings=(
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        mapped_reference_updates={"sharpness": 1.0},
        plan=_two_segment_luma_plan(sha256(b"source").hexdigest()),
    )

    for check_id in (
        "black_regression",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
    ):
        check = _check(report, "faithful", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.measured["applicable"] is True
        assert check.measured["reference"] == "retained_source_ranges"
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW
    assert report.outcome is RescueOutcome.NEEDS_REVIEW


def test_retained_range_visual_reference_allows_normal_partial_faithful(
    tmp_path: Path,
) -> None:
    mappings = (
        SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
    )
    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": 3.0},
        mappings=mappings,
        mapped_reference_updates={},
        plan=_two_segment_luma_plan(sha256(b"source").hexdigest()),
    )

    for check_id in (
        "black_regression",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
    ):
        check = _check(report, "faithful", check_id)
        assert check.status is RescueVerificationStatus.PASSED
        assert check.measured["applicable"] is True
        assert check.measured["reference"] == "retained_source_ranges"
    assert report.faithful_status is RescueVerificationStatus.PASSED


def test_unavailable_retained_range_reference_requires_review(tmp_path: Path) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": 3.0},
        mappings=(
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        mapped_reference_updates=RuntimeError("measurement unavailable"),
        plan=_two_segment_luma_plan(sha256(b"source").hexdigest()),
    )

    for check_id in (
        "black_regression",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
    ):
        check = _check(report, "faithful", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.measured["applicable"] is False
        assert check.measured["reason"] == "retained_source_reference_unavailable"


def test_full_source_reencode_uses_codec_aligned_visual_reference(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={"black_events": 1},
        mapped_reference_updates={"black_events": 0},
        faithful_render_mode="single_reencode",
    )

    check = _check(report, "faithful", "black_regression")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert check.measured["applicable"] is True
    assert check.measured["reference"] == "codec_aligned_source"


def _fixed_offset_action() -> RescueAction:
    return _action(
        RescueActionKind.CORRECT_FIXED_AV_OFFSET,
        {"offset_seconds": 0.4, "audio_shift_seconds": -0.4},
    )


@pytest.mark.parametrize("output_end", [0.4, 40.0])
def test_fixed_offset_rejects_grossly_time_scaled_retained_mapping(
    output_end: float,
) -> None:
    """Capability admission must reject a forged retained-timeline scale."""
    plan = _plan("a" * 64, _fixed_offset_action())
    stretched_mapping = SourceMapping(
        source_start=0.0,
        source_end=4.0,
        output_start=0.0,
        output_end=output_end,
        output_relative_path="faithful-rescue.mp4",
    )

    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(plan, (stretched_mapping,))


def test_fixed_offset_allows_retained_mapping_within_native_duration_tolerance() -> (
    None
):
    plan = _plan("a" * 64, _fixed_offset_action())
    measured_mapping = SourceMapping(
        source_start=0.0,
        source_end=4.0,
        output_start=0.0,
        output_end=4.25,
        output_relative_path="faithful-rescue.mp4",
    )

    require_executable_action_scopes(plan, (measured_mapping,))


def test_fixed_offset_rejects_accumulated_retained_mapping_drift() -> None:
    """Two individually tolerated segments must not accumulate a global scale."""
    plan = _two_segment_fixed_offset_plan("a" * 64)
    mappings = (
        SourceMapping(0.0, 1.0, 0.0, 1.2, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.2, 3.4, "faithful-rescue.mp4"),
    )

    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(plan, mappings)


@pytest.mark.parametrize(
    "mappings",
    (
        (SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),),
        (
            SourceMapping(2.0, 4.0, 0.0, 2.0, "faithful-rescue.mp4"),
            SourceMapping(0.0, 1.0, 2.0, 3.0, "faithful-rescue.mp4"),
        ),
    ),
)
def test_local_improvement_rejects_omitted_or_reordered_retained_ranges(
    mappings: tuple[SourceMapping, ...],
) -> None:
    """Every mapped content change must execute the exact retained plan timeline."""
    plan = _two_segment_luma_plan("a" * 64)

    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(plan, mappings)


@pytest.mark.parametrize(
    ("mappings", "duration"),
    (
        ((SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),), 1.0),
        (
            (
                SourceMapping(2.0, 4.0, 0.0, 2.0, "faithful-rescue.mp4"),
                SourceMapping(0.0, 1.0, 2.0, 3.0, "faithful-rescue.mp4"),
            ),
            3.0,
        ),
    ),
)
def test_verifier_rejects_omitted_or_reordered_plan_retained_ranges(
    tmp_path: Path,
    mappings: tuple[SourceMapping, ...],
    duration: float,
) -> None:
    """A self-consistent artifact mapping cannot replace the plan-bound timeline."""
    input_hash = sha256(b"source").hexdigest()
    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": duration},
        mappings=mappings,
        mapped_reference_updates={},
        plan=_two_segment_luma_plan(input_hash),
    )

    assert (
        _check(report, "faithful", "source_mapping").status
        is RescueVerificationStatus.FAILED
    )


def test_faithful_fixed_offset_check_uses_native_residual(tmp_path: Path) -> None:
    """Catches checking the shift plan without measuring the faithful artifact."""
    report = _verify(
        tmp_path,
        faithful_updates={
            "av_offset_seconds": 0.01,
            "av_offset_method": "first_usable_packet_timestamp",
            "av_offset_tool_version": "ffprobe test-version",
        },
        actions=(_fixed_offset_action(),),
    )

    check = _check(report, "faithful", "fixed_av_offset")
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured == {
        "applicable": True,
        "measurement_method": "first_usable_packet_timestamp",
        "tool_version": "ffprobe test-version",
        "planned_offset_seconds": 0.4,
        "planned_shift_seconds": -0.4,
        "observed_residual_seconds": 0.01,
        "tolerance_seconds": 0.04,
    }


def test_fixed_offset_reference_uses_explicit_packet_origin_rendering(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    faithful = tmp_path / "faithful-rescue.mp4"
    source.write_bytes(b"source")
    faithful.write_bytes(b"faithful")
    mappings = (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),)
    provider = _FakeMeasurementProvider(
        {source: _snapshot(source), faithful: _snapshot(faithful)}
    )

    RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=None,
        plan=_plan(sha256(source.read_bytes()).hexdigest(), _fixed_offset_action()),
        mappings=mappings,
        faithful_render_mode="single_reencode",
    )

    assert provider.mapped_reference_calls == [
        (source, mappings, ReferenceRenderOptions(preserve_packet_origin=True))
    ]


def test_real_fixed_offset_correction_reduces_packet_residual(
    tmp_path: Path,
) -> None:
    """Catches reporting a planned shift without native output packet evidence."""
    filename = "rescue_fixed_av_offset.mp4"
    source = _real_fixture(filename)
    source_hash = sha256(source.read_bytes()).hexdigest()
    manifest = cast(
        dict[str, dict[str, dict[str, Any]]],
        json.loads(REAL_MANIFEST_PATH.read_text(encoding="utf-8")),
    )
    tolerance = float(
        manifest["rescue"][filename]["acceptance"][
            "fixed_offset_residual_tolerance_seconds"
        ]
    )
    assessment_service = LocalRescueAssessmentService(
        sync_provider=lambda *_args, **_kwargs: SyncEventMeasurements(
            audio_events=((1.4, 0.99), (2.4, 0.99), (3.4, 0.99)),
            video_events=((1.0, 0.99), (2.0, 0.99), (3.0, 0.99)),
        )
    )
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=tmp_path / "fixed offset 中文",
            strategy=RescueStrategy.BALANCED,
        ),
        dependencies=RescuePipelineDependencies(assessment_service=assessment_service),
    )
    preparation = pipeline.prepare(source)
    correction = next(
        action
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
    )
    assert _json_number(correction.parameters["offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(correction.parameters["audio_shift_seconds"]) == pytest.approx(
        -0.4
    )
    confirmation = _real_confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    result = pipeline.execute(preparation, confirmation)

    assert result.verification is not None
    check = _check(result.verification, "faithful", "fixed_av_offset")
    assert check.status is RescueVerificationStatus.PASSED
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.verification.faithful_status is RescueVerificationStatus.PASSED
    assert result.status in {RescueStatus.COMPLETED, RescueStatus.PARTIAL}
    assert check.measured["measurement_method"] == "first_usable_packet_timestamp"
    assert check.measured["tool_version"]
    assert _json_number(check.measured["planned_offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(check.measured["planned_shift_seconds"]) == pytest.approx(-0.4)
    assert _json_number(check.measured["tolerance_seconds"]) == tolerance
    assert abs(_json_number(check.measured["observed_residual_seconds"])) <= tolerance
    assert sha256(source.read_bytes()).hexdigest() == source_hash


class _InjectedMiddleDamageScanner:
    """Bind one exact structural deletion while retaining native media execution."""

    def scan(
        self,
        _source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        _config: object,
    ) -> MediaDamageMap:
        intervals = tuple(
            DamageInterval(
                id=make_damage_id(
                    source_hash,
                    "video:0",
                    kind,
                    start_seconds,
                    end_seconds,
                ),
                stream_id="video:0",
                kind=kind,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                measurements={"fixture_evidence": "injected_middle_damage_v1"},
            )
            for kind, start_seconds, end_seconds in (
                (DamageKind.DECODABLE, 0.0, 2.0),
                (DamageKind.UNDECODABLE, 2.0, 3.0),
                (DamageKind.DECODABLE, 3.0, metadata.duration_seconds),
            )
        )
        return MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=metadata.duration_seconds,
            scan_coverage=((0.0, metadata.duration_seconds),),
            intervals=intervals,
        )


def test_real_fixed_offset_survives_two_segment_salvage_concat(
    tmp_path: Path,
) -> None:
    """Catches concat reintroducing A/V offset after middle-range salvage."""
    filename = "rescue_fixed_av_offset.mp4"
    source = _real_fixture(filename)
    source_hash = sha256(source.read_bytes()).hexdigest()
    manifest = cast(
        dict[str, dict[str, dict[str, Any]]],
        json.loads(REAL_MANIFEST_PATH.read_text(encoding="utf-8")),
    )
    tolerance = float(
        manifest["rescue"][filename]["acceptance"][
            "fixed_offset_residual_tolerance_seconds"
        ]
    )
    assessment_service = LocalRescueAssessmentService(
        sync_provider=lambda *_args, **_kwargs: SyncEventMeasurements(
            audio_events=((1.4, 0.99), (2.4, 0.99), (3.4, 0.99)),
            video_events=((1.0, 0.99), (2.0, 0.99), (3.0, 0.99)),
        )
    )
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=tmp_path / "fixed offset salvage 中文",
            strategy=RescueStrategy.BALANCED,
        ),
        dependencies=RescuePipelineDependencies(
            scanner=_InjectedMiddleDamageScanner(),
            assessment_service=assessment_service,
        ),
    )
    preparation = pipeline.prepare(source)
    correction = next(
        action
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
    )
    assert _json_number(correction.parameters["offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(correction.parameters["audio_shift_seconds"]) == pytest.approx(
        -0.4
    )
    assert preparation.previews is not None
    assert correction.id in preparation.previews.previewed_action_ids
    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(
            preparation.plan,
            (
                SourceMapping(
                    source_start=0.0,
                    source_end=2.0,
                    output_start=0.0,
                    output_end=2.0,
                    output_relative_path="faithful-rescue.mp4",
                ),
            ),
        )
    confirmation = _real_confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    result = pipeline.execute(preparation, confirmation)

    assert result.failed_source_ranges == ((2.0, 3.0),)
    assert len(result.source_mappings) == 2
    source_ranges = tuple(
        (mapping.source_start, mapping.source_end) for mapping in result.source_mappings
    )
    assert source_ranges[0] == pytest.approx((0.0, 2.0), abs=0.11)
    assert source_ranges[1] == pytest.approx((3.0, 6.0), abs=0.11)
    assert result.technical_report is not None
    check = _check(
        result.technical_report.verification,
        "faithful",
        "fixed_av_offset",
    )
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured["measurement_method"] == "first_usable_packet_timestamp"
    assert check.measured["tool_version"]
    assert _json_number(check.measured["planned_offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(check.measured["planned_shift_seconds"]) == pytest.approx(-0.4)
    assert _json_number(check.measured["tolerance_seconds"]) == tolerance
    assert abs(_json_number(check.measured["observed_residual_seconds"])) <= tolerance
    assert sha256(source.read_bytes()).hexdigest() == source_hash


def test_fixed_offset_expectation_is_inherited_by_improved_artifact(
    tmp_path: Path,
) -> None:
    """Catches dropping residual verification after faithful-only correction."""
    evidence = {
        "av_offset_seconds": 0.01,
        "av_offset_method": "first_usable_packet_timestamp",
        "av_offset_tool_version": "ffprobe test-version",
    }
    report = _verify(
        tmp_path,
        faithful_updates=evidence,
        improved_updates=evidence,
        actions=(_fixed_offset_action(),),
    )

    assert _check(report, "faithful", "fixed_av_offset").measured["applicable"]
    assert _check(report, "improved", "fixed_av_offset").measured["applicable"]


@pytest.mark.parametrize(
    ("residual", "method", "tool_version", "expected"),
    [
        (None, None, None, RescueVerificationStatus.NEEDS_REVIEW),
        (
            0.04,
            "first_usable_packet_timestamp",
            "ffprobe test-version",
            RescueVerificationStatus.PASSED,
        ),
        (
            0.040001,
            "first_usable_packet_timestamp",
            "ffprobe test-version",
            RescueVerificationStatus.NEEDS_REVIEW,
        ),
    ],
)
def test_fixed_offset_residual_requires_native_evidence_within_tolerance(
    tmp_path: Path,
    residual: float | None,
    method: str | None,
    tool_version: str | None,
    expected: RescueVerificationStatus,
) -> None:
    """Catches inferred passes and an exclusive residual tolerance boundary."""
    report = _verify(
        tmp_path,
        faithful_updates={
            "av_offset_seconds": residual,
            "av_offset_method": method,
            "av_offset_tool_version": tool_version,
        },
        actions=(_fixed_offset_action(),),
    )

    check = _check(report, "faithful", "fixed_av_offset")
    assert check.status is expected
    assert check.measured["tolerance_seconds"] == 0.04


def test_improvement_limits_apply_only_to_the_improved_artifact(
    tmp_path: Path,
) -> None:
    stabilize = _action(RescueActionKind.STABILIZE, {"max_crop_ratio": 0.08})
    normalize = _action(
        RescueActionKind.NORMALIZE_AUDIO,
        {
            "target_lufs": -16.0,
            "loudness_tolerance_lu": 1.0,
            "true_peak_limit_dbtp": -1.5,
        },
    )
    report = _verify(
        tmp_path,
        actions=(stabilize, normalize),
        faithful_updates={
            "crop_ratio": 0.5,
            "integrated_lufs": -18.0,
            "true_peak_dbtp": -1.0,
        },
        improved_updates={
            "crop_ratio": 0.09,
            "integrated_lufs": -18.0,
            "true_peak_dbtp": -1.0,
        },
    )
    for check_id in ("stabilization_crop", "audio_loudness", "audio_peak"):
        faithful = _check(report, "faithful", check_id)
        assert faithful.status is RescueVerificationStatus.PASSED
        assert faithful.measured["applicable"] is False
        assert (
            _check(report, "improved", check_id).status
            is RescueVerificationStatus.NEEDS_REVIEW
        )
    assert all(
        _check(report, "faithful", check_id).status is RescueVerificationStatus.PASSED
        for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    )
    assert report.faithful_status is RescueVerificationStatus.PASSED


def test_missing_native_stabilization_crop_evidence_requires_review(
    tmp_path: Path,
) -> None:
    """Catches treating unavailable observed crop evidence as a measured zero."""
    stabilize = _action(RescueActionKind.STABILIZE, {"max_crop_ratio": 0.08})

    report = _verify(
        tmp_path,
        actions=(stabilize,),
        improved_updates={"crop_ratio": None},
    )

    check = _check(report, "improved", "stabilization_crop")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert check.measured["observed_ratio"] is None
    assert check.measured["reason"] == "native_crop_measurement_unavailable"
    assert report.improved_status is RescueVerificationStatus.NEEDS_REVIEW


@pytest.mark.parametrize(
    "relative_path", ["../escape.mp4", "C:/private/file.mp4", "/tmp/file.mp4"]
)
def test_artifact_relative_path_is_required_and_safe(
    tmp_path: Path, relative_path: str
) -> None:
    with pytest.raises(ValueError):
        _verify(tmp_path, faithful_updates={"relative_path": relative_path})


def test_artifact_relative_path_must_match_its_public_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _verify(
            tmp_path,
            faithful_updates={"relative_path": "other-safe-name.mp4"},
            improved_updates={"relative_path": "faithful-rescue.mp4"},
        )


def test_mapping_requires_exact_contiguous_complete_source_trace(
    tmp_path: Path,
) -> None:
    invalid_sets = (
        (SourceMapping(0.0, 2.0, 0.0, 1.5, "faithful-rescue.mp4"),),
        (
            SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 2.5, 4.5, "faithful-rescue.mp4"),
        ),
        (SourceMapping(0.0, 4.0, 0.0, 4.0, "../escape.mp4"),),
        (SourceMapping(0.0, 4.0, 0.0, 4.0, "other-safe-name.mp4"),),
    )
    for index, mappings in enumerate(invalid_sets):
        report = _verify(tmp_path / str(index), mappings=mappings)
        assert (
            _check(report, "faithful", "source_mapping").status
            is RescueVerificationStatus.FAILED
        )


def test_mapping_rejects_accumulated_segment_duration_drift(tmp_path: Path) -> None:
    """Independent verification also rejects cumulative mapping time scaling."""
    mappings = (
        SourceMapping(0.0, 1.0, 0.0, 1.2, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.2, 3.4, "faithful-rescue.mp4"),
    )

    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": 3.4},
        mappings=mappings,
    )

    assert (
        _check(report, "faithful", "source_mapping").status
        is RescueVerificationStatus.FAILED
    )


def test_improved_uses_faithful_source_mapping_without_false_filename_failure(
    tmp_path: Path,
) -> None:
    report = _verify(tmp_path, improved_updates={})
    assert (
        _check(report, "improved", "source_mapping").status
        is RescueVerificationStatus.PASSED
    )


def test_snapshot_rejects_non_finite_or_out_of_range_metrics(tmp_path: Path) -> None:
    path = tmp_path / "x.mp4"
    path.write_bytes(b"x")
    for updates in (
        {"clipping_ratio": float("nan")},
        {"crop_ratio": -0.1},
        {"black_events": -1},
        {"integrated_lufs": float("inf")},
    ):
        with pytest.raises(ValueError):
            _snapshot(path, **updates)


def test_verifier_measures_every_candidate_and_binds_artifact_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    faithful = tmp_path / "faithful-rescue.mp4"
    improved = tmp_path / "improved-viewing.mp4"
    for path, content in (
        (source, b"source"),
        (faithful, b"faithful"),
        (improved, b"improved"),
    ):
        path.write_bytes(content)
    provider = _FakeMeasurementProvider(
        {path: _snapshot(path) for path in (source, faithful, improved)}
    )
    report = RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=improved,
        plan=_plan(sha256(source.read_bytes()).hexdigest()),
        mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
    )
    assert provider.calls == [
        (source, "source"),
        (faithful, "faithful-rescue.mp4"),
        (improved, "improved-viewing.mp4"),
    ]
    assert [(item.relative_path, item.sha256) for item in report.artifacts] == [
        ("faithful-rescue.mp4", sha256(b"faithful").hexdigest()),
        ("improved-viewing.mp4", sha256(b"improved").hexdigest()),
    ]


def test_native_provider_rejects_arbitrary_bytes_when_tools_are_available(
    tmp_path: Path,
) -> None:
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        pytest.skip("native FFmpeg verification tools are unavailable")
    path = tmp_path / "arbitrary.mp4"
    path.write_bytes(b"not a video")
    measured = NativeMediaMeasurementProvider().measure(
        path, "faithful-rescue.mp4", lambda: False
    )
    assert measured.complete_decode is False
    assert measured.video_stream_count == 0


def test_native_provider_decodes_real_generated_fixture_when_available() -> None:
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        pytest.skip("native FFmpeg verification tools are unavailable")
    fixture = Path("tests/fixtures/generated/clean_motion.mp4")
    if not fixture.is_file():
        pytest.skip("generated native fixture is unavailable")
    measured = NativeMediaMeasurementProvider().measure(
        fixture, "faithful-rescue.mp4", lambda: False
    )
    assert measured.complete_decode is True
    assert measured.video_stream_count == 1
    assert measured.duration_seconds > 0
