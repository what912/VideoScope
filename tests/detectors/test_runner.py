"""Tests for sequential and failure-isolated detector execution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from videoscope.detectors import (
    AnalysisContext,
    DetectorRegistry,
    DetectorRunner,
)
from videoscope.domain import (
    DetectorStatus,
    Finding,
    VideoMetadata,
)
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

from .dummy import DummyDetector

INPUT_HASH = "ab" * 32


def make_context(tmp_path: Path) -> AnalysisContext:
    """Create a complete context with Unicode and spaced paths."""
    input_path = tmp_path / "输入 视频.mp4"
    input_path.write_bytes(b"video")
    workspace = tmp_path / "工作 空间"
    workspace.mkdir(exist_ok=True)
    return AnalysisContext(
        input_path=input_path,
        input_hash=INPUT_HASH,
        metadata=VideoMetadata(
            filename=input_path.name,
            container_format="mp4",
            codec="h264",
            width=320,
            height=180,
            duration_seconds=6.0,
            average_frame_rate=10.0,
            estimated_frame_count=60,
            has_audio=False,
            file_size_bytes=input_path.stat().st_size,
        ),
        prompt=None,
        frame_samples=(
            FrameSample(
                timestamp_seconds=1.0,
                sample_index=0,
                relative_path="frames/帧 000.jpg",
                width=320,
                height=180,
            ),
        ),
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=6.0,
                duration_seconds=6.0,
                representative_timestamp=3.0,
            ),
        ),
        workspace=workspace,
        shared_cache={"feature": [1, 2, 3]},
    )


class FailingDetector(DummyDetector):
    """Detector double which leaks data in its exception for sanitizer tests."""

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        raise RuntimeError(
            f"failed at {context.input_path}; workspace={context.workspace}; "
            f"prompt={context.prompt}; api_key=private-value"
        )


class KeyboardInterruptDetector(DummyDetector):
    """Detector double which requests process interruption."""

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        raise KeyboardInterrupt


class SystemExitDetector(DummyDetector):
    """Detector double which requests process exit."""

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        raise SystemExit(2)


def test_runner_records_normal_detector(tmp_path: Path) -> None:
    registry = DetectorRegistry([DummyDetector()])

    result = DetectorRunner(registry).run(make_context(tmp_path))

    assert len(result.findings) == 1
    assert result.executions[0].status is DetectorStatus.OK
    assert result.executions[0].findings_count == 1
    assert result.executions[0].elapsed_seconds >= 0


def test_runner_reports_current_detector_without_changing_result(
    tmp_path: Path,
) -> None:
    messages: list[str] = []
    registry = DetectorRegistry([DummyDetector()])

    result = DetectorRunner(registry, progress=messages.append).run(
        make_context(tmp_path)
    )

    assert messages == ["Running detector: test.dummy"]
    assert len(result.findings) == 1


def test_runner_records_valid_empty_result(tmp_path: Path) -> None:
    registry = DetectorRegistry([DummyDetector()])

    result = DetectorRunner(registry).run(
        make_context(tmp_path),
        configurations={"test.dummy": {"emit_finding": False}},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.OK
    assert result.executions[0].findings_count == 0


def test_invalid_config_is_isolated_as_detector_error(tmp_path: Path) -> None:
    registry = DetectorRegistry([DummyDetector()])

    result = DetectorRunner(registry).run(
        make_context(tmp_path),
        configurations={"test.dummy": {"score": 2.0}},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.DETECTOR_ERROR
    assert result.executions[0].error_type == "ValidationError"


def test_one_detector_failure_does_not_stop_another(tmp_path: Path) -> None:
    failing = FailingDetector("test.a_failure")
    succeeding = DummyDetector("test.b_success")
    registry = DetectorRegistry([succeeding, failing])
    context = make_context(tmp_path).model_copy(
        update={"prompt": "private prompt text"}
    )

    result = DetectorRunner(registry).run(context)

    assert [execution.detector_id for execution in result.executions] == [
        "test.a_failure",
        "test.b_success",
    ]
    assert [execution.status for execution in result.executions] == [
        DetectorStatus.DETECTOR_ERROR,
        DetectorStatus.OK,
    ]
    assert len(result.findings) == 1
    assert result.findings[0].detector_id == "test.b_success"
    error_message = result.executions[0].error_message
    assert error_message is not None
    assert str(context.input_path) not in error_message
    assert str(context.workspace) not in error_message
    assert "private-value" not in error_message
    assert "private prompt text" not in error_message
    assert "prompt=<prompt>" in error_message
    assert "api_key=<redacted>" in error_message


def test_runner_uses_only_default_enabled_detectors_by_default(
    tmp_path: Path,
) -> None:
    enabled = DummyDetector("test.enabled")
    disabled = DummyDetector("test.disabled", default_enabled=False)
    registry = DetectorRegistry([disabled, enabled])

    result = DetectorRunner(registry).run(make_context(tmp_path))

    assert [execution.detector_id for execution in result.executions] == [
        "test.enabled"
    ]


def test_findings_are_sorted_independently_of_execution_order(
    tmp_path: Path,
) -> None:
    detector_z = DummyDetector("test.z")
    detector_a = DummyDetector("test.a")
    registry = DetectorRegistry([detector_z, detector_a])

    result = DetectorRunner(registry).run(
        make_context(tmp_path),
        detector_ids=["test.z", "test.a"],
        configurations={
            "test.z": {"start_seconds": 2.0, "end_seconds": 3.0},
            "test.a": {"start_seconds": 1.0, "end_seconds": 2.0},
        },
    )
    repeated = DetectorRunner(registry).run(
        make_context(tmp_path),
        detector_ids=["test.a", "test.z"],
        configurations={
            "test.z": {"start_seconds": 2.0, "end_seconds": 3.0},
            "test.a": {"start_seconds": 1.0, "end_seconds": 2.0},
        },
    )

    assert [finding.detector_id for finding in result.findings] == [
        "test.a",
        "test.z",
    ]
    assert result.findings == repeated.findings


def test_analysis_context_exposes_optional_cancellation_callback(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path).model_copy(
        update={"cancellation_callback": lambda: True}
    )

    assert context.is_cancelled() is True


def test_runner_does_not_catch_keyboard_interrupt(tmp_path: Path) -> None:
    registry = DetectorRegistry([KeyboardInterruptDetector()])

    with pytest.raises(KeyboardInterrupt):
        DetectorRunner(registry).run(make_context(tmp_path))


def test_runner_does_not_catch_system_exit(tmp_path: Path) -> None:
    registry = DetectorRegistry([SystemExitDetector()])

    with pytest.raises(SystemExit):
        DetectorRunner(registry).run(make_context(tmp_path))
