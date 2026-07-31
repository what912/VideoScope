"""Determinism and manifest-scope tests for benchmark orchestration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from videoscope.analysis import AnalysisConfig
from videoscope.benchmarking import (
    BenchmarkProfile,
    BenchmarkReport,
    BenchmarkRunner,
)
from videoscope.domain import (
    AnalysisReport,
    DetectorExecution,
    DetectorStatus,
    Evidence,
    Finding,
    Severity,
    TimeRange,
    VideoMetadata,
    make_finding_id,
)

INPUT_HASH = "ab" * 32


class FixedRunClock:
    """Return the same one-second run envelope for each runner."""

    def __init__(self) -> None:
        self.values = iter((10.0, 11.0))

    def __call__(self) -> float:
        return next(self.values)


def _write_manifest(root: Path) -> Path:
    generated = root / "generated"
    generated.mkdir(parents=True)
    (generated / "black.mp4").write_bytes(b"black")
    (generated / "clean.mp4").write_bytes(b"clean")
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "videos": {
                    "black.mp4": {
                        "duration_seconds": 5.0,
                        "expected_anomaly_type": "black_segment",
                        "expected_time_ranges": [
                            {"start_seconds": 2.0, "end_seconds": 3.0}
                        ],
                        "tolerance_seconds": 0.1,
                    },
                    "clean.mp4": {
                        "duration_seconds": 5.0,
                        "expected_anomaly_type": "none",
                        "expected_time_ranges": [],
                        "tolerance_seconds": 0.1,
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest


def _analyze(input_path: Path, config: AnalysisConfig) -> AnalysisReport:
    detector_ids = tuple(config.enabled_detectors or ())
    findings: list[Finding] = []
    if input_path.name == "black.mp4":
        time_range = TimeRange(start_seconds=2.0, end_seconds=3.0)
        findings.append(
            Finding(
                id=make_finding_id(
                    input_hash=INPUT_HASH,
                    detector_id="near_black",
                    time_range=time_range,
                ),
                detector_id="near_black",
                detector_version="1.0.0",
                title="Near-black interval detected",
                description="Observed low luma.",
                severity=Severity.MEDIUM,
                score=0.8,
                confidence=0.9,
                time_range=time_range,
                evidence=[
                    Evidence(
                        evidence_type="sampled_frame",
                        timestamp_seconds=2.0,
                        description="Observed frame.",
                    )
                ],
                limitations=["May be intentional."],
            )
        )
    return AnalysisReport(
        tool_version="0.1.0",
        analysis_id="ignored-by-benchmark",
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        input_hash=INPUT_HASH,
        metadata=VideoMetadata(
            filename=input_path.name,
            container_format="mp4",
            codec="test",
            width=32,
            height=18,
            duration_seconds=5.0,
            average_frame_rate=10,
            estimated_frame_count=50,
            has_audio=False,
            file_size_bytes=input_path.stat().st_size,
        ),
        detector_executions=[
            DetectorExecution(
                detector_id=detector_id,
                status=DetectorStatus.OK,
                elapsed_seconds=0,
                findings_count=sum(
                    finding.detector_id == detector_id for finding in findings
                ),
            )
            for detector_id in detector_ids
        ],
        findings=findings,
    )


def _run(root: Path, manifest: Path) -> BenchmarkReport:
    profiles = [
        BenchmarkProfile(label="default", config=AnalysisConfig()),
        BenchmarkProfile(
            label="variant",
            config=AnalysisConfig(
                detector_configurations={"near_black": {"mean_luma_threshold": 0.07}}
            ),
        ),
    ]
    return BenchmarkRunner(
        analyzer=_analyze,
        clock=FixedRunClock(),
        ffmpeg_version="ffmpeg test",
    ).run(
        manifest,
        output_directory=root,
        profiles=profiles,
        detector_ids=("near_black",),
    )


def test_benchmark_result_is_deterministic_and_per_detector(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path / "dataset")

    first = _run(tmp_path / "first", manifest)
    second = _run(tmp_path / "second", manifest)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.profiles) == 2
    for profile in first.profiles:
        assert len(profile.detectors) == 1
        detector = profile.detectors[0]
        assert detector.detector_id == "near_black"
        assert detector.metrics.event_f1 == 1
        assert detector.metrics.temporal_iou == 1
        assert detector.negative_case_count == 1
        assert detector.negative_false_positive_event_count == 0
    assert (tmp_path / "first" / "benchmark.json").is_file()


def test_legacy_manifest_excludes_other_unannotated_detectors(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path / "dataset")
    report = BenchmarkRunner(
        analyzer=_analyze,
        clock=FixedRunClock(),
        ffmpeg_version="ffmpeg test",
    ).run(
        manifest,
        output_directory=tmp_path / "result",
        profiles=[BenchmarkProfile(label="default", config=AnalysisConfig())],
        detector_ids=("near_black", "possible_freeze"),
    )

    freeze = next(
        item
        for item in report.profiles[0].detectors
        if item.detector_id == "possible_freeze"
    )
    assert freeze.evaluated_case_count == 1
    assert freeze.excluded_unannotated_case_count == 1
    assert freeze.negative_case_count == 1


def test_benchmark_supports_chinese_manifest_video_and_output_paths(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "人工 标注集"
    videos = dataset / "本地 视频"
    videos.mkdir(parents=True)
    video_name = "干净 样例.mp4"
    (videos / video_name).write_bytes(b"clean")
    manifest = dataset / "清单.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "video_root": "本地 视频",
                "videos": {
                    video_name: {
                        "duration_seconds": 5.0,
                        "expected_anomaly_type": "none",
                        "expected_time_ranges": [],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = BenchmarkRunner(
        analyzer=_analyze,
        clock=FixedRunClock(),
        ffmpeg_version="ffmpeg test",
    ).run(
        manifest,
        output_directory=tmp_path / "评测 输出",
        profiles=[BenchmarkProfile(label="默认 配置", config=AnalysisConfig())],
        detector_ids=("near_black",),
    )

    assert report.profiles[0].detectors[0].negative_case_count == 1
    content = (tmp_path / "评测 输出" / "benchmark.json").read_text(encoding="utf-8")
    assert video_name in content
    assert str(tmp_path) not in content
