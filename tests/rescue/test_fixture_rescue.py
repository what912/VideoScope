"""Real FFmpeg acceptance for deterministic Video Rescue fixtures."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
import pytest

from videoscope.rescue.assessment import RescueAssessmentBundle
from videoscope.rescue.errors import RescueConfirmationError
from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    RescueActionKind,
    RescueConfirmation,
    RescueStrategy,
    RescueVerificationStatus,
    make_damage_id,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    RescueResult,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.rescue.verification import (
    MediaVerificationSnapshot,
    NativeMediaMeasurementProvider,
    RescueVerifier,
)
from videoscope.rescue.visual import FlickerCorrectionPlan

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "generated"
MANIFEST_PATH = Path(__file__).parents[1] / "fixtures" / "manifest.json"
RESCUE_FIXTURE_NAMES = (
    "rescue_clean_av.mp4",
    "rescue_dark_noise.mp4",
    "rescue_fixed_av_offset.mp4",
    "rescue_flicker_middle_damaged.mp4",
    "rescue_flicker.mp4",
    "rescue_low_loudness.mp4",
    "rescue_middle_damaged.mp4",
    "rescue_missing_audio.mp4",
    "rescue_shake.mp4",
    "rescue_soft_detail.mp4",
    "rescue_tail_damaged.mp4",
)

COMBINED_STRUCTURAL_DEFLICKER_FIXTURE = "rescue_flicker_middle_damaged.mp4"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verification_diagnostics(result: RescueResult) -> tuple[dict[str, Any], ...]:
    """Keep cross-platform acceptance failures actionable in CI logs."""
    if result.verification is None:
        return ()
    return tuple(check.model_dump(mode="json") for check in result.verification.checks)


@pytest.fixture
def rescue_dark_noise() -> Iterator[Path]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and ffprobe are required for real Rescue acceptance")
    source = FIXTURE_ROOT / "rescue_dark_noise.mp4"
    if not source.is_file():
        pytest.skip("generate deterministic fixtures before real Rescue acceptance")
    yield source


def _fixture(filename: str) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and ffprobe are required for real Rescue acceptance")
    source = FIXTURE_ROOT / filename
    if not source.is_file():
        pytest.skip("generate deterministic fixtures before real Rescue acceptance")
    return source


IMPROVEMENT_ACTION_KINDS = {
    RescueActionKind.ADJUST_LUMA,
    RescueActionKind.DENOISE_VIDEO,
    RescueActionKind.SHARPEN,
    RescueActionKind.DEFLICKER,
    RescueActionKind.STABILIZE,
    RescueActionKind.NORMALIZE_AUDIO,
    RescueActionKind.DENOISE_AUDIO,
    RescueActionKind.CORRECT_FIXED_AV_OFFSET,
}


def _confirmation(
    preparation: Any,
) -> RescueConfirmation:
    accepted = tuple(
        action.id for action in preparation.plan.actions if action.requires_confirmation
    )
    trim_damage_ids = tuple(
        damage_id
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.TRIM_DAMAGED_EDGES and action.id in accepted
        for damage_id in action.parameters.get("damage_ids", ())
        if isinstance(damage_id, str)
    )
    publish_improved = any(
        action.id in accepted
        and action.strategy is RescueStrategy.BALANCED
        and action.kind in IMPROVEMENT_ACTION_KINDS
        for action in preparation.plan.actions
    )
    return RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=publish_improved,
        accepted_action_ids=accepted,
        accepted_trim_damage_ids=trim_damage_ids,
    )


def run_confirmed_balanced_rescue(
    source: Path,
    output: Path,
    *,
    dependencies: RescuePipelineDependencies | None = None,
    include_improvements: bool = True,
    locked_ranges: tuple[tuple[float, float], ...] = (),
) -> RescueResult:
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=output,
            strategy=(
                RescueStrategy.BALANCED
                if include_improvements
                else RescueStrategy.CONSERVATIVE
            ),
            locked_ranges=locked_ranges,
        ),
        dependencies=dependencies,
    )
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)
    return pipeline.execute(preparation, confirmation)


def _manifest_entry(filename: str) -> dict[str, Any]:
    manifest = cast(
        dict[str, dict[str, dict[str, Any]]],
        json.loads(MANIFEST_PATH.read_text(encoding="utf-8")),
    )
    for section in ("rescue", "rescue_derivatives"):
        if filename in manifest.get(section, {}):
            return manifest[section][filename]
    raise KeyError(filename)


def test_combined_structural_deflicker_fixture_contract_is_declared() -> None:
    """Catches dropping the fixed source-time safety annotations for real media."""
    entry = _manifest_entry(COMBINED_STRUCTURAL_DEFLICKER_FIXTURE)

    assert entry["generation"] == "ffmpeg_filter_then_payload_zeroing"
    assert entry["source_deletion_interval"] == {
        "start_seconds": 2.0,
        "end_seconds": 3.0,
    }
    assert entry["authorized_correction_intervals"] == [
        {"start_seconds": 0.5, "end_seconds": 3.5}
    ]
    assert entry["locked_interval"] == {
        "start_seconds": 3.5,
        "end_seconds": 4.5,
    }
    assert entry["clean_interval"] == {
        "start_seconds": 5.0,
        "end_seconds": 6.0,
    }
    assert entry["acceptance"]["maximum_residual_luma"] == 0.14
    assert entry["acceptance"]["mapping_tolerance_seconds"] == 0.11
    assert entry["acceptance"]["decoded_frame_tolerance"] == 0.04


def _streamed_luma_metrics(
    path: Path,
    time_ranges: tuple[tuple[float, float], ...] = (),
) -> tuple[float, float, int]:
    capture = cv2.VideoCapture(str(path))
    frame_rate = float(capture.get(cv2.CAP_PROP_FPS))
    assert frame_rate > 0
    means: list[float] = []
    residuals: list[float] = []
    decoded_frames = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            timestamp = decoded_frames / frame_rate
            decoded_frames += 1
            if time_ranges and not any(
                start <= timestamp < end for start, end in time_ranges
            ):
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            means.append(float(np.mean(gray)) / 255.0)
            residuals.append(
                float(np.mean(np.abs(gray - cv2.GaussianBlur(gray, (3, 3), 0)))) / 255.0
            )
    finally:
        capture.release()
    assert means
    return float(np.mean(means)), float(np.mean(residuals)), len(means)


def _decoded_luma_series(path: Path) -> tuple[tuple[float, float], ...]:
    capture = cv2.VideoCapture(str(path))
    frame_rate = float(capture.get(cv2.CAP_PROP_FPS))
    assert frame_rate > 0
    samples: list[tuple[float, float]] = []
    frame_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            samples.append(
                (
                    frame_index / frame_rate,
                    float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))) / 255.0,
                )
            )
            frame_index += 1
    finally:
        capture.release()
    assert samples
    return tuple(samples)


def _mapped_output_ranges(
    mappings: tuple[SourceMapping, ...],
    source_ranges: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    mapped: list[tuple[float, float]] = []
    for mapping in mappings:
        for source_start, source_end in source_ranges:
            overlap_start = max(mapping.source_start, source_start)
            overlap_end = min(mapping.source_end, source_end)
            if overlap_start >= overlap_end:
                continue
            mapped.append(
                (
                    mapping.output_start + overlap_start - mapping.source_start,
                    mapping.output_start + overlap_end - mapping.source_start,
                )
            )
    return tuple(mapped)


def _luma_values_in_ranges(
    samples: tuple[tuple[float, float], ...],
    ranges: tuple[tuple[float, float], ...],
) -> tuple[float, ...]:
    return tuple(
        value
        for timestamp, value in samples
        if any(start <= timestamp < end for start, end in ranges)
    )


class _CombinedStructuralDeflickerAssessment:
    def assess(
        self,
        _source: Path,
        source_hash: str,
        _metadata: object,
        _base_damage_map: object,
        _workspace: Path,
        _cancellation_callback: object,
    ) -> RescueAssessmentBundle:
        interval = DamageInterval(
            id=make_damage_id(source_hash, "video:0", DamageKind.FLICKER, 0.5, 4.5),
            stream_id="video:0",
            kind=DamageKind.FLICKER,
            start_seconds=0.5,
            end_seconds=4.5,
        )
        gains = tuple(
            (
                frame_index / 10.0,
                1.08 if frame_index % 2 == 0 else 1.0 / 1.08,
            )
            for frame_index in range(5, 46)
        )
        return RescueAssessmentBundle(
            flicker_correction=FlickerCorrectionPlan(
                intervals=((0.5, 4.5),), gains=gains
            ),
            evidence_intervals=(interval,),
            parameters={"fixture_evidence": "alternating_luma_curve_v1"},
        )


def assert_improvement_within_manifest_bounds(
    result: RescueResult, fixture_name: str
) -> None:
    assert result.faithful_path is not None
    assert result.improved_path is not None
    contract = cast(dict[str, float], _manifest_entry(fixture_name)["acceptance"])
    source = FIXTURE_ROOT / fixture_name
    source_luma, source_noise, source_frames = _streamed_luma_metrics(source)
    improved_luma, improved_noise, improved_frames = _streamed_luma_metrics(
        result.improved_path
    )
    assert improved_frames == pytest.approx(source_frames, abs=1)
    assert result.public_root is not None
    plan = cast(
        dict[str, Any],
        json.loads((result.public_root / "rescue-plan.json").read_text("utf-8")),
    )
    authorized_luma_ranges = tuple(
        (float(start), float(end))
        for action in cast(list[dict[str, Any]], plan["actions"])
        if action["kind"] == RescueActionKind.ADJUST_LUMA.value
        for start, end in cast(list[list[float]], action["source_ranges"])
    )
    assert authorized_luma_ranges
    source_luma, _source_noise, _source_frames = _streamed_luma_metrics(
        source, authorized_luma_ranges
    )
    improved_luma, _improved_noise, _improved_frames = _streamed_luma_metrics(
        result.improved_path, authorized_luma_ranges
    )
    assert improved_luma - source_luma >= contract["minimum_luma_gain"]
    assert improved_luma <= contract["maximum_mean_luma"]
    assert improved_noise <= source_noise + contract["maximum_noise_increase"]


def test_real_dark_noisy_fixture_delivers_both_verified_outputs(
    rescue_dark_noise: Path,
    tmp_path: Path,
) -> None:
    source_hash = sha256_file(rescue_dark_noise)
    result = run_confirmed_balanced_rescue(rescue_dark_noise, tmp_path / "中文 output")
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.improved_path is not None and result.improved_path.is_file()
    assert result.verification is not None
    assert result.verification.faithful_status is RescueVerificationStatus.PASSED
    assert result.verification.improved_status is RescueVerificationStatus.PASSED
    assert sha256_file(rescue_dark_noise) == source_hash
    assert_improvement_within_manifest_bounds(result, "rescue_dark_noise.mp4")


def _assert_duration_within_manifest(result: RescueResult, filename: str) -> None:
    assert result.verification is not None
    contract = cast(dict[str, float], _manifest_entry(filename)["acceptance"])
    check = next(
        item
        for item in result.verification.checks
        if item.artifact == "faithful" and item.check_id == "duration"
    )
    expected = float(cast(float, check.measured["expected_seconds"]))
    observed = float(cast(float, check.measured["observed_seconds"]))
    assert abs(observed - expected) <= contract["duration_tolerance_seconds"]


def test_real_clean_fixture_completes(tmp_path: Path) -> None:
    filename = "rescue_clean_av.mp4"
    source = _fixture(filename)
    source_hash = sha256_file(source)

    result = run_confirmed_balanced_rescue(source, tmp_path / filename)

    assert result.status is RescueStatus.COMPLETED
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert sha256_file(source) == source_hash
    _assert_duration_within_manifest(result, filename)


def test_real_missing_audio_requires_review_without_preview_capability(
    tmp_path: Path,
) -> None:
    filename = "rescue_missing_audio.mp4"
    source = _fixture(filename)
    source_hash = sha256_file(source)

    result = run_confirmed_balanced_rescue(source, tmp_path / filename)

    assert result.status is RescueStatus.NEEDS_REVIEW
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.technical_report is not None
    assert "preview_renderer_unavailable" in " ".join(
        result.technical_report.manual_review_reasons
    )
    assert sha256_file(source) == source_hash
    _assert_duration_within_manifest(result, filename)


def test_real_middle_damage_is_partial_with_manifest_bounded_ranges(
    tmp_path: Path,
) -> None:
    filename = "rescue_middle_damaged.mp4"
    source = _fixture(filename)
    source_hash = sha256_file(source)

    result = run_confirmed_balanced_rescue(
        source,
        tmp_path / "middle 中文",
        include_improvements=False,
    )

    assert result.status is RescueStatus.PARTIAL, _verification_diagnostics(result)
    assert result.faithful_path is not None and result.faithful_path.is_file()
    expected = cast(
        list[dict[str, float]], _manifest_entry(filename)["expected_damage_intervals"]
    )
    tolerance = float(_manifest_entry(filename)["damage_tolerance_seconds"])
    assert len(result.failed_source_ranges) == len(expected) == 1
    observed_start, observed_end = result.failed_source_ranges[0]
    assert abs(observed_start - expected[0]["start_seconds"]) <= tolerance
    assert abs(observed_end - expected[0]["end_seconds"]) <= tolerance
    assert sha256_file(source) == source_hash
    _assert_duration_within_manifest(result, filename)


def test_real_structural_deflicker_respects_mappings_locks_and_clean_frames(
    tmp_path: Path,
) -> None:
    """Catches applying a source-time curve after deletion or across locked ranges."""
    filename = COMBINED_STRUCTURAL_DEFLICKER_FIXTURE
    source = _fixture(filename)
    source_hash = sha256_file(source)
    entry = _manifest_entry(filename)
    deletion = cast(dict[str, float], entry["source_deletion_interval"])
    authorized = tuple(
        (float(item["start_seconds"]), float(item["end_seconds"]))
        for item in cast(
            list[dict[str, float]], entry["authorized_correction_intervals"]
        )
    )
    locked = cast(dict[str, float], entry["locked_interval"])
    clean = cast(dict[str, float], entry["clean_interval"])
    locked_ranges = ((locked["start_seconds"], locked["end_seconds"]),)

    result = run_confirmed_balanced_rescue(
        source,
        tmp_path / "combined 中文",
        dependencies=RescuePipelineDependencies(
            assessment_service=_CombinedStructuralDeflickerAssessment()
        ),
        locked_ranges=locked_ranges,
    )

    assert result.status is RescueStatus.PARTIAL, _verification_diagnostics(result)
    assert result.technical_report is not None
    assert result.technical_report.manual_review_reasons == (
        "Some observed source intervals were not retained in the faithful output.",
    )
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.improved_path is not None and result.improved_path.is_file()
    assert result.verification is not None
    assert result.verification.faithful_status is RescueVerificationStatus.PASSED
    assert result.verification.improved_status is RescueVerificationStatus.PASSED
    assert sha256_file(source) == source_hash
    acceptance = cast(dict[str, float], entry["acceptance"])
    mapping_tolerance = acceptance["mapping_tolerance_seconds"]
    assert len(result.failed_source_ranges) == 1
    failed_start, failed_end = result.failed_source_ranges[0]
    assert failed_start == pytest.approx(
        deletion["start_seconds"], abs=mapping_tolerance
    )
    assert failed_end == pytest.approx(deletion["end_seconds"], abs=mapping_tolerance)
    assert all(
        mapping.source_end <= deletion["start_seconds"] + mapping_tolerance
        or mapping.source_start >= deletion["end_seconds"] - mapping_tolerance
        for mapping in result.source_mappings
    )

    authorized_output = _mapped_output_ranges(result.source_mappings, authorized)
    locked_output = _mapped_output_ranges(result.source_mappings, locked_ranges)
    clean_output = _mapped_output_ranges(
        result.source_mappings,
        ((clean["start_seconds"], clean["end_seconds"]),),
    )
    np.testing.assert_allclose(
        np.asarray(authorized_output),
        np.asarray(((0.5, 2.0), (2.0, 2.5))),
        rtol=0.0,
        atol=mapping_tolerance,
    )
    np.testing.assert_allclose(
        np.asarray(locked_output),
        np.asarray(((2.5, 3.5),)),
        rtol=0.0,
        atol=mapping_tolerance,
    )
    np.testing.assert_allclose(
        np.asarray(clean_output),
        np.asarray(((4.0, 5.0),)),
        rtol=0.0,
        atol=mapping_tolerance,
    )

    faithful_luma = _decoded_luma_series(result.faithful_path)
    improved_luma = _decoded_luma_series(result.improved_path)
    assert len(faithful_luma) == len(improved_luma)
    faithful_authorized = _luma_values_in_ranges(faithful_luma, authorized_output)
    improved_authorized = _luma_values_in_ranges(improved_luma, authorized_output)
    assert len(faithful_authorized) == len(improved_authorized) >= 10
    assert (
        float(np.mean(np.abs(np.asarray(improved_authorized) - faithful_authorized)))
        > 0.005
    )
    improved_residual = float(np.mean(np.abs(np.diff(improved_authorized))))
    faithful_residual = float(np.mean(np.abs(np.diff(faithful_authorized))))
    assert improved_residual < faithful_residual
    assert improved_residual <= acceptance["maximum_residual_luma"]

    for untouched_ranges in (locked_output, clean_output):
        faithful_untouched = _luma_values_in_ranges(faithful_luma, untouched_ranges)
        improved_untouched = _luma_values_in_ranges(improved_luma, untouched_ranges)
        assert len(faithful_untouched) == len(improved_untouched) >= 8
        assert (
            float(np.max(np.abs(np.asarray(improved_untouched) - faithful_untouched)))
            <= acceptance["decoded_frame_tolerance"]
        )

    flicker_check = next(
        check
        for check in result.verification.checks
        if check.artifact == "improved" and check.check_id == "flicker_regression"
    )
    assert flicker_check.status is RescueVerificationStatus.PASSED
    assert flicker_check.measured["applicable"] is True


@pytest.mark.parametrize("filename", RESCUE_FIXTURE_NAMES)
def test_every_real_rescue_fixture_matches_manifest_structural_outcome(
    filename: str,
    tmp_path: Path,
) -> None:
    """Manifest outcomes cover faithful structural salvage, before improvements."""
    source = _fixture(filename)
    source_hash = sha256_file(source)
    entry = _manifest_entry(filename)

    result = run_confirmed_balanced_rescue(
        source,
        tmp_path / f"all outcomes {filename}",
        include_improvements=False,
    )

    assert entry["acceptance"]["outcome_scope"] == "faithful_structural"
    assert result.status.value == entry["acceptance"]["expected_outcome"]
    assert result.improved_path is None
    assert sha256_file(source) == source_hash
    _assert_duration_within_manifest(result, filename)
    if filename == "rescue_tail_damaged.mp4":
        assert result.status is RescueStatus.PARTIAL
        assert len(result.failed_source_ranges) == 1
        observed_start, observed_end = result.failed_source_ranges[0]
        assert observed_start == pytest.approx(5.0, abs=1.0)
        assert observed_end == pytest.approx(6.0, abs=1.0)


class _InjectedSideEffectProvider:
    def __init__(
        self,
        *,
        fail_faithful_decode: bool = False,
        faithful_visual_regression: bool = False,
    ) -> None:
        self._native = NativeMediaMeasurementProvider()
        self._fail_faithful_decode = fail_faithful_decode
        self._faithful_visual_regression = faithful_visual_regression

    def measure(
        self, path: Path, relative_path: str, cancellation_callback: Any
    ) -> MediaVerificationSnapshot:
        measured = self._native.measure(path, relative_path, cancellation_callback)
        if relative_path == "improved-viewing.mp4":
            return replace(measured, clipping_ratio=1.0)
        if relative_path == "faithful-rescue.mp4" and self._faithful_visual_regression:
            return replace(
                measured,
                black_events=measured.black_events + 1,
                freeze_events=measured.freeze_events + 1,
            )
        if relative_path == "faithful-rescue.mp4" and self._fail_faithful_decode:
            return replace(measured, complete_decode=False)
        return measured

    def measure_mapped_reference(
        self,
        path: Path,
        mappings: tuple[Any, ...],
        render_mode: Any,
        reference_options: Any,
        cancellation_callback: Any,
    ) -> MediaVerificationSnapshot:
        return self._native.measure_mapped_reference(
            path,
            mappings,
            render_mode,
            reference_options,
            cancellation_callback,
        )


def test_retained_range_visual_regression_blocks_partial_publication(
    tmp_path: Path,
) -> None:
    source = _fixture("rescue_middle_damaged.mp4")
    dependencies = RescuePipelineDependencies(
        verifier=RescueVerifier(
            measurement_provider=_InjectedSideEffectProvider(
                faithful_visual_regression=True
            )
        )
    )

    result = run_confirmed_balanced_rescue(
        source,
        tmp_path / "retained regression",
        dependencies=dependencies,
    )

    assert result.verification is not None
    assert result.verification.faithful_status is RescueVerificationStatus.NEEDS_REVIEW
    assert result.status is RescueStatus.FAILED
    assert result.public_root is None
    assert result.faithful_path is None
    for check_id in ("black_regression", "freeze_regression"):
        check = next(
            item
            for item in result.verification.checks
            if item.artifact == "faithful" and item.check_id == check_id
        )
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.measured["applicable"] is True
        assert check.measured["reference"] == "retained_source_ranges"


def test_injected_improved_side_effect_needs_review_without_invalidating_faithful(
    tmp_path: Path,
) -> None:
    source = _fixture("rescue_dark_noise.mp4")
    dependencies = RescuePipelineDependencies(
        verifier=RescueVerifier(measurement_provider=_InjectedSideEffectProvider())
    )

    result = run_confirmed_balanced_rescue(
        source, tmp_path / "side effect", dependencies=dependencies
    )

    assert result.status is RescueStatus.NEEDS_REVIEW
    assert result.verification is not None
    assert result.verification.faithful_status is RescueVerificationStatus.PASSED
    assert result.verification.improved_status is RescueVerificationStatus.NEEDS_REVIEW
    clipping = next(
        item
        for item in result.verification.checks
        if item.artifact == "improved" and item.check_id == "luma_clipping"
    )
    output_ratio = clipping.measured["output_ratio"]
    source_ratio = clipping.measured["source_ratio"]
    assert isinstance(output_ratio, (int, float)) and not isinstance(output_ratio, bool)
    assert isinstance(source_ratio, (int, float)) and not isinstance(source_ratio, bool)
    assert float(output_ratio) == 1.0
    assert float(output_ratio) > float(source_ratio)


def test_injected_zero_safe_output_fails_without_public_media(tmp_path: Path) -> None:
    source = _fixture("rescue_clean_av.mp4")
    dependencies = RescuePipelineDependencies(
        verifier=RescueVerifier(
            measurement_provider=_InjectedSideEffectProvider(fail_faithful_decode=True)
        )
    )

    result = run_confirmed_balanced_rescue(
        source, tmp_path / "zero safe", dependencies=dependencies
    )

    assert result.status is RescueStatus.FAILED
    assert result.faithful_path is None
    assert result.public_root is None
    assert result.verification is not None
    decodable = next(
        item
        for item in result.verification.checks
        if item.artifact == "faithful" and item.check_id == "decodable"
    )
    assert decodable.status is RescueVerificationStatus.FAILED
    assert decodable.measured == {"complete_decode": False}


def test_real_execution_can_be_cancelled_before_any_public_output(
    tmp_path: Path,
) -> None:
    source = _fixture("rescue_clean_av.mp4")
    source_hash = sha256_file(source)
    statuses: list[RescueStatus] = []
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=tmp_path / "cancelled 中文",
            strategy=RescueStrategy.BALANCED,
        ),
        progress=statuses.append,
    )
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)
    pipeline.cancel()

    with pytest.raises(RescueConfirmationError):
        pipeline.execute(preparation, confirmation)

    assert statuses[-1] is RescueStatus.AWAITING_CONFIRMATION
    assert RescueStatus.PROCESSING not in statuses
    assert not (tmp_path / "cancelled 中文" / "rescue-output").exists()
    assert sha256_file(source) == source_hash
