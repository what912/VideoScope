"""Contract tests for the private, local-only V15 demo verifier."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import cv2
import numpy as np
import pytest


def _module() -> Any:
    return importlib.import_module("scripts.verify_b_v15_demo")


def test_verifier_public_contract_exists() -> None:
    module = _module()
    assert callable(module.verify_demo)
    assert callable(module.main)


def test_verifier_rejects_missing_local_inputs(tmp_path: Path) -> None:
    module = _module()
    with pytest.raises(module.DemoVerificationError, match="source"):
        module.verify_demo(
            source=tmp_path / "missing-source.mp4",
            v14=tmp_path / "v14.mp4",
            candidate=tmp_path / "candidate.mp4",
            clean_reference=tmp_path / "clean.mp4",
            output=tmp_path / "review",
            ffmpeg=Path("ffmpeg"),
            ffprobe=Path("ffprobe"),
        )


def _request(tmp_path: Path) -> dict[str, Path]:
    inputs: dict[str, Path] = {}
    for label in ("source", "v14", "candidate", "clean_reference"):
        path = tmp_path / f"{label}.mp4"
        path.write_bytes(label.encode("ascii"))
        inputs[label] = path
    return {
        **inputs,
        "output": tmp_path / "review",
        "ffmpeg": Path("ffmpeg"),
        "ffprobe": Path("ffprobe"),
    }


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "missing_label", ("source", "v14", "candidate", "clean_reference")
)
def test_verifier_rejects_each_missing_input(
    tmp_path: Path, missing_label: str
) -> None:
    module = _module()
    request = _request(tmp_path)
    request[missing_label].unlink()
    with pytest.raises(module.DemoVerificationError, match=missing_label):
        module.verify_demo(**request)


def test_verifier_rejects_directory_input(tmp_path: Path) -> None:
    module = _module()
    request = _request(tmp_path)
    request["source"].unlink()
    request["source"].mkdir()
    with pytest.raises(module.DemoVerificationError, match="source"):
        module.verify_demo(**request)


def test_verifier_rejects_input_alias_before_measurement(tmp_path: Path) -> None:
    module = _module()
    request = _request(tmp_path)
    request["candidate"] = request["source"]
    with pytest.raises(module.DemoVerificationError, match="alias"):
        module.verify_demo(**request)


def test_verifier_rejects_candidate_with_source_bytes(tmp_path: Path) -> None:
    module = _module()
    request = _request(tmp_path)
    request["candidate"].write_bytes(request["source"].read_bytes())
    with pytest.raises(module.DemoVerificationError, match="candidate"):
        module.verify_demo(**request)


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    module = _module()
    request = _request(tmp_path)
    output = request["output"]
    output.mkdir()
    sentinel = output / "approved.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(module.DemoVerificationError, match="exists"):
        module.verify_demo(**request)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_external_commands_use_argument_arrays_and_shell_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    observed: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> object:
        observed["arguments"] = arguments
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module._run_external(["ffprobe", "-v", "error"]) == b"ok"
    assert observed["arguments"] == ["ffprobe", "-v", "error"]
    assert observed["shell"] is False


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "mode", ("missing", "timeout", "nonzero", "oversize")
)
def test_external_command_failures_are_structured_and_path_free(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    module = _module()

    def fake_run(_arguments: list[str], **_kwargs: object) -> object:
        if mode == "missing":
            raise FileNotFoundError("C:/private/tool.exe")
        if mode == "timeout":
            raise subprocess.TimeoutExpired("C:/private/tool.exe", 1.0)
        if mode == "nonzero":
            return SimpleNamespace(
                returncode=3, stdout=b"", stderr=b"C:/private/source.mp4 failed"
            )
        return SimpleNamespace(returncode=0, stdout=b"x" * 33, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(module.DemoVerificationError) as raised:
        module._run_external(["C:/private/tool.exe"], maximum_output_bytes=32)
    assert "C:/private" not in str(raised.value)


def _contract_payload() -> dict[str, object]:
    return {
        "expected_duration_seconds": 2.0,
        "expected_width": 320,
        "expected_height": 180,
        "expected_frame_rate": 4.0,
        "expected_sample_rate": 48_000,
        "clarity_timestamp_seconds": 0.5,
        "audio_start_seconds": 0.8,
        "audio_end_seconds": 1.2,
        "motion_start_seconds": 1.2,
        "motion_end_seconds": 1.8,
        "target_intervals": ((0.4, 0.7), (0.8, 1.8)),
    }


def _probe_documents() -> tuple[bytes, bytes]:
    probe = {
        "format": {"duration": "2.000000"},
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 180,
                "avg_frame_rate": "4/1",
                "duration": "2.000000",
                "nb_frames": "8",
            },
            {
                "codec_type": "audio",
                "sample_rate": "48000",
                "duration": "2.000000",
            },
        ],
    }
    frames = {
        "frames": [
            {"best_effort_timestamp_time": f"{index / 4:.6f}"} for index in range(8)
        ]
    }
    return json.dumps(probe).encode(), json.dumps(frames).encode()


def test_demo_contract_rejects_nonfinite_and_old_weak_thresholds() -> None:
    module = _module()
    payload = _contract_payload()
    for key, value in (
        ("expected_duration_seconds", math.nan),
        ("minimum_tonal_attenuation_db", 12.0),
        ("maximum_motion_p90_pixels", 4.0),
    ):
        invalid = dict(payload)
        invalid[key] = value
        with pytest.raises(ValueError):
            module.DemoContract(**invalid)


def test_media_probe_requires_audio_and_exact_actual_pts() -> None:
    module = _module()
    probe, frames = _probe_documents()
    info = module._parse_media_probe(
        label="source",
        sha256="a" * 64,
        probe_bytes=probe,
        frame_bytes=frames,
    )
    assert info.frame_timestamps == tuple(index / 4 for index in range(8))
    assert info.sample_rate == 48_000
    assert info.frame_count == 8


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "failure", ("bad_json", "missing_audio", "bad_pts")
)
def test_media_probe_fails_closed_on_invalid_documents(failure: str) -> None:
    module = _module()
    probe_bytes, frame_bytes = _probe_documents()
    probe = json.loads(probe_bytes)
    frames = json.loads(frame_bytes)
    if failure == "bad_json":
        probe_bytes = b"{truncated"
    elif failure == "missing_audio":
        probe["streams"] = [probe["streams"][0]]
        probe_bytes = json.dumps(probe).encode()
    else:
        frames["frames"][3]["best_effort_timestamp_time"] = "NaN"
        frame_bytes = json.dumps(frames).encode()
    with pytest.raises(module.DemoVerificationError):
        module._parse_media_probe(
            label="candidate",
            sha256="b" * 64,
            probe_bytes=probe_bytes,
            frame_bytes=frame_bytes,
        )


def test_media_set_rejects_pts_duration_and_hash_mismatch() -> None:
    module = _module()
    probe, frames = _probe_documents()
    base = module._parse_media_probe(
        label="source", sha256="a" * 64, probe_bytes=probe, frame_bytes=frames
    )
    contract = module.DemoContract(**_contract_payload())
    module._validate_media_set((base, base, base, base), contract)
    for update in (
        {"duration_seconds": 2.2},
        {"frame_timestamps": (*base.frame_timestamps[:-1], 1.9)},
        {"sha256": "not-a-hash"},
    ):
        bad = base.model_copy(update=update)
        with pytest.raises(module.DemoVerificationError):
            module._validate_media_set((base, base, base, bad), contract)


def test_media_set_rejects_shared_non_cfr_actual_pts() -> None:
    module = _module()
    probe, frames = _probe_documents()
    base = module._parse_media_probe(
        label="source", sha256="a" * 64, probe_bytes=probe, frame_bytes=frames
    )
    shared_bad = base.model_copy(
        update={
            "frame_timestamps": (
                0.1,
                0.35,
                0.6,
                0.85,
                1.1,
                1.35,
                1.6,
                1.85,
            )
        }
    )
    with pytest.raises(module.DemoVerificationError, match="CFR"):
        module._validate_media_set(
            (shared_bad, shared_bad, shared_bad, shared_bad),
            module.DemoContract(**_contract_payload()),
        )


def test_probe_performs_full_decode_and_reads_actual_frame_inventory(
    tmp_path: Path,
) -> None:
    module = _module()
    path = tmp_path / "本地 media.mp4"
    path.write_bytes(b"stable-media")
    probe, frames = _probe_documents()
    commands: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: object) -> bytes:
        commands.append(arguments)
        if "-show_frames" in arguments:
            return frames
        if "-show_streams" in arguments:
            return probe
        return b""

    info = module._probe_media(
        label="source",
        path=path,
        ffprobe=Path("ffprobe"),
        ffmpeg=Path("ffmpeg"),
        runner=runner,
    )

    assert info.frame_count == 8
    assert len(commands) == 3
    assert "-show_frames" in commands[1]
    assert commands[2][-2:] == ["null", "-"]
    assert str(path) in commands[2]
    assert "-xerror" in commands[2]
    assert commands[2][commands[2].index("-err_detect") + 1] == "explode"


def test_probe_rejects_media_mutation_during_measurement(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "source.mp4"
    path.write_bytes(b"before")
    probe, frames = _probe_documents()
    calls = 0

    def runner(arguments: list[str], **_kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"after")
        if "-show_frames" in arguments:
            return frames
        if "-show_streams" in arguments:
            return probe
        return b""

    with pytest.raises(module.DemoVerificationError, match="changed"):
        module._probe_media(
            label="source",
            path=path,
            ffprobe=Path("ffprobe"),
            ffmpeg=Path("ffmpeg"),
            runner=runner,
        )


def _clarity_frames() -> dict[str, np.ndarray[Any, np.dtype[np.uint8]]]:
    clean: np.ndarray[Any, np.dtype[np.uint8]] = np.full(
        (180, 320, 3), 22, dtype=np.uint8
    )
    cv2.putText(
        clean,
        "OBSERVE THE SIGNAL",
        (18, 82),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        (240, 240, 240),
        2,
        cv2.LINE_AA,
    )
    cv2.rectangle(clean, (20, 105), (300, 150), (80, 210, 190), 2)
    source = cv2.GaussianBlur(clean, (0, 0), 3.0)
    v14 = cv2.GaussianBlur(clean, (0, 0), 2.5)
    return {"source": source, "v14": v14, "candidate": clean, "clean": clean}


def test_clarity_gate_requires_structure_edge_continuity_and_side_effects() -> None:
    module = _module()
    frames = _clarity_frames()
    temporal = {label: (frame, frame) for label, frame in frames.items()}

    measured = module._measure_clarity_frames(frames, temporal)

    assert measured["status"] == "passed"
    assert set(measured["checks"]) == {
        "multi_scale_edge_spread",
        "edge_continuity",
        "structural_similarity",
        "ringing_noise",
        "temporal_residual",
    }
    assert all(item["status"] == "passed" for item in measured["checks"].values())


def test_global_sharpness_increase_cannot_pass_clarity_gate() -> None:
    module = _module()
    frames = _clarity_frames()
    bad = frames["source"].copy()
    checker = (np.indices(bad.shape[:2]).sum(axis=0) % 2).astype(np.uint8) * 90
    bad = np.clip(bad.astype(np.int16) + checker[..., None], 0, 255).astype(np.uint8)
    assert (
        cv2.Laplacian(bad, cv2.CV_64F).var()
        > cv2.Laplacian(frames["source"], cv2.CV_64F).var()
    )
    frames["candidate"] = bad
    temporal = {label: (frame, frame) for label, frame in frames.items()}

    measured = module._measure_clarity_frames(frames, temporal)

    assert measured["status"] == "failed"
    assert measured["checks"]["ringing_noise"]["status"] == "failed"


def test_local_text_blur_cannot_hide_inside_passing_full_frame_aggregate() -> None:
    module = _module()
    clean: np.ndarray[Any, np.dtype[np.uint8]] = np.full(
        (240, 400, 3), 18, dtype=np.uint8
    )
    for x in range(8, 400, 12):
        cv2.line(clean, (x, 100), (x, 239), (105, 135, 125), 1)
    for y in range(104, 240, 12):
        cv2.line(clean, (0, y), (399, y), (90, 125, 115), 1)
    cv2.putText(
        clean,
        "LOCAL TEXT MUST STAY CLEAR",
        (12, 62),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.66,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )
    source = cv2.GaussianBlur(clean, (0, 0), 3.0)
    v14 = cv2.GaussianBlur(clean, (0, 0), 2.5)
    candidate = clean.copy()
    localized_blur = cv2.GaussianBlur(clean, (0, 0), 5.0)
    candidate[18:82, 8:330] = localized_blur[18:82, 8:330]
    frames = {
        "source": source,
        "v14": v14,
        "candidate": candidate,
        "clean": clean,
    }
    temporal = {label: (frame, frame) for label, frame in frames.items()}

    measured = module._measure_clarity_frames(frames, temporal)

    assert all(item["status"] == "passed" for item in measured["checks"].values())
    assert measured["status"] == "failed"
    assert measured["local_regions_status"] == "failed"
    assert any(region["status"] == "failed" for region in measured["regions"])


def _audio_samples() -> dict[str, np.ndarray[Any, np.dtype[np.float32]]]:
    sample_rate = 48_000
    timeline = np.arange(sample_rate * 2, dtype=np.float64) / sample_rate
    base = 0.18 * np.sin(2.0 * np.pi * 220.0 * timeline)
    target_mask = (timeline >= 0.8) & (timeline < 1.0)
    interference = 0.28 * np.sin(2.0 * np.pi * 880.0 * timeline) * target_mask
    source = base + interference
    return {
        "source": source.astype(np.float32),
        "v14": (base + interference * 0.35).astype(np.float32),
        "candidate": base.astype(np.float32),
        "clean": base.astype(np.float32),
    }


def test_audio_gate_uses_every_complete_50ms_window_and_preserves_base() -> None:
    module = _module()
    contract = module.DemoContract(**_contract_payload())

    measured = module._measure_audio_samples(_audio_samples(), 48_000, contract)

    assert measured["status"] == "passed"
    assert len(measured["windows"]) == 8
    assert measured["checks"]["minimum_target_attenuation"]["value_db"] >= 24.0
    assert measured["checks"]["persistent_tone_preservation"]["status"] == "passed"
    assert measured["checks"]["boundary_transient"]["status"] == "passed"


def test_audio_average_or_event_count_cannot_hide_last_target_window_failure() -> None:
    module = _module()
    samples = _audio_samples()
    sample_rate = 48_000
    start = int(0.95 * sample_rate)
    end = int(1.0 * sample_rate)
    samples["candidate"][start:end] = samples["source"][start:end]

    measured = module._measure_audio_samples(
        samples, sample_rate, module.DemoContract(**_contract_payload())
    )

    assert measured["status"] == "failed"
    assert measured["checks"]["minimum_target_attenuation"]["status"] == "failed"
    assert measured["checks"]["minimum_target_attenuation"]["value_db"] < 24.0


def test_audio_boundary_click_and_nonfinite_sample_fail_closed() -> None:
    module = _module()
    contract = module.DemoContract(**_contract_payload())
    clicked = _audio_samples()
    clicked["candidate"][int(1.0 * 48_000)] = 1.0
    measured = module._measure_audio_samples(clicked, 48_000, contract)
    assert measured["status"] == "failed"
    assert measured["checks"]["boundary_transient"]["status"] == "failed"

    invalid = _audio_samples()
    invalid["candidate"][0] = np.nan
    with pytest.raises(module.DemoVerificationError, match="finite"):
        module._measure_audio_samples(invalid, 48_000, contract)


def test_audio_right_boundary_click_is_measured_explicitly() -> None:
    module = _module()
    contract = module.DemoContract(**_contract_payload())
    clicked = _audio_samples()
    clicked["candidate"][int(contract.audio_end_seconds * 48_000)] = 1.0

    measured = module._measure_audio_samples(clicked, 48_000, contract)

    boundary = measured["checks"]["boundary_transient"]
    assert measured["status"] == "failed"
    assert boundary["status"] == "failed"
    assert set(boundary["boundaries"]) == {"left", "right"}
    assert boundary["boundaries"]["right"]["sample_jump_excess"] > 0.05


def test_audio_finite_width_boundary_energy_ramp_cannot_hide_from_rms_gate() -> None:
    module = _module()
    contract = module.DemoContract(**_contract_payload())
    ramped = _audio_samples()
    start = int(contract.audio_end_seconds * 48_000)
    width = int(0.02 * 48_000)
    ramped["candidate"][start : start + width] += np.linspace(
        0.0, 0.16, width, endpoint=False, dtype=np.float32
    )
    ramped["candidate"][start + width : start + int(0.05 * 48_000)] += 0.16

    measured = module._measure_audio_samples(ramped, 48_000, contract)

    boundary = measured["checks"]["boundary_transient"]
    assert measured["status"] == "failed"
    assert boundary["status"] == "failed"
    assert boundary["boundaries"]["right"]["rms_increase_db"] > (
        contract.maximum_boundary_rms_increase_db
    )


def _motion_frames() -> tuple[
    tuple[float, ...],
    tuple[np.ndarray[Any, np.dtype[np.uint8]], ...],
    tuple[np.ndarray[Any, np.dtype[np.uint8]], ...],
    tuple[np.ndarray[Any, np.dtype[np.uint8]], ...],
]:
    base: np.ndarray[Any, np.dtype[np.uint8]] = np.full(
        (180, 320, 3), 20, dtype=np.uint8
    )
    for x in range(20, 320, 30):
        cv2.line(base, (x, 10), (x, 170), (190, 190, 190), 1)
    for y in range(15, 180, 25):
        cv2.line(base, (10, y), (310, y), (150, 210, 180), 1)
    cv2.circle(base, (155, 92), 18, (240, 100, 80), -1)
    shifts = ((8.0, -4.0), (-7.0, 5.0), (6.0, 3.0))
    source = tuple(
        cv2.warpAffine(
            base,
            np.asarray(((1.0, 0.0, dx), (0.0, 1.0, dy)), dtype=np.float32),
            (320, 180),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        for dx, dy in shifts
    )
    clean = (base.copy(), base.copy(), base.copy())
    return (1.25, 1.5, 1.75), source, clean, clean


def test_motion_gate_compares_every_actual_pts_to_direct_clean_anchor() -> None:
    module = _module()
    timestamps, source, candidate, clean = _motion_frames()
    measured = module._measure_motion_frames(
        timestamps=timestamps,
        source_frames=source,
        candidate_frames=candidate,
        clean_frames=clean,
        contract=module.DemoContract(**_contract_payload()),
    )

    assert measured["status"] == "passed"
    assert measured["expected_frame_count"] == len(timestamps)
    assert measured["reliable_frame_count"] == len(timestamps)
    assert measured["compared_frame_count"] == len(timestamps)
    assert len(measured["frames"]) == len(timestamps)
    assert measured["residual_median_pixels"] <= 0.5
    assert measured["residual_p90_pixels"] <= 1.0


def test_motion_gate_allows_one_constant_safe_crop_after_clean_anchor() -> None:
    module = _module()
    timestamps, source, _candidate, clean = _motion_frames()
    height, width = clean[0].shape[:2]
    scale = 1.07
    crop = np.asarray(
        (
            (scale, 0.0, (width - 1) * (1.0 - scale) / 2.0 + 4.0),
            (0.0, scale, (height - 1) * (1.0 - scale) / 2.0 - 2.0),
        ),
        dtype=np.float32,
    )
    candidate = tuple(
        cv2.warpAffine(
            frame,
            crop,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        for frame in clean
    )

    measured = module._measure_motion_frames(
        timestamps=timestamps,
        source_frames=source,
        candidate_frames=candidate,
        clean_frames=clean,
        contract=module.DemoContract(**_contract_payload()),
    )

    assert measured["status"] == "passed"
    assert measured["residual_median_pixels"] <= 0.5
    assert measured["residual_p90_pixels"] <= 1.0
    assert 0.0 < measured["crop_ratio_p95"] < 0.08


def test_sparse_or_half_stabilization_cannot_pass_motion_gate() -> None:
    module = _module()
    timestamps, source, candidate, clean = _motion_frames()
    half_fixed = list(candidate)
    half_fixed[-1] = source[-1]
    measured = module._measure_motion_frames(
        timestamps=timestamps,
        source_frames=source,
        candidate_frames=tuple(half_fixed),
        clean_frames=clean,
        contract=module.DemoContract(**_contract_payload()),
    )
    assert measured["status"] == "failed"
    assert measured["residual_p90_pixels"] > 1.0

    with pytest.raises(module.DemoVerificationError, match="coverage"):
        module._measure_motion_frames(
            timestamps=timestamps,
            source_frames=source,
            candidate_frames=candidate[:-1],
            clean_frames=clean,
            contract=module.DemoContract(**_contract_payload()),
        )


def test_transition_plan_bound_motion_measurement_does_not_confuse_clean_anchor() -> (
    None
):
    """A confirmed per-PTS correction is distinct from the clean reference."""
    module = _module()
    timestamps, source, _candidate, clean = _motion_frames()
    planned = ((8.0, -4.0), (-7.0, 5.0), (6.0, 3.0))
    candidate = tuple(
        cv2.warpAffine(
            frame,
            np.asarray(((1.0, 0.0, dx), (0.0, 1.0, dy)), dtype=np.float32),
            (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        for frame, (dx, dy) in zip(source, planned, strict=True)
    )
    direct = module._measure_motion_frames(
        timestamps=timestamps,
        source_frames=source,
        candidate_frames=candidate,
        clean_frames=clean,
        contract=module.DemoContract(**_contract_payload()),
    )
    assert direct["status"] == "failed"
    assert direct["residual_p90_pixels"] > 1.0
    measured = module._measure_motion_frames(
        timestamps=timestamps,
        source_frames=source,
        candidate_frames=candidate,
        clean_frames=clean,
        contract=module.DemoContract(**_contract_payload()),
        expected_plan_frames=candidate,
    )
    assert measured["status"] == "passed"
    assert measured["residual_p90_pixels"] <= 1.0


def test_transition_plan_bound_motion_rejects_incomplete_expected_inventory() -> None:
    module = _module()
    timestamps, source, candidate, clean = _motion_frames()
    invalid_inventories: tuple[tuple[np.ndarray, ...], ...] = (
        (),
        (np.zeros((3, 3), dtype=np.uint8),),
    )
    for planned in invalid_inventories:
        with pytest.raises(module.DemoVerificationError, match="expected frame"):
            module._measure_motion_frames(
                timestamps=timestamps,
                source_frames=source,
                candidate_frames=candidate,
                clean_frames=clean,
                contract=module.DemoContract(**_contract_payload()),
                expected_plan_frames=planned,
            )


def _transition_plan(
    timestamps: tuple[float, ...],
    *,
    source_hash: str = "a" * 64,
) -> SimpleNamespace:
    action = SimpleNamespace(
        kind=SimpleNamespace(value="stabilize"),
        id="rescue_action_" + "b" * 64,
        source_ranges=((32.0, 36.0),),
        parameters={
            "algorithm_version": "1",
            "method": "transition_anchor_v1",
            "residual_goal_median_pixels": 0.5,
            "residual_goal_p90_pixels": 1.0,
            "frame_width": 640,
            "frame_height": 360,
            "crop_ratio": 0.01,
            "transition_correction_count": len(timestamps),
            "motion_transforms": [
                {
                    "timestamp_seconds": timestamp,
                    "rotation_degrees": 0.0,
                    "scale": 1.0,
                    "translation_x": float(index),
                    "translation_y": -float(index) / 2.0,
                    "semantics": "frame_correction",
                }
                for index, timestamp in enumerate(timestamps)
            ],
        },
    )
    return SimpleNamespace(
        input_hash=source_hash,
        plan_digest="c" * 64,
        actions=(action,),
    )


def test_transition_plan_binding_is_path_free_and_exact() -> None:
    module = _module()
    timestamps = (32.0, 32.041667, 32.083333)
    contract = module.DemoContract()
    plan_bytes = b'{"validated":"plan"}'

    binding = module._transition_binding_from_validated_plan(
        _transition_plan(timestamps),
        plan_bytes=plan_bytes,
        source_sha256="a" * 64,
        timestamps=timestamps,
        contract=contract,
    )

    assert binding.plan_sha256 == hashlib.sha256(plan_bytes).hexdigest()
    assert binding.plan_digest == "c" * 64
    assert binding.action_id == "rescue_action_" + "b" * 64
    assert binding.frame_width == 640
    assert binding.frame_height == 360
    assert binding.safe_crop_ratio == pytest.approx(0.01)
    assert tuple(
        (item.translation_x, item.translation_y) for item in binding.transforms
    ) == ((0.0, -0.0), (1.0, -0.5), (2.0, -1.0))


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "mutation",
    (
        "wrong_source",
        "wrong_range",
        "wrong_method",
        "wrong_goal",
        "wrong_count",
        "wrong_timestamp",
        "wrong_semantics",
        "nonfinite_translation",
    ),
)
def test_transition_plan_binding_rejects_semantic_drift(mutation: str) -> None:
    module = _module()
    timestamps = (32.0, 32.041667, 32.083333)
    plan = _transition_plan(timestamps)
    action = plan.actions[0]
    if mutation == "wrong_source":
        plan.input_hash = "d" * 64
    elif mutation == "wrong_range":
        action.source_ranges = ((32.0, 35.0),)
    elif mutation == "wrong_method":
        action.parameters["method"] = "anchor_v1"
    elif mutation == "wrong_goal":
        action.parameters["residual_goal_p90_pixels"] = 1.01
    elif mutation == "wrong_count":
        action.parameters["transition_correction_count"] = 2
    elif mutation == "wrong_timestamp":
        action.parameters["motion_transforms"][1]["timestamp_seconds"] = 32.05
    elif mutation == "wrong_semantics":
        action.parameters["motion_transforms"][1]["semantics"] = "observation"
    else:
        action.parameters["motion_transforms"][1]["translation_x"] = math.nan

    with pytest.raises(module.DemoVerificationError, match="transition plan"):
        module._transition_binding_from_validated_plan(
            plan,
            plan_bytes=b'{"validated":"plan"}',
            source_sha256="a" * 64,
            timestamps=timestamps,
            contract=module.DemoContract(),
        )


def _bundle(module: Any) -> Any:
    return module.VerificationBundle(
        metrics={
            "schema_version": "1",
            "status": "passed",
            "artifacts": {
                "contact_sheet": "frame-contact-sheet.png",
                "audio": "audio-short-windows.json",
                "motion": "motion-residual.json",
            },
        },
        audio={"schema_version": "1", "status": "passed", "windows": []},
        motion={"schema_version": "1", "status": "passed", "frames": []},
        contact_sheet_png=b"deterministic-png",
    )


def test_bundle_publication_is_byte_stable_relative_and_no_clobber(
    tmp_path: Path,
) -> None:
    module = _module()
    first = tmp_path / "第一次 review"
    second = tmp_path / "second review"
    module._publish_bundle(first, _bundle(module))
    module._publish_bundle(second, _bundle(module))
    expected = {
        "metrics.json",
        "frame-contact-sheet.png",
        "audio-short-windows.json",
        "motion-residual.json",
    }
    assert {item.name for item in first.iterdir()} == expected
    assert {name: (first / name).read_bytes() for name in expected} == {
        name: (second / name).read_bytes() for name in expected
    }
    serialized = (first / "metrics.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert not any(
        value.startswith(("/", "C:"))
        for value in json.loads(serialized)["artifacts"].values()
    )
    with pytest.raises(module.DemoVerificationError, match="exists"):
        module._publish_bundle(first, _bundle(module))


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "invalid", ("absolute", "nan")
)
def test_bundle_rejects_private_paths_and_nonfinite_json(
    tmp_path: Path, invalid: str
) -> None:
    module = _module()
    bundle = _bundle(module)
    metrics = dict(bundle.metrics)
    metrics["forged"] = (
        str(tmp_path / "private.mp4") if invalid == "absolute" else math.nan
    )
    bundle = module.VerificationBundle(
        metrics=metrics,
        audio=bundle.audio,
        motion=bundle.motion,
        contact_sheet_png=bundle.contact_sheet_png,
    )
    with pytest.raises(module.DemoVerificationError):
        module._publish_bundle(tmp_path / "review", bundle)
    assert not (tmp_path / "review").exists()


def _winerror_5() -> PermissionError:
    error = PermissionError("injected Windows sharing violation")
    error.winerror = 5
    return error


def test_verifier_windows_no_replace_rename_retries_consecutive_sharing_violations(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "completed-staging"
    target = tmp_path / "review"
    source.mkdir()
    calls: list[tuple[Path, Path]] = []
    delays: list[float] = []

    def rename(observed_source: Path, observed_target: Path) -> None:
        calls.append((observed_source, observed_target))
        if len(calls) <= 2:
            raise _winerror_5()

    module._retry_windows_no_replace_rename(
        source,
        target,
        rename=rename,
        sleep=delays.append,
    )

    assert calls == [(source, target), (source, target), (source, target)]
    assert delays == [0.01, 0.02]


def test_verifier_windows_no_replace_rename_surfaces_exhausted_sharing_violation(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "completed-staging"
    target = tmp_path / "review"
    source.mkdir()
    (source / "metrics.json").write_bytes(b"complete-bundle")
    attempts = 0
    delays: list[float] = []
    final_error = _winerror_5()

    def rename(_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise final_error

    with pytest.raises(PermissionError) as caught:
        module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is final_error
    assert attempts == 6
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16]
    assert (source / "metrics.json").read_bytes() == b"complete-bundle"
    assert not target.exists()


def test_verifier_windows_no_replace_rename_stops_when_target_appears(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "completed-staging"
    target = tmp_path / "review"
    source.mkdir()
    (source / "metrics.json").write_bytes(b"complete-bundle")
    attempts = 0
    delays: list[float] = []
    error = _winerror_5()

    def rename(_source: Path, observed_target: Path) -> None:
        nonlocal attempts
        attempts += 1
        observed_target.mkdir()
        (observed_target / "winner.txt").write_bytes(b"race-winner")
        raise error

    with pytest.raises(PermissionError) as caught:
        module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is error
    assert attempts == 1
    assert delays == []
    assert (source / "metrics.json").read_bytes() == b"complete-bundle"
    assert (target / "winner.txt").read_bytes() == b"race-winner"


def test_verifier_windows_no_replace_rename_stops_when_staging_disappears(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "completed-staging"
    target = tmp_path / "review"
    source.mkdir()
    attempts = 0
    delays: list[float] = []
    error = _winerror_5()

    def rename(observed_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        observed_source.rmdir()
        raise error

    with pytest.raises(PermissionError) as caught:
        module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is error
    assert attempts == 1
    assert delays == []
    assert not source.exists()
    assert not target.exists()


def test_verifier_windows_no_replace_rename_does_not_retry_other_os_errors(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "completed-staging"
    target = tmp_path / "review"
    source.mkdir()
    attempts = 0
    delays: list[float] = []
    error = OSError("injected non-Windows error")
    error.winerror = 32

    def rename(_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(OSError) as caught:
        module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is error
    assert attempts == 1
    assert delays == []
    assert source.is_dir()
    assert not target.exists()


def test_publication_race_preserves_winner_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    output = tmp_path / "review"

    def lose_race(source: Path, target: Path) -> None:
        del source
        target.mkdir()
        (target / "winner.txt").write_text("winner", encoding="utf-8")
        raise FileExistsError("injected race")

    monkeypatch.setattr(module, "_rename_directory_no_replace", lose_race)
    with pytest.raises(module.DemoVerificationError, match="exists"):
        module._publish_bundle(output, _bundle(module))
    assert (output / "winner.txt").read_text(encoding="utf-8") == "winner"
    assert not list(tmp_path.glob(".review.staging-*"))


def test_atomic_directory_move_never_replaces_real_empty_race_winner(
    tmp_path: Path,
) -> None:
    module = _module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "ours.txt").write_text("ours", encoding="utf-8")
    winner = tmp_path / "review"
    winner.mkdir()

    with pytest.raises(FileExistsError):
        module._rename_directory_no_replace(staging, winner)

    assert winner.is_dir()
    assert list(winner.iterdir()) == []
    assert (staging / "ours.txt").read_text(encoding="utf-8") == "ours"


def test_verify_demo_wires_measurement_bundle_to_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    request = _request(tmp_path)
    observed: dict[str, object] = {}

    def fake_build(**kwargs: object) -> object:
        observed.update(kwargs)
        return _bundle(module)

    monkeypatch.setattr(module, "_build_verification_bundle", fake_build)
    outcome = module.verify_demo(**request)

    assert outcome.status == "passed"
    assert outcome.output == request["output"]
    assert (request["output"] / "metrics.json").is_file()
    assert observed["source"] == request["source"]


def test_verify_demo_rechecks_all_hashes_after_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    request = _request(tmp_path)

    def mutate_after_probe(**_kwargs: object) -> object:
        request["v14"].write_bytes(b"changed-after-probe")
        return _bundle(module)

    monkeypatch.setattr(module, "_build_verification_bundle", mutate_after_probe)
    with pytest.raises(module.DemoVerificationError, match="changed"):
        module.verify_demo(**request)
    assert not request["output"].exists()


def test_all_acceptance_thresholds_are_strict_serialized_and_digest_bound() -> None:
    module = _module()
    contract = module.DemoContract(**_contract_payload())
    expected_fields = {
        "maximum_edge_spread_source_ratio",
        "maximum_edge_spread_v14_ratio",
        "minimum_edge_continuity_source_delta",
        "minimum_edge_continuity_v14_delta",
        "minimum_structural_similarity_delta",
        "maximum_structure_error_source_ratio",
        "maximum_structure_error_v14_ratio",
        "maximum_ringing_noise_ratio",
        "maximum_temporal_residual",
        "clarity_roi_rows",
        "clarity_roi_columns",
        "minimum_clarity_roi_edge_pixels",
        "target_evidence_margin_db",
        "maximum_persistent_tone_difference_db",
        "maximum_non_target_difference_db",
        "maximum_boundary_sample_jump_excess",
        "maximum_boundary_rms_increase_db",
        "maximum_boundary_crest_excess",
        "minimum_registration_response",
    }
    serialized = contract.model_dump(mode="json")
    assert expected_fields <= serialized.keys()
    assert (
        module._contract_digest(contract)
        == hashlib.sha256(module._canonical_json_bytes(serialized)).hexdigest()
    )
    changed = contract.model_copy(update={"maximum_temporal_residual": 0.04})
    assert module._contract_digest(changed) != module._contract_digest(contract)
    with pytest.raises(ValueError):
        module.DemoContract(
            **_contract_payload(), maximum_boundary_rms_increase_db=math.nan
        )
    with pytest.raises(ValueError):
        module.DemoContract(
            **_contract_payload(),
            maximum_edge_spread_source_ratio=0.99,
            maximum_edge_spread_v14_ratio=0.9,
        )
    with pytest.raises(ValueError):
        module.DemoContract(**_contract_payload(), clarity_roi_rows=0)


def test_verification_cancellation_leaves_no_output_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    request = _request(tmp_path)
    build_called = False

    def unexpected_build(**_kwargs: object) -> object:
        nonlocal build_called
        build_called = True
        return _bundle(module)

    monkeypatch.setattr(module, "_build_verification_bundle", unexpected_build)
    with pytest.raises(module.DemoVerificationCancelled):
        module.verify_demo(**request, cancellation_callback=lambda: True)
    assert build_called is False
    assert not request["output"].exists()
    assert not list(tmp_path.glob(".review.staging-*"))


def test_cli_test_contract_override_is_explicit_strict_and_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    contract_path = tmp_path / "native-contract.json"
    contract_path.write_text(
        json.dumps(_contract_payload(), sort_keys=True), encoding="utf-8"
    )
    observed: dict[str, object] = {}

    def fake_verify(**kwargs: object) -> object:
        observed.update(kwargs)
        return SimpleNamespace(status="passed")

    monkeypatch.setenv("VIDEOSCOPE_ALLOW_B_V15_TEST_CONTRACT", "1")
    monkeypatch.setattr(module, "verify_demo", fake_verify)
    exit_code = module.main(
        [
            "--source",
            str(tmp_path / "source.mp4"),
            "--v14",
            str(tmp_path / "v14.mp4"),
            "--candidate",
            str(tmp_path / "candidate.mp4"),
            "--clean-reference",
            str(tmp_path / "clean.mp4"),
            "--output",
            str(tmp_path / "review"),
            "--ffmpeg",
            "ffmpeg",
            "--ffprobe",
            "ffprobe",
            "--test-contract",
            str(contract_path),
        ]
    )

    assert exit_code == 0
    assert observed["contract"] == module.DemoContract(**_contract_payload())


def test_cli_test_contract_cannot_weaken_default_without_explicit_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    contract_path = tmp_path / "native-contract.json"
    contract_path.write_text(json.dumps(_contract_payload()), encoding="utf-8")
    called = False

    def unexpected_verify(**_kwargs: object) -> object:
        nonlocal called
        called = True
        return SimpleNamespace(status="passed")

    monkeypatch.delenv("VIDEOSCOPE_ALLOW_B_V15_TEST_CONTRACT", raising=False)
    monkeypatch.setattr(module, "verify_demo", unexpected_verify)
    exit_code = module.main(
        [
            "--source",
            str(tmp_path / "source.mp4"),
            "--v14",
            str(tmp_path / "v14.mp4"),
            "--candidate",
            str(tmp_path / "candidate.mp4"),
            "--clean-reference",
            str(tmp_path / "clean.mp4"),
            "--output",
            str(tmp_path / "review"),
            "--ffmpeg",
            "ffmpeg",
            "--ffprobe",
            "ffprobe",
            "--test-contract",
            str(contract_path),
        ]
    )

    assert exit_code == 2
    assert called is False


def _native_tool_paths() -> tuple[Path, Path]:
    raw = os.environ.get("VIDEOSCOPE_B_V15_FFMPEG_BIN")
    if os.environ.get("VIDEOSCOPE_RUN_B_V15_NATIVE") != "1":
        pytest.skip("set explicit local FFmpeg variables for the B V15 native case")
    if raw is None or not raw:
        pytest.skip("set explicit local FFmpeg variables for the B V15 native case")
    directory = Path(cast(str, raw))
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg = directory / f"ffmpeg{suffix}"
    ffprobe = directory / f"ffprobe{suffix}"
    if not ffmpeg.is_file() or not ffprobe.is_file():
        pytest.fail("explicit native FFmpeg 8.1.2 tools are unavailable")
    return ffmpeg, ffprobe


def _native_run(
    arguments: list[str], timeout: float = 600.0
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        shell=False,
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def _make_default_contract_native_media(
    directory: Path, ffmpeg: Path
) -> dict[str, Path]:
    base = (
        "color=c=0x10181a:s=1280x720:r=24:d=42,"
        "drawgrid=w=97:h=73:t=2:c=0x38605a,"
        "drawbox=x=85:y=65:w=420:h=90:c=white:t=5,"
        "drawbox=x=720:y=250:w=310:h=180:c=0xd09060:t=7,"
        "drawbox=x=230:y=520:w=610:h=75:c=0x70c0b0:t=6"
    )
    base_audio = "sine=f=220:r=48000:d=42"
    encoding = [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
    ]
    outputs = {
        label: directory / f"{label} local 42s.mp4"
        for label in ("source", "v14", "candidate", "clean")
    }

    clean_command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        base,
        "-f",
        "lavfi",
        "-i",
        base_audio,
        *encoding,
        str(outputs["clean"]),
    ]
    clean_result = _native_run(clean_command)
    assert clean_result.returncode == 0, clean_result.stderr[-2000:].decode(
        errors="replace"
    )

    for label, sigma, tone_volume in (
        ("source", 3.0, 2.2),
        ("v14", 1.8, 0.6),
    ):
        video = (
            f"{base},gblur=sigma={sigma}:enable='between(t,5,10)',"
            "pad=1320:760:20:20:color=0x10181a,"
            "crop=1280:720:"
            "x='if(between(t,32,36),20+8*sin(2*PI*t),20)':"
            "y='if(between(t,32,36),20+5*cos(2*PI*t),20)'"
        )
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            video,
            "-f",
            "lavfi",
            "-i",
            base_audio,
            "-f",
            "lavfi",
            "-i",
            "sine=f=880:r=48000:d=42",
            "-filter_complex",
            f"[2:a]volume='if(between(t,31.8,32.2),{tone_volume},0)':"
            "eval=frame[tone];"
            "[1:a][tone]amix=inputs=2:normalize=0[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            *encoding,
            str(outputs[label]),
        ]
        result = _native_run(command)
        assert result.returncode == 0, result.stderr[-2000:].decode(errors="replace")

    candidate_command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        base,
        "-f",
        "lavfi",
        "-i",
        base_audio,
        "-filter_complex",
        "[0:v]split[original][zoom_input];"
        "[zoom_input]crop=1272:716:4:2,scale=1280:720:flags=bicubic[zoom];"
        "[original][zoom]blend=all_expr='if(between(T,32,32.25),B,A)'[v]",
        "-map",
        "[v]",
        "-map",
        "1:a:0",
        *encoding,
        str(outputs["candidate"]),
    ]
    candidate_result = _native_run(candidate_command)
    assert candidate_result.returncode == 0, candidate_result.stderr[-2000:].decode(
        errors="replace"
    )
    assert len({_sha256_file(path) for path in outputs.values()}) == 4
    return outputs


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_native_cli_default_contract_real_media_end_to_end(tmp_path: Path) -> None:
    module = _module()
    ffmpeg, ffprobe = _native_tool_paths()
    media = _make_default_contract_native_media(tmp_path, ffmpeg)
    script = Path(module.__file__).resolve()

    def cli(output: Path, candidate: Path) -> list[str]:
        arguments = [
            sys.executable,
            str(script),
            "--source",
            str(media["source"]),
            "--v14",
            str(media["v14"]),
            "--candidate",
            str(candidate),
            "--clean-reference",
            str(media["clean"]),
            "--output",
            str(output),
            "--ffmpeg",
            str(ffmpeg),
            "--ffprobe",
            str(ffprobe),
        ]
        assert "--test-contract" not in arguments
        return arguments

    input_hashes = {label: _sha256_file(path) for label, path in media.items()}
    first = tmp_path / "默认合同 review one"
    first_result = _native_run(cli(first, media["candidate"]), timeout=1200.0)
    assert first_result.returncode == 0, first_result.stderr[-4000:].decode(
        errors="replace"
    )
    assert input_hashes == {label: _sha256_file(path) for label, path in media.items()}
    metrics = json.loads((first / "metrics.json").read_text(encoding="utf-8"))
    motion = json.loads((first / "motion-residual.json").read_text(encoding="utf-8"))[
        "measurement"
    ]
    assert metrics["contract"]["expected_duration_seconds"] == 42.0
    assert metrics["contract"]["expected_frame_rate"] == 24.0
    assert metrics["checks"]["clarity"]["actual_timestamp_seconds"] == 6.0
    assert (
        max(
            math.hypot(frame["expected_translation_x"], frame["expected_translation_y"])
            for frame in motion["frames"]
        )
        > 1.0
    )
    assert 0.005 < motion["crop_ratio_p95"] < 0.08
    assert set(metrics["artifacts"].values()) == {
        "frame-contact-sheet.png",
        "audio-short-windows.json",
        "motion-residual.json",
    }

    second = tmp_path / "default contract review two"
    second_result = _native_run(cli(second, media["candidate"]), timeout=1200.0)
    assert second_result.returncode == 0, second_result.stderr[-4000:].decode(
        errors="replace"
    )
    artifact_names = {
        "metrics.json",
        "frame-contact-sheet.png",
        "audio-short-windows.json",
        "motion-residual.json",
    }
    assert {name: _sha256_file(first / name) for name in artifact_names} == {
        name: _sha256_file(second / name) for name in artifact_names
    }

    measurable_failure = tmp_path / "measurable failure.mp4"
    remux_result = _native_run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media["source"]),
            "-map",
            "0",
            "-c",
            "copy",
            "-metadata",
            "comment=independent measurable failure",
            str(measurable_failure),
        ]
    )
    assert remux_result.returncode == 0
    assert _sha256_file(measurable_failure) != _sha256_file(media["source"])
    needs_review = tmp_path / "needs review"
    review_result = _native_run(cli(needs_review, measurable_failure), timeout=1200.0)
    assert review_result.returncode == 5
    assert (
        json.loads((needs_review / "metrics.json").read_text(encoding="utf-8"))[
            "status"
        ]
        == "needs_review"
    )

    missing_output = tmp_path / "must not publish"
    missing_argv = cli(missing_output, media["candidate"])
    missing_argv[missing_argv.index("--source") + 1] = str(tmp_path / "missing.mp4")
    error_result = _native_run(missing_argv)
    assert error_result.returncode == 2
    assert not missing_output.exists()

    corrupted = tmp_path / "corrupted frame.mp4"
    payload = bytearray(media["clean"].read_bytes())
    corruption_start = len(payload) // 2
    for index in range(corruption_start, min(corruption_start + 16_384, len(payload))):
        payload[index] ^= 0xA5
    corrupted.write_bytes(payload)
    with pytest.raises(module.DemoVerificationError, match="command failed"):
        module._run_external(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(corrupted),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=300.0,
            maximum_output_bytes=1024 * 1024,
        )
