"""Tests for deterministic evidence artifact management."""

from __future__ import annotations

from pathlib import Path

from videoscope.analysis import EvidenceManager
from videoscope.domain import Evidence, Finding, Severity, TimeRange, make_finding_id
from videoscope.video import FrameSample


def test_evidence_manager_selects_front_middle_end_and_hides_input_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "工作 space"
    frames = workspace / "frames"
    frames.mkdir(parents=True)
    samples: list[FrameSample] = []
    for index in range(5):
        relative_path = Path("frames") / f"sample_{index}.jpg"
        (workspace / relative_path).write_bytes(bytes([index]))
        samples.append(
            FrameSample(
                timestamp_seconds=float(index),
                sample_index=index,
                relative_path=relative_path.as_posix(),
                width=1,
                height=1,
            )
        )
    time_range = TimeRange(start_seconds=0.0, end_seconds=4.0)
    finding = Finding(
        id=make_finding_id(
            input_hash="a" * 64,
            detector_id="test.detector",
            time_range=time_range,
        ),
        detector_id="test.detector",
        detector_version="1.0",
        title="Observation",
        description="Observable test interval.",
        severity=Severity.LOW,
        score=0.5,
        confidence=0.5,
        time_range=time_range,
        evidence=[
            Evidence(
                evidence_type="sampled_frame",
                timestamp_seconds=0.0,
                relative_path="frames/sample_0.jpg",
                description="Raw detector evidence.",
            )
        ],
    )

    materialized = EvidenceManager(
        workspace=workspace,
        output_directory=tmp_path / "output",
        frame_samples=tuple(samples),
    ).materialize((finding,))

    evidence = materialized[0].evidence
    assert [item.timestamp_seconds for item in evidence] == [0.0, 2.0, 4.0]
    assert all(item.relative_path is not None for item in evidence)
    assert all("private-video" not in (item.relative_path or "") for item in evidence)
    assert all(
        (tmp_path / "output" / (item.relative_path or "")).is_file()
        for item in evidence
    )
