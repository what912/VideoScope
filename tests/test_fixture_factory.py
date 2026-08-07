"""Tests for the deterministic FFmpeg fixture factory."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from PIL import Image

from scripts import generate_test_videos as factory

EXPECTED_FILENAMES = {
    "black_segment.mp4",
    "blur_segment.mp4",
    "clean_motion.mp4",
    "flicker_segment.mp4",
    "freeze_segment.mp4",
    "scene_cut.mp4",
    "stable_text.mp4",
    "changing_text.mp4",
}

EXPECTED_PRIVACY_FILENAMES = {
    "privacy_tags_av.mp4",
    "privacy_manual_visual.mp4",
    "privacy_qr.mp4",
    "privacy_text.mp4",
    "privacy_clean.mp4",
}

EXPECTED_RESCUE_FILENAMES = {
    "rescue_clean_av.mp4",
    "rescue_missing_audio.mp4",
    "rescue_low_loudness.mp4",
    "rescue_fixed_av_offset.mp4",
    "rescue_dark_noise.mp4",
    "rescue_soft_detail.mp4",
    "rescue_flicker.mp4",
    "rescue_shake.mp4",
    "rescue_tail_damaged.mp4",
    "rescue_middle_damaged.mp4",
}
COMBINED_RESCUE_FILENAME = "rescue_flicker_middle_damaged.mp4"
EXPECTED_GENERATED_RESCUE_FILENAMES = EXPECTED_RESCUE_FILENAMES | {
    COMBINED_RESCUE_FILENAME
}


def test_manifest_matches_canonical_factory_data() -> None:
    manifest_path = Path(__file__).parent / "fixtures" / "manifest.json"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert on_disk == factory.manifest_data()
    assert set(on_disk["videos"]) == EXPECTED_FILENAMES


def test_privacy_fixtures_are_declared_with_review_contracts() -> None:
    manifest = factory.manifest_data()
    privacy = cast(dict[str, dict[str, object]], manifest["privacy"])

    assert set(privacy) == EXPECTED_PRIVACY_FILENAMES
    for fixture_name, raw_entry in privacy.items():
        entry = dict(raw_entry)
        assert float(cast(float, entry["duration_seconds"])) <= 6.0
        assert int(cast(int, entry["width"])) <= 320
        assert int(cast(int, entry["height"])) <= 180
        assert entry["frame_rate"] in {10, 12}
        assert entry["timing_tolerance_seconds"] == 0.11
        assert isinstance(entry["expected_categories"], list)
        assert isinstance(entry["risks"], list)
        assert isinstance(entry["manual_visual_regions"], list)
        assert isinstance(entry["manual_audio_intervals"], list)
        for risk in entry["risks"]:
            assert risk["category"] in entry["expected_categories"]
            assert 0.0 <= risk["start_seconds"] <= risk["end_seconds"]
            if "box" in risk:
                box = risk["box"]
                assert 0.0 <= box["x_min"] < box["x_max"] <= 1.0
                assert 0.0 <= box["y_min"] < box["y_max"] <= 1.0
            assert risk["decision"] in {"allow", "redact"}


def test_rescue_fixtures_declare_deterministic_local_damage_contracts() -> None:
    specs = factory.rescue_fixture_specs()
    rescue = cast(dict[str, dict[str, object]], factory.rescue_manifest_data())

    assert {spec.filename for spec in specs} == EXPECTED_RESCUE_FILENAMES
    assert set(rescue) == EXPECTED_RESCUE_FILENAMES
    assert all(spec.gop_size is None for spec in factory.fixture_specs())
    assert all(factory._rescue_fixture_spec(spec).gop_size == 1 for spec in specs)
    for fixture_name, entry in rescue.items():
        assert entry["duration_seconds"] == 6.0
        assert isinstance(entry["width"], int) and entry["width"] <= 320
        assert isinstance(entry["height"], int) and entry["height"] <= 180
        assert entry["frame_rate"] in {10, 12}
        assert entry["source_recipe_id"] == "videoscope-rescue-clean-av-v1"
        assert entry["source_sha256_record"] == "rescue-source-hashes.json"
        assert entry["expected_damage_kinds"]
        assert entry["damage_tolerance_seconds"] == 1.0
        assert entry["generation"] in {"ffmpeg_filter", "payload_zeroing"}
        acceptance = cast(dict[str, object], entry["acceptance"])
        assert acceptance["outcome_scope"] == "faithful_structural"
        assert acceptance["expected_outcome"] in {
            "completed",
            "needs_review",
            "partial",
        }
        assert acceptance["duration_tolerance_seconds"] == 0.25

    assert rescue["rescue_clean_av.mp4"]["expected_damage_kinds"] == ["decodable"]
    assert rescue["rescue_missing_audio.mp4"]["expected_damage_kinds"] == [
        "missing_stream"
    ]
    missing_audio_acceptance = cast(
        dict[str, object], rescue["rescue_missing_audio.mp4"]["acceptance"]
    )
    assert missing_audio_acceptance["expected_outcome"] == "needs_review"
    assert rescue["rescue_middle_damaged.mp4"]["expected_damage_intervals"] == [
        {"start_seconds": 2.0, "end_seconds": 3.0}
    ]
    assert rescue["rescue_tail_damaged.mp4"]["expected_damage_intervals"] == [
        {"start_seconds": 5.0, "end_seconds": 6.0}
    ]
    assert rescue["rescue_dark_noise.mp4"]["acceptance"] == {
        "outcome_scope": "faithful_structural",
        "expected_outcome": "completed",
        "duration_tolerance_seconds": 0.25,
        "minimum_luma_gain": 0.01,
        "maximum_mean_luma": 0.35,
        "maximum_noise_increase": 0.0,
    }


def test_payload_corruption_streams_packet_spans_without_reading_the_whole_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "原始 视频.mp4"
    destination = tmp_path / "损坏 视频.mp4"
    original = bytes(range(64))
    source.write_bytes(original)
    observed_arguments: list[list[str]] = []

    def fake_packet_lines(
        arguments: list[str],
        *,
        source: Path,
        timeout_seconds: float,
    ) -> Iterator[str]:
        del timeout_seconds
        observed_arguments.append(arguments)
        assert source == tmp_path / "原始 视频.mp4"
        yield "pts_time=1.9|dts_time=1.9|duration_time=0.2|pos=10|size=4"
        yield "pts_time=2.4|dts_time=2.4|duration_time=0.2|pos=20|size=3"
        yield "pts_time=3.0|dts_time=3.0|duration_time=0.2|pos=30|size=4"
        yield "pts_time=2.9|dts_time=2.9|duration_time=0.2|pos=40|size=2"
        yield "pts_time=N/A|dts_time=2.7|duration_time=0.1|pos=50|size=2"

    original_read_bytes = Path.read_bytes

    def reject_whole_source_read(path: Path) -> bytes:
        if path == source:
            raise AssertionError("source must be copied and hashed in bounded chunks")
        return original_read_bytes(path)

    monkeypatch.setattr(
        factory, "_iter_command_stdout_lines", fake_packet_lines, raising=False
    )
    monkeypatch.setattr(Path, "read_bytes", reject_whole_source_read)

    factory._copy_with_zeroed_payload(
        source=source,
        destination=destination,
        expected_range=factory.ExpectedRange(2.0, 3.0),
        ffprobe="fake-ffprobe",
    )

    expected = bytearray(original)
    for start, size in ((10, 4), (20, 3), (40, 2), (50, 2)):
        expected[start : start + size] = b"\x00" * size
    with source.open("rb") as source_file:
        assert source_file.read() == original
    with destination.open("rb") as destination_file:
        assert destination_file.read() == expected
    assert len(observed_arguments) == 1
    assert observed_arguments[0][0] == "fake-ffprobe"
    assert observed_arguments[0][-1] == str(source)


def test_source_hash_record_contains_the_actual_pristine_sha256(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pristine = tmp_path / "rescue_clean_av.mp4"
    payload = (b"pristine-video-bytes" * 1000) + b"tail"
    pristine.write_bytes(payload)
    original_read_bytes = Path.read_bytes

    def reject_whole_source_read(path: Path) -> bytes:
        if path == pristine:
            raise AssertionError("source hash must be computed in bounded chunks")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_source_read)

    recorded = factory._record_rescue_source_hash(
        output_directory=tmp_path,
        pristine_path=pristine,
    )

    expected = hashlib.sha256(payload).hexdigest()
    assert recorded == expected
    record = json.loads(
        (tmp_path / "rescue-source-hashes.json").read_text(encoding="utf-8")
    )
    assert record == {"rescue_clean_av.mp4": expected}


def test_scanner_to_manifest_hook_validates_each_declared_damage_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scanned: list[str] = []
    for filename in ("rescue_middle_damaged.mp4", "rescue_tail_damaged.mp4"):
        (tmp_path / filename).write_bytes(b"fixture")

    class FakeScanner:
        def scan(self, source: Path, **_kwargs: object) -> SimpleNamespace:
            scanned.append(source.name)
            interval = (
                (2.0, 3.0) if source.name == "rescue_middle_damaged.mp4" else (5.0, 6.0)
            )
            return SimpleNamespace(
                intervals=(
                    SimpleNamespace(
                        kind=SimpleNamespace(value="undecodable"),
                        start_seconds=interval[0],
                        end_seconds=interval[1],
                    ),
                )
            )

    monkeypatch.setattr("videoscope.rescue.scanner.RescueScanner", FakeScanner)
    monkeypatch.setattr(
        factory,
        "probe_video",
        lambda **_kwargs: factory.ProbeResult(6.0, 320, 180, 10.0, True),
    )

    factory.validate_rescue_observed_intervals(
        output_directory=tmp_path,
        ffmpeg="fake-ffmpeg",
        ffprobe="fake-ffprobe",
    )

    assert scanned == ["rescue_middle_damaged.mp4", "rescue_tail_damaged.mp4"]


def test_scanner_to_manifest_hook_rejects_an_interval_outside_tolerance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for filename in ("rescue_middle_damaged.mp4", "rescue_tail_damaged.mp4"):
        (tmp_path / filename).write_bytes(b"fixture")

    class FakeScanner:
        def scan(self, source: Path, **_kwargs: object) -> SimpleNamespace:
            del source
            return SimpleNamespace(
                intervals=(
                    SimpleNamespace(
                        kind=SimpleNamespace(value="undecodable"),
                        start_seconds=0.0,
                        end_seconds=1.0,
                    ),
                )
            )

    monkeypatch.setattr("videoscope.rescue.scanner.RescueScanner", FakeScanner)
    monkeypatch.setattr(
        factory,
        "probe_video",
        lambda **_kwargs: factory.ProbeResult(6.0, 320, 180, 10.0, True),
    )

    with pytest.raises(factory.FixtureFactoryError, match="observed interval"):
        factory.validate_rescue_observed_intervals(
            output_directory=tmp_path,
            ffmpeg="fake-ffmpeg",
            ffprobe="fake-ffprobe",
        )


def test_rescue_generation_rejects_a_scanner_manifest_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str, str]] = []

    monkeypatch.setattr(factory, "rescue_fixture_specs", lambda: ())
    monkeypatch.setattr(
        factory,
        "_record_rescue_source_hash",
        lambda **_kwargs: "a" * 64,
    )

    def reject_mismatch(
        *, output_directory: Path, ffmpeg: str, ffprobe: str
    ) -> tuple[str, ...]:
        calls.append((output_directory, ffmpeg, ffprobe))
        raise factory.FixtureFactoryError("scanner observed interval mismatch")

    monkeypatch.setattr(factory, "validate_rescue_observed_intervals", reject_mismatch)

    with pytest.raises(factory.FixtureFactoryError, match="observed interval mismatch"):
        factory.generate_rescue_fixtures(
            output_directory=tmp_path,
            ffmpeg="fake-ffmpeg",
            ffprobe="fake-ffprobe",
            force=True,
        )

    assert calls == [(tmp_path, "fake-ffmpeg", "fake-ffprobe")]


def test_privacy_manifest_records_exact_manual_and_audio_intervals() -> None:
    privacy = cast(
        dict[str, dict[str, object]],
        factory.privacy_manifest_data(),
    )

    manual = privacy["privacy_manual_visual.mp4"]
    assert manual["manual_visual_regions"] == [
        {
            "start_seconds": 0.4,
            "end_seconds": 3.6,
            "box": {"x_min": 0.1, "y_min": 0.15, "x_max": 0.8, "y_max": 0.75},
            "style": "blur",
        }
    ]
    assert manual["visual_story"] == {
        "kind": "moving_face_like_region",
        "visible_intervals": [[0.4, 1.4], [2.0, 3.6]],
        "occluded_intervals": [[1.4, 2.0]],
        "reappears": True,
    }
    assert privacy["privacy_tags_av.mp4"]["manual_audio_intervals"] == [
        {"start_seconds": 1.0, "end_seconds": 2.0, "style": "mute"}
    ]
    qr_risks = cast(list[dict[str, object]], privacy["privacy_qr.mp4"]["risks"])
    assert [risk["motion"] for risk in qr_risks] == [
        "static",
        "moving",
        "scaling",
        "edge_adjacent",
    ]
    assert [(risk["start_seconds"], risk["end_seconds"]) for risk in qr_risks] == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
        (3.0, 4.0),
    ]


def test_privacy_manifest_covers_metadata_and_multilingual_text_semantics() -> None:
    privacy = cast(dict[str, dict[str, object]], factory.privacy_manifest_data())

    metadata = cast(
        dict[str, object],
        privacy["privacy_tags_av.mp4"]["metadata_expectations"],
    )
    assert set(cast(list[str], metadata["scopes"])) == {
        "global",
        "stream",
        "chapter",
        "attachment",
    }
    assert set(cast(list[str], metadata["semantic_fields"])) >= {
        "author",
        "title",
        "device",
        "location",
    }
    assert set(cast(list[str], metadata["keys"])) >= {
        "artist",
        "title",
        "comment",
        "location",
    }
    assert metadata["device_semantic_key"] == "comment"
    assert metadata["attached_picture"] is True

    cases = cast(list[dict[str, object]], privacy["privacy_text.mp4"]["text_cases"])
    assert [case["kind"] for case in cases] == [
        "phone",
        "email",
        "address",
        "account",
        "ordinary",
    ]
    assert {case["language"] for case in cases[:-1]} == {"zh-CN", "en"}
    assert all(case["sensitive"] is True for case in cases[:-1])
    assert cases[-1]["sensitive"] is False


def test_manual_fixture_box_uses_half_open_pixel_bounds(tmp_path: Path) -> None:
    frames = factory.generate_privacy_frames(tmp_path, mode="manual_visual")
    image = Image.open(frames[4]).convert("RGB")

    # The manifest x_max is exclusive: pixel 255 is inside, pixel 256 is outside.
    assert image.getpixel((255, 70)) != image.getpixel((256, 70))


def test_privacy_qr_encoder_failure_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cv2 = importlib.import_module("cv2")

    monkeypatch.delattr(cv2, "QRCodeEncoder_create", raising=False)

    with pytest.raises(factory.FixtureFactoryError, match="OpenCV 4.5 or newer"):
        factory.generate_privacy_frames(Path("unused"), mode="qr")


def test_privacy_qr_encoder_runtime_failure_reports_version_and_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cv2 = importlib.import_module("cv2")

    class BrokenEncoder:
        def encode(self, value: str) -> object:
            del value
            raise RuntimeError("private local detail")

    monkeypatch.setattr(cv2, "QRCodeEncoder_create", BrokenEncoder)

    with pytest.raises(factory.FixtureFactoryError) as error:
        factory.generate_privacy_frames(tmp_path, mode="qr")

    message = str(error.value)
    assert str(getattr(cv2, "__version__")) in message
    assert "upgrade" in message.casefold()
    assert "private local detail" not in message


def test_ffmpeg_command_is_an_argument_array(tmp_path: Path) -> None:
    spec = factory.fixture_specs()[0]
    output_path = tmp_path / "含 空格" / spec.filename

    command = factory.build_ffmpeg_command(
        ffmpeg="ffmpeg",
        spec=spec,
        output_path=output_path,
    )

    assert isinstance(command, list)
    assert command[0] == "ffmpeg"
    assert command[-1] == str(output_path)
    assert "-vf" in command
    assert "-y" in command


def test_program_generated_text_sequences_are_stable_and_change(
    tmp_path: Path,
) -> None:
    stable = factory.generate_text_frames(tmp_path / "stable", mode="stable")
    changing = factory.generate_text_frames(tmp_path / "changing", mode="changing")

    assert len(stable) == len(changing) == 60
    assert stable[10].read_bytes() == stable[30].read_bytes()
    assert changing[10].read_bytes() != changing[30].read_bytes()
    assert changing[20].read_bytes() != changing[22].read_bytes()


def test_run_checked_disables_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args == ["ffmpeg", "-version"]
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="ffmpeg version test",
            stderr="",
        )

    monkeypatch.setattr("scripts.generate_test_videos.subprocess.run", fake_run)

    result = factory.run_checked(["ffmpeg", "-version"])

    assert result.returncode == 0


def test_run_checked_bounds_and_redacts_sensitive_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文 source.mp4"
    staging = tmp_path / "private temp" / "stage.mp4"
    output = tmp_path / "public output.mp4"
    leaked = f"failed at {source} via {staging} -> {output} " + "x" * 5000

    monkeypatch.setattr(
        "scripts.generate_test_videos.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", leaked),
    )

    with pytest.raises(factory.FixtureFactoryError) as error:
        factory.run_checked(
            ["ffmpeg", "-i", str(source), str(output)],
            sensitive_paths=(source, staging, output),
        )

    message = str(error.value)
    assert "ffmpeg exited with status 1" in message
    assert "[local-path]" in message
    assert str(source) not in message
    assert str(staging) not in message
    assert str(output) not in message
    assert len(message) < 2300


def test_force_replaces_old_files_with_mocked_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "中文 fixtures"
    output_directory.mkdir()
    old_path = output_directory / "clean_motion.mp4"
    old_path.write_bytes(b"old")

    def fake_run_checked(
        args: Sequence[str],
        *,
        timeout_seconds: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(args[-1])
        output_path.write_bytes(b"generated")
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
        )

    def fake_validate_video(
        *,
        ffmpeg: str,
        ffprobe: str,
        video_path: Path,
    ) -> factory.ProbeResult:
        assert ffmpeg == "fake-ffmpeg"
        assert ffprobe == "fake-ffprobe"
        assert video_path.read_bytes() == b"generated"
        return factory.ProbeResult(6.0, 320, 180, 10.0)

    monkeypatch.setattr(factory, "run_checked", fake_run_checked)
    monkeypatch.setattr(factory, "validate_video", fake_validate_video)

    generated = factory.generate_fixtures(
        output_directory=output_directory,
        manifest_path=tmp_path / "manifest.json",
        ffmpeg="fake-ffmpeg",
        ffprobe="fake-ffprobe",
        force=True,
    )

    assert {path.name for path in generated} == EXPECTED_FILENAMES
    assert old_path.read_bytes() == b"generated"


def test_missing_ffmpeg_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "scripts.generate_test_videos.shutil.which",
        lambda name: None,
    )

    exit_code = factory.main(["--force"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Missing required system executable" in captured.err
    assert "ffmpeg" in captured.err
    assert "ffprobe" in captured.err


def test_real_ffmpeg_factory_when_available(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for the integration fixture test")
    assert ffmpeg is not None
    assert ffprobe is not None

    generated = factory.generate_fixtures(
        output_directory=tmp_path / "generated videos",
        manifest_path=tmp_path / "manifest.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )

    assert {path.name for path in generated} == EXPECTED_FILENAMES
    assert all(path.stat().st_size > 0 for path in generated)


def test_real_privacy_factory_is_repeatable_when_available(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for privacy fixture generation")
    assert ffmpeg is not None
    assert ffprobe is not None
    output = tmp_path / "隐私 fixtures"

    first = factory.generate_privacy_fixtures(
        output_directory=output,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )
    first_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first
    }
    second = factory.generate_privacy_fixtures(
        output_directory=output,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )
    second_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in second
    }

    assert set(first_hashes) == EXPECTED_PRIVACY_FILENAMES
    assert second_hashes == first_hashes


def test_real_rescue_scanner_matches_manifest_when_tools_are_available(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip(
            "FFmpeg and ffprobe are required for Rescue scanner/manifest validation"
        )
    assert ffmpeg is not None
    assert ffprobe is not None
    output = tmp_path / "救援 fixtures"

    generated = factory.generate_rescue_fixtures(
        output_directory=output,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )

    assert {path.name for path in generated} == EXPECTED_GENERATED_RESCUE_FILENAMES
    source_hashes = json.loads(
        (output / "rescue-source-hashes.json").read_text(encoding="utf-8")
    )
    pristine = output / "rescue_clean_av.mp4"
    assert source_hashes == {
        pristine.name: hashlib.sha256(pristine.read_bytes()).hexdigest()
    }
    assert set(
        factory.validate_rescue_observed_intervals(
            output_directory=output,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
    ) == {"rescue_middle_damaged.mp4", "rescue_tail_damaged.mp4"}
