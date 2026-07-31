"""End-to-end pipeline tests using local deterministic media doubles."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from tests.detectors.dummy import DummyDetector
from videoscope.ai import (
    DevicePreference,
    EmbeddingCache,
    FakeEmbeddingProvider,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    ModelSpec,
)
from videoscope.analysis import (
    AnalysisCancelledError,
    AnalysisConfig,
    AnalysisInternalError,
    AnalysisPipeline,
    AnalysisProcessingError,
)
from videoscope.detectors import (
    AnalysisContext,
    DetectorRegistry,
    DetectorRequirements,
    EstimatedCost,
    PromptAlignmentDetector,
)
from videoscope.domain import DetectorStatus, Finding, read_report_json
from videoscope.video import VideoDecodeError

from .helpers import FakeMedia, FixedSceneDetector, TickClock


class EmptyDetectorConfig(BaseModel):
    """No-op strict config for injected failure detectors."""


class FailingDetector:
    """Fail without preventing another detector from producing a report."""

    id = "test.failure"
    display_name = "Failing detector"
    version = "1.0.0"
    description = "Injected failure for pipeline tests."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = EmptyDetectorConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        del context, config
        raise RuntimeError("injected detector failure")


class InterruptingDetector:
    """Simulate an explicit user interruption."""

    id = "test.interrupt"
    display_name = "Interrupting detector"
    version = "1.0.0"
    description = "Injected interruption for pipeline tests."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = EmptyDetectorConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        del context, config
        raise KeyboardInterrupt


class FailingHTMLRenderer:
    """Inject a renderer error after report construction."""

    def render(self, *args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise RuntimeError("injected HTML failure")


def _pipeline(
    config: AnalysisConfig,
    media: FakeMedia,
    *,
    registry: DetectorRegistry | None = None,
    model_runtime: ModelRuntimeManager | None = None,
    cancellation_callback: Callable[[], bool] | None = None,
) -> AnalysisPipeline:
    return AnalysisPipeline(
        config,
        registry=registry or DetectorRegistry([DummyDetector()]),
        scene_detector=FixedSceneDetector(),
        hash_function=lambda path: "a" * 64,
        probe_function=media.probe,
        sample_function=media.sample,
        detector_clock=TickClock(),
        ffmpeg="unused",
        ffprobe="unused",
        model_runtime=model_runtime,
        cancellation_callback=cancellation_callback,
    )


def _fake_prompt_runtime(tmp_path: Path) -> ModelRuntimeManager:
    config = ModelRuntimeConfig(
        device=DevicePreference.CPU,
        disk_cache_directory=tmp_path / "prompt embedding cache",
    )
    runtime = ModelRuntimeManager(
        config,
        cache=EmbeddingCache(
            memory_budget_bytes=config.memory_budget_bytes,
            disk_directory=config.disk_cache_directory,
        ),
        cuda_available=lambda: False,
    )
    runtime.register(
        ModelSpec(
            provider_id="fake",
            model_id="pipeline-prompt-v1",
            preprocessing_version="test-images-v1",
        ),
        lambda device, precision: FakeEmbeddingProvider(
            device,
            precision,
            model_id="pipeline-prompt-v1",
        ),
    )
    return runtime


def test_pipeline_writes_report_evidence_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "中文 输入 video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "自定义 output"
    media = FakeMedia()

    result = _pipeline(
        AnalysisConfig(output_directory=output),
        media,
    ).run(input_path, prompt="本地提示")

    assert result.report_path == output / "report.json"
    assert result.report_path.is_file()
    assert result.html_report_path == output / "report.html"
    assert result.html_report_path.is_file()
    assert result.bundled_video_path is None
    assert result.workspace_directory is None
    assert all(not workspace.exists() for workspace in media.workspace_parents)
    assert len(result.report.findings) == 1
    assert all(
        evidence.relative_path is not None
        and evidence.relative_path.startswith("evidence/")
        for evidence in result.report.findings[0].evidence
    )
    assert all(
        input_path.name not in evidence.relative_path
        for evidence in result.report.findings[0].evidence
        if evidence.relative_path is not None
    )
    content = result.report_path.read_text(encoding="utf-8")
    assert str(media.workspace_parents[0]) not in content
    assert result.report.configuration["output_directory"] == "."
    assert read_report_json(result.report_path) == result.report


def test_json_only_skips_html_report(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "json-only"

    result = _pipeline(
        AnalysisConfig(output_directory=output, json_only=True),
        FakeMedia(),
    ).run(input_path)

    assert result.report_path.is_file()
    assert result.html_report_path is None
    assert not (output / "report.html").exists()


def test_bundle_video_uses_private_neutral_filename(tmp_path: Path) -> None:
    input_path = tmp_path / "私人 名称.mp4"
    input_path.write_bytes(b"source-video")
    output = tmp_path / "bundle"

    result = _pipeline(
        AnalysisConfig(output_directory=output, bundle_video=True),
        FakeMedia(),
    ).run(input_path)

    assert result.bundled_video_path == output / "media" / "bundled-video.mp4"
    assert result.bundled_video_path.read_bytes() == b"source-video"
    assert result.html_report_path is not None
    html = result.html_report_path.read_text(encoding="utf-8")
    assert 'src="media/bundled-video.mp4"' in html
    assert f"media/{input_path.name}" not in html


def test_detector_failure_still_generates_report(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "run"
    registry = DetectorRegistry(
        [
            FailingDetector(),
            DummyDetector("test.success"),
        ]
    )

    result = _pipeline(
        AnalysisConfig(output_directory=output),
        FakeMedia(),
        registry=registry,
    ).run(input_path)

    assert result.report_path.is_file()
    assert [item.status for item in result.report.detector_executions] == [
        DetectorStatus.DETECTOR_ERROR,
        DetectorStatus.OK,
    ]
    assert len(result.report.findings) == 1
    assert result.report.findings[0].detector_id == "test.success"


def test_descriptive_prompt_alignment_is_persisted_in_report_runtime(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "prompt video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "prompt-report"
    runtime = _fake_prompt_runtime(tmp_path)
    config = AnalysisConfig(
        output_directory=output,
        json_only=True,
        enabled_detectors=("prompt_alignment",),
        detector_configurations={
            "prompt_alignment": {
                "mode": "descriptive",
                "representative_frames_per_scene": 3,
                "provider_id": "fake",
                "model_id": "pipeline-prompt-v1",
                "preprocessing_version": "test-images-v1",
            }
        },
    )

    result = _pipeline(
        config,
        FakeMedia(),
        registry=DetectorRegistry([PromptAlignmentDetector()]),
        model_runtime=runtime,
    ).run(input_path, prompt="a locally analyzed prompt")

    assert result.report.findings == []
    assert result.report.detector_executions[0].status is DetectorStatus.OK
    diagnostics = cast(
        dict[str, Any],
        result.report.runtime["detector_diagnostics"],
    )
    prompt_diagnostics = cast(dict[str, Any], diagnostics["prompt_alignment"])
    assert prompt_diagnostics["mode"] == "descriptive"
    assert len(prompt_diagnostics["scenes"]) == 1
    assert prompt_diagnostics["lowest_scene"]["scene_index"] == 0
    model_runs = cast(list[object], result.report.runtime["model_runs"])
    assert len(model_runs) == 2
    report_text = result.report_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in report_text


def test_html_failure_preserves_json_and_evidence(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "html-failed"
    pipeline = _pipeline(
        AnalysisConfig(output_directory=output),
        FakeMedia(),
    )
    pipeline.html_renderer = cast(Any, FailingHTMLRenderer())

    with pytest.raises(AnalysisInternalError, match="report.json was preserved"):
        pipeline.run(input_path)

    report = read_report_json(output / "report.json")
    assert report.findings
    assert any("HTML report rendering failed" in item for item in report.warnings)
    assert (output / "evidence").is_dir()
    assert not (output / "report.html").exists()


def test_keep_workspace_preserves_extracted_frames(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = _pipeline(
        AnalysisConfig(
            output_directory=tmp_path / "run",
            keep_workspace=True,
        ),
        FakeMedia(),
    ).run(input_path)

    assert result.workspace_directory is not None
    assert result.workspace_directory.is_dir()
    report_content = result.report_path.read_text(encoding="utf-8")
    assert str(result.workspace_directory) not in report_content


def test_repeated_runs_are_stable_except_run_envelope(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    first = _pipeline(
        AnalysisConfig(output_directory=tmp_path / "first"),
        FakeMedia(),
    ).run(input_path)
    second = _pipeline(
        AnalysisConfig(output_directory=tmp_path / "second"),
        FakeMedia(),
    ).run(input_path)

    first_data = first.report.model_dump(mode="json")
    second_data = second.report.model_dump(mode="json")
    first_data.pop("analysis_id")
    first_data.pop("created_at")
    second_data.pop("analysis_id")
    second_data.pop("created_at")
    assert first_data == second_data


def test_interruption_cleans_staging_and_temporary_workspace(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "interrupted"
    media = FakeMedia()
    registry = DetectorRegistry([InterruptingDetector()])

    with pytest.raises(KeyboardInterrupt):
        _pipeline(
            AnalysisConfig(output_directory=output),
            media,
            registry=registry,
        ).run(input_path)

    assert not output.exists()
    assert all(not workspace.exists() for workspace in media.workspace_parents)


def test_publication_interruption_restores_previous_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    old_evidence = output / "evidence" / "frame.jpg"
    old_evidence.parent.mkdir(parents=True)
    old_evidence.write_bytes(b"old-evidence")
    old_report = output / "report.json"
    old_report.write_text('{"old": true}', encoding="utf-8")
    staging = output / ".staging"
    new_evidence = staging / "evidence" / "frame.jpg"
    new_evidence.parent.mkdir(parents=True)
    new_evidence.write_bytes(b"new-evidence")
    staged_report = staging / "report.json"
    staged_report.write_text('{"new": true}', encoding="utf-8")
    original_replace = Path.replace

    def interrupt_report_publish(source: Path, target: Path) -> Path:
        if source == staged_report:
            raise KeyboardInterrupt
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", interrupt_report_publish)

    with pytest.raises(KeyboardInterrupt):
        AnalysisPipeline._publish(staging, output)

    assert old_evidence.read_bytes() == b"old-evidence"
    assert old_report.read_text(encoding="utf-8") == '{"old": true}'


def test_video_processing_error_uses_processing_failure_class(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "broken.mp4"
    input_path.write_bytes(b"broken")
    media = FakeMedia()

    def fail_probe(path: Path, *, ffprobe: str) -> object:
        del path, ffprobe
        raise VideoDecodeError("video could not be decoded")

    pipeline = _pipeline(
        AnalysisConfig(output_directory=tmp_path / "failed"),
        media,
    )
    pipeline.probe_function = fail_probe  # type: ignore[assignment]

    with pytest.raises(AnalysisProcessingError, match="decoded"):
        pipeline.run(input_path)

    assert not (tmp_path / "failed").exists()


def test_report_json_is_valid_and_has_no_absolute_workspace(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    media = FakeMedia()
    result = _pipeline(
        AnalysisConfig(output_directory=tmp_path / "json-run"),
        media,
    ).run(input_path)

    payload = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert payload["runtime"]["sample_count"] == 7
    assert payload["runtime"]["scene_count"] == 1
    assert str(media.workspace_parents[0]) not in json.dumps(
        payload,
        ensure_ascii=False,
    )


def test_cooperative_cancellation_cleans_unpublished_artifacts(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "cancelled.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "cancelled-output"
    media = FakeMedia()

    with pytest.raises(AnalysisCancelledError, match="cancelled"):
        _pipeline(
            AnalysisConfig(output_directory=output),
            media,
            cancellation_callback=lambda: True,
        ).run(input_path)

    assert not output.exists()
