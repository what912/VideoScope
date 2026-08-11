from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

import scripts.generate_growth_cases as growth_case_generator
from scripts.generate_growth_cases import (
    CASE_SPECS,
    PUBLIC_REPORT_KEYS,
    CaseGenerationError,
    CommandResult,
    CompletedCase,
    extract_comparison_assets,
    generate_case_sources,
    write_public_case_record,
)
from videoscope.rescue.models import RescueSymptom


class FakeRunner:
    """Record the complete shell boundary and materialize requested outputs."""

    def __init__(self, calls: list[tuple[list[str], bool]]) -> None:
        self.calls = calls

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        shell: bool,
    ) -> CommandResult:
        arguments = list(argv)
        self.calls.append((arguments, shell))
        assert 0 < timeout_seconds <= 300
        output = Path(arguments[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((output.name + "\n").encode("utf-8"))
        return CommandResult(returncode=0, stderr="")


def _base_record() -> dict[str, Any]:
    return {
        "id": "case-fixture-001",
        "slug": "case-fixture",
        "featured": True,
        "provenance": "project-authored",
        "authorizationSummary": {
            "en": "Project-authored procedural media.",
            "zh-CN": "项目原创程序化媒体。",
        },
        "title": {"en": "Fixture case", "zh-CN": "测试案例"},
        "summary": {
            "en": "A bounded local comparison.",
            "zh-CN": "一个有界的本地对比。",
        },
        "observableSymptom": {
            "en": "A visible engineering condition is present.",
            "zh-CN": "存在可见的工程条件。",
        },
        "actions": [
            {
                "workflow": "video-rescue",
                "actionId": "fixture-action",
                "version": "0.2",
                "kind": "remux",
                "description": {
                    "en": "Repackages the verified source.",
                    "zh-CN": "重新封装已验证的来源。",
                },
                "parameters": {"sourceReadOnly": True},
            }
        ],
        "unresolved": [
            {
                "en": "The case is procedural, not user footage.",
                "zh-CN": "本案例为程序化素材，并非用户视频。",
            }
        ],
        "limitations": [
            {
                "en": "This demonstrates one controlled condition.",
                "zh-CN": "本案例仅演示一种受控条件。",
            }
        ],
        "comparison": {"startSeconds": 3, "endSeconds": 8},
        "media": {
            "durationSeconds": 5,
            "width": 320,
            "height": 180,
            "frameRate": 24,
        },
        "versions": {
            "videoscope": "0.8.0",
            "ffmpeg": "fixture",
            "platform": "fixture",
            "configuration": "fixture",
        },
        "verification": {
            "status": "completed",
            "checks": [
                {
                    "checkId": "decode",
                    "status": "passed",
                    "summary": {
                        "en": "Both clips decode.",
                        "zh-CN": "两个片段均可解码。",
                    },
                    "measured": {"decode": True},
                }
            ],
        },
        "reproduction": ["python scripts/generate_growth_cases.py --force"],
    }


def _completed_case(tmp_path: Path) -> CompletedCase:
    case_directory = tmp_path / "site" / "public" / "cases" / "case-fixture"
    case_directory.mkdir(parents=True)
    for name, contents in {
        "before.mp4": b"before-video-bytes",
        "after.mp4": b"after-video-bytes",
        "poster.webp": b"poster-bytes",
    }.items():
        (case_directory / name).write_bytes(contents)
    manifest_path = tmp_path / "site" / "src" / "data" / "case-studies.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        '{"schemaVersion":1,"generatedBy":"scripts/generate_growth_cases.py","cases":[]}\n',
        encoding="utf-8",
    )
    return CompletedCase(
        case_id="case-fixture-001",
        slug="case-fixture",
        status="completed",
        case_directory=case_directory,
        manifest_path=manifest_path,
        record=_base_record(),
        public_report={
            "case_id": "case-fixture-001",
            "schema_version": "1.0",
            "actions": _base_record()["actions"],
            "comparison": {"startSeconds": 3, "endSeconds": 8},
            "verification": _base_record()["verification"],
            "limitations": _base_record()["limitations"],
            "versions": _base_record()["versions"],
            "output_sha256": {},
        },
    )


def test_ffmpeg_commands_are_argument_arrays_and_never_shell(tmp_path: Path) -> None:
    calls: list[tuple[list[str], bool]] = []
    runner = FakeRunner(calls=calls)

    sources = generate_case_sources(tmp_path, runner=runner)

    assert {path.name for path in sources.values()} == {
        "timeline-rescue-source.mkv",
        "measured-improvement-source.mp4",
        "no-crop-vertical-source.mp4",
    }
    assert calls
    assert all(isinstance(argv, list) and shell is False for argv, shell in calls)
    assert all(
        Path(argv[-1]).resolve().is_relative_to(tmp_path.resolve()) for argv, _ in calls
    )


def test_timeline_source_uses_a_nonzero_offset_within_duration_tolerance(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    generate_case_sources(tmp_path, runner=FakeRunner(calls=calls))

    timeline = next(argv for argv, _ in calls if argv[-1].endswith(".mkv"))
    offset = float(timeline[timeline.index("-output_ts_offset") + 1])
    assert 0 < offset <= 0.15


def test_timeline_source_requests_bitexact_container_muxing(tmp_path: Path) -> None:
    calls: list[tuple[list[str], bool]] = []

    generate_case_sources(tmp_path, runner=FakeRunner(calls=calls))

    timeline = next(argv for argv, _ in calls if argv[-1].endswith(".mkv"))
    assert timeline[timeline.index("-fflags") : timeline.index("-fflags") + 2] == [
        "-fflags",
        "+bitexact",
    ]


def test_measured_source_avoids_sampling_alias_and_unsupported_audio_hint(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    generate_case_sources(tmp_path, runner=FakeRunner(calls=calls))

    measured = next(
        argv
        for argv, _ in calls
        if argv[-1].endswith("measured-improvement-source.mp4")
    )
    filter_graph = measured[measured.index("-filter_complex") + 1]
    assert any(value.startswith("color=") for value in measured)
    assert any("color=c=0x526277" in value for value in measured)
    assert "drawgrid=width=32:height=24" in filter_graph
    assert "color=white@0.65" in filter_graph
    assert filter_graph.count("drawbox=") >= 2
    assert "t*260" in filter_graph and "t*190" in filter_graph
    assert "floor(n/5)" in filter_graph
    assert "0.05,-0.08" in filter_graph
    assert "anoisesrc" in " ".join(measured)
    measured_spec = next(
        spec for spec in CASE_SPECS if spec.slug == "measured-improvement"
    )
    assert RescueSymptom.AUDIO_NOISE not in measured_spec.rescue_symptoms


def test_comparison_clips_use_the_identical_source_range(tmp_path: Path) -> None:
    calls: list[tuple[list[str], bool]] = []
    runner = FakeRunner(calls=calls)
    before_source = tmp_path / "before-source.mp4"
    after_source = tmp_path / "after-source.mp4"
    before_source.write_bytes(b"before")
    after_source.write_bytes(b"after")

    assets = extract_comparison_assets(
        before_source,
        after_source,
        comparison={"startSeconds": 3, "endSeconds": 8},
        destination=tmp_path / "public-case",
        runner=runner,
    )

    assert {path.name for path in assets.values()} == {
        "before.mp4",
        "after.mp4",
        "poster.webp",
    }
    before_argv, after_argv = calls[0][0], calls[1][0]
    assert before_argv[before_argv.index("-ss") : before_argv.index("-ss") + 4] == [
        "-ss",
        "3",
        "-t",
        "5",
    ]
    assert after_argv[after_argv.index("-ss") : after_argv.index("-ss") + 4] == [
        "-ss",
        "3",
        "-t",
        "5",
    ]


def test_public_comparison_clips_apply_the_declared_media_budget(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], bool]] = []
    before_source = tmp_path / "before-source.mp4"
    after_source = tmp_path / "after-source.mp4"
    before_source.write_bytes(b"before")
    after_source.write_bytes(b"after")

    extract_comparison_assets(
        before_source,
        after_source,
        comparison={"startSeconds": 3, "endSeconds": 8},
        destination=tmp_path / "public-case",
        runner=FakeRunner(calls=calls),
    )

    for argv, _ in calls[:2]:
        scale_filter = argv[argv.index("-vf") + 1]
        assert "min(1280,iw)" in scale_filter
        assert "min(720,ih)" in scale_filter
        assert "force_original_aspect_ratio=decrease" in scale_filter
        assert "force_divisible_by=2" in scale_filter


def test_comparison_duration_accepts_one_frame_of_container_rounding() -> None:
    assert growth_case_generator._comparison_durations_match(6.0, 6.041, 24.0)
    assert not growth_case_generator._comparison_durations_match(6.0, 6.1, 24.0)


def test_public_video_dimensions_enforce_budget_and_optional_aspect_ratio() -> None:
    assert growth_case_generator._public_video_dimensions_pass(640, 360)
    assert growth_case_generator._public_video_dimensions_pass(
        404, 720, expected_aspect_ratio=9 / 16
    )
    assert not growth_case_generator._public_video_dimensions_pass(1080, 1920)
    assert not growth_case_generator._public_video_dimensions_pass(1282, 720)
    assert not growth_case_generator._public_video_dimensions_pass(
        640, 360, expected_aspect_ratio=9 / 16
    )


def test_vertical_poster_measurement_requires_all_four_retained_edge_markers(
    tmp_path: Path,
) -> None:
    poster = tmp_path / "vertical.webp"
    image = Image.new("RGB", (1080, 1920), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 656, 40, 1263), fill=(239, 68, 68))
    draw.rectangle((1039, 656, 1079, 1263), fill=(59, 130, 246))
    draw.rectangle((0, 656, 1079, 685), fill=(245, 158, 11))
    draw.rectangle((0, 1234, 1079, 1263), fill=(168, 85, 247))
    image.save(poster, format="WEBP", lossless=True)

    measured = growth_case_generator._measure_vertical_edge_markers(poster)

    assert measured == {
        "leftEdgeMarkerVisible": True,
        "rightEdgeMarkerVisible": True,
        "topEdgeMarkerVisible": True,
        "bottomEdgeMarkerVisible": True,
    }
    Image.new("RGB", (1080, 1920), "black").save(poster, format="WEBP", lossless=True)
    assert not all(
        growth_case_generator._measure_vertical_edge_markers(poster).values()
    )


def test_manifest_update_uses_relative_paths_and_real_hashes(tmp_path: Path) -> None:
    completed = _completed_case(tmp_path)

    summary = write_public_case_record(completed)

    assert summary.status == "completed"
    assert len(summary.sha256["afterVideo"]) == 64
    assert (
        summary.sha256["afterVideo"] == hashlib.sha256(b"after-video-bytes").hexdigest()
    )
    assert all("Users" not in value for value in summary.public_json_strings)
    manifest = json.loads(completed.manifest_path.read_text(encoding="utf-8"))
    record = manifest["cases"][0]
    assert record["assets"] == {
        "beforeVideo": "/VideoScope/cases/case-fixture/before.mp4",
        "afterVideo": "/VideoScope/cases/case-fixture/after.mp4",
        "poster": "/VideoScope/cases/case-fixture/poster.webp",
        "publicReport": "/VideoScope/cases/case-fixture/public-report.json",
    }
    report = json.loads(
        (completed.case_directory / "public-report.json").read_text(encoding="utf-8")
    )
    assert set(report) == PUBLIC_REPORT_KEYS
    assert report["output_sha256"]["afterVideo"] == summary.sha256["afterVideo"]


def test_public_report_rejects_fields_outside_the_allowlist(tmp_path: Path) -> None:
    completed = _completed_case(tmp_path)
    completed.public_report["working_directory"] = str(tmp_path)

    with pytest.raises(CaseGenerationError, match="allowlist"):
        write_public_case_record(completed)


def test_public_action_prefers_the_pipeline_action_version() -> None:
    class VersionedAction:
        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {
                "id": "rescue-action",
                "version": "1",
                "kind": "remux",
                "description": "Repackage streams.",
                "parameters": {},
            }

    action = growth_case_generator._public_action(
        "video-rescue", VersionedAction(), version="0.2"
    )

    assert action["version"] == "1"


def test_script_execution_imports_videoscope_from_the_current_checkout() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = repository_root / "scripts" / "generate_growth_cases.py"
    code = (
        "import inspect, runpy; "
        f"namespace = runpy.run_path({str(script)!r}, run_name='growth_case_test'); "
        "print(inspect.getfile(namespace['VideoRescuePipeline']))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    imported_path = Path(completed.stdout.strip()).resolve()
    assert imported_path.is_relative_to((repository_root / "src").resolve()), (
        inspect.cleandoc(
            f"""
            The generator imported VideoScope from {imported_path}, not the current
            checkout at {repository_root / "src"}.
            """
        )
    )
