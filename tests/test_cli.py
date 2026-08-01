"""Tests for the VideoScope CLI."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from videoscope import __version__
from videoscope.ai import ModelRuntimeConfig
from videoscope.analysis import (
    AnalysisConfig,
    AnalysisInternalError,
    AnalysisProcessingError,
)
from videoscope.cli import app
from videoscope.detectors import DetectorRegistry

runner = CliRunner()


def test_help_lists_doctor_and_analyze_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "analyze" in result.stdout
    assert "benchmark" in result.stdout
    assert "models" in result.stdout
    assert "serve" in result.stdout
    assert "Local-first diagnostics" in result.stdout


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"VideoScope {__version__}"


def test_models_list_reports_lazy_optional_providers() -> None:
    result = runner.invoke(app, ["models", "list"])

    assert result.exit_code == 0
    assert "openclip" in result.stdout
    assert "ViT-B-32" in result.stdout
    assert "dinov2" in result.stdout
    assert "facebookresearch/dinov2" in result.stdout
    assert "paddleocr" in result.stdout
    assert "PP-OCRv5-mobile/ch" in result.stdout


def test_models_doctor_is_offline_and_allows_no_provider(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "models",
            "doctor",
            "--cache-directory",
            str(tmp_path / "模型 cache"),
        ],
    )

    assert result.exit_code == 0
    assert "Implicit model download is disabled" in result.stdout
    assert "4 provider model(s) registered" in result.stdout


def test_serve_defaults_to_loopback_and_random_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def capture_server(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("videoscope.web.server.run_server", capture_server)

    result = runner.invoke(
        app,
        ["serve", "--job-directory", str(tmp_path / "本地 jobs")],
    )

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 0
    assert captured["cpu_concurrency"] == 2
    assert captured["heavy_ai_concurrency"] == 1
    assert captured["allow_network"] is False


def test_serve_requires_explicit_permission_for_network_binding() -> None:
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "requires --allow-network" in result.stderr


def test_analyze_missing_input_uses_exit_code_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["analyze", str(tmp_path / "missing.mp4")],
    )

    assert result.exit_code == 2


def test_benchmark_missing_manifest_uses_exit_code_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["benchmark", str(tmp_path / "missing.json")],
    )

    assert result.exit_code == 2
    assert "manifest not found" in result.stderr


def test_invalid_json_config_uses_exit_code_2(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    config = tmp_path / "bad.json"
    config.write_text("{bad", encoding="utf-8")

    result = runner.invoke(
        app,
        ["analyze", str(input_path), "--config", str(config)],
    )

    assert result.exit_code == 2
    assert "not valid JSON" in result.stderr


def test_unknown_detector_uses_exit_code_2_before_media_processing(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "analyze",
            str(input_path),
            "--detector",
            "missing.detector",
        ],
    )

    assert result.exit_code == 2
    assert "Unknown detector" in result.stderr


def test_prompt_alignment_requires_explicit_ai_enable(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "analyze",
            str(input_path),
            "--detector",
            "prompt_alignment",
        ],
    )

    assert result.exit_code == 2
    assert "requires --enable-ai" in result.stderr


def _assert_pipeline_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    error: AnalysisProcessingError | AnalysisInternalError,
    expected_code: int,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    class FailingPipeline:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run(self, input_path: Path, *, prompt: str | None = None) -> object:
            del input_path, prompt
            raise error

    monkeypatch.setattr("videoscope.cli.AnalysisPipeline", FailingPipeline)

    result = runner.invoke(app, ["analyze", str(input_path)])

    assert result.exit_code == expected_code


def test_analyze_maps_processing_failure_to_exit_code_3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_pipeline_failure_exit_code(
        monkeypatch,
        tmp_path,
        error=AnalysisProcessingError("decode failed"),
        expected_code=3,
    )


def test_analyze_maps_internal_failure_to_exit_code_4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_pipeline_failure_exit_code(
        monkeypatch,
        tmp_path,
        error=AnalysisInternalError("internal failed"),
        expected_code=4,
    )


def test_findings_do_not_change_success_exit_code_and_quiet_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    class SuccessfulPipeline:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run(
            self,
            input_path: Path,
            *,
            prompt: str | None = None,
        ) -> object:
            del input_path, prompt
            return SimpleNamespace(
                report_path=tmp_path / "report.json",
                html_report_path=None,
                report=SimpleNamespace(findings=[object()]),
            )

    monkeypatch.setattr("videoscope.cli.AnalysisPipeline", SuccessfulPipeline)

    result = runner.invoke(
        app,
        ["analyze", str(input_path), "--quiet", "--json-only"],
    )

    assert result.exit_code == 0
    assert result.stdout == ""


def test_cli_options_override_json_config_and_repeat_detector_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "输入 video.mp4"
    input_path.write_bytes(b"video")
    config_path = tmp_path / "analysis.json"
    config_path.write_text(
        ('{"sample_fps": 1.0, "enabled_detectors": ["near_black", "possible_freeze"]}'),
        encoding="utf-8",
    )
    output = tmp_path / "custom output"
    captured: dict[str, object] = {}

    class CapturingPipeline:
        def __init__(
            self,
            config: AnalysisConfig,
            **kwargs: object,
        ) -> None:
            captured["config"] = config
            captured["kwargs"] = kwargs

        def run(
            self,
            input_path: Path,
            *,
            prompt: str | None = None,
        ) -> object:
            captured["input_path"] = input_path
            captured["prompt"] = prompt
            return SimpleNamespace(
                report_path=output / "report.json",
                html_report_path=output / "report.html",
                report=SimpleNamespace(findings=[]),
            )

    monkeypatch.setattr("videoscope.cli.AnalysisPipeline", CapturingPipeline)

    result = runner.invoke(
        app,
        [
            "analyze",
            str(input_path),
            "--config",
            str(config_path),
            "--output",
            str(output),
            "--sample-fps",
            "3",
            "--detector",
            "near_black",
            "--detector",
            "global_flicker",
            "--disable-detector",
            "near_black",
            "--prompt",
            "本地提示",
            "--keep-workspace",
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    effective = captured["config"]
    assert isinstance(effective, AnalysisConfig)
    assert effective.output_directory == output
    assert effective.sample_fps == 3.0
    assert effective.enabled_detectors == ("global_flicker",)
    assert effective.keep_workspace is True
    assert captured["prompt"] == "本地提示"


def test_enable_ai_passes_runtime_device_and_download_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    captured: dict[str, object] = {}
    fake_runtime = SimpleNamespace()

    def create_runtime(config: object, **kwargs: object) -> object:
        captured["runtime_config"] = config
        captured["runtime_kwargs"] = kwargs
        return fake_runtime

    class CapturingPipeline:
        def __init__(
            self,
            config: AnalysisConfig,
            **kwargs: object,
        ) -> None:
            captured["config"] = config
            captured["pipeline_kwargs"] = kwargs

        def run(
            self,
            input_path: Path,
            *,
            prompt: str | None = None,
        ) -> object:
            del input_path, prompt
            return SimpleNamespace(
                report_path=tmp_path / "report.json",
                html_report_path=None,
                report=SimpleNamespace(findings=[]),
            )

    monkeypatch.setattr("videoscope.cli.create_model_runtime", create_runtime)
    monkeypatch.setattr("videoscope.cli.AnalysisPipeline", CapturingPipeline)

    result = runner.invoke(
        app,
        [
            "analyze",
            str(input_path),
            "--enable-ai",
            "--allow-model-download",
            "--ai-device",
            "cpu",
            "--json-only",
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    runtime_config = cast(ModelRuntimeConfig, captured["runtime_config"])
    assert runtime_config.device.value == "cpu"
    assert runtime_config.allow_model_download is True
    pipeline_kwargs = cast(dict[str, object], captured["pipeline_kwargs"])
    assert pipeline_kwargs["model_runtime"] is fake_runtime
    registry = cast(DetectorRegistry, pipeline_kwargs["registry"])
    assert "prompt_alignment" in {item.id for item in registry.list_available()}


def test_ai_runtime_options_rejected_without_enable_ai(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "analyze",
            str(input_path),
            "--allow-model-download",
        ],
    )

    assert result.exit_code == 2
    assert "requires --enable-ai or --enable-ocr" in result.stderr


def test_text_stability_requires_enable_ocr_and_uses_optional_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    rejected = runner.invoke(
        app,
        ["analyze", str(input_path), "--detector", "text_stability"],
    )
    assert rejected.exit_code == 2
    assert "text_stability requires --enable-ocr" in rejected.stderr

    captured: dict[str, object] = {}

    class CapturingPipeline:
        def __init__(self, config: AnalysisConfig, **kwargs: object) -> None:
            del config
            captured.update(kwargs)

        def run(
            self,
            input_path: Path,
            *,
            prompt: str | None = None,
        ) -> object:
            del input_path, prompt
            return SimpleNamespace(
                report_path=tmp_path / "report.json",
                html_report_path=None,
                report=SimpleNamespace(findings=[]),
            )

    monkeypatch.setattr("videoscope.cli.AnalysisPipeline", CapturingPipeline)
    enabled = runner.invoke(
        app,
        [
            "analyze",
            str(input_path),
            "--enable-ocr",
            "--detector",
            "text_stability",
            "--json-only",
            "--quiet",
        ],
    )

    assert enabled.exit_code == 0
    registry = cast(DetectorRegistry, captured["registry"])
    assert "text_stability" in {item.id for item in registry.list_available()}
    assert captured["model_runtime"] is not None


def test_open_report_uses_system_browser_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    html_path = tmp_path / "output" / "report.html"
    opened: list[str] = []

    class SuccessfulPipeline:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run(
            self,
            input_path: Path,
            *,
            prompt: str | None = None,
        ) -> object:
            del input_path, prompt
            return SimpleNamespace(
                report_path=tmp_path / "output" / "report.json",
                html_report_path=html_path,
                report=SimpleNamespace(findings=[]),
            )

    def record_opened_url(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr("videoscope.cli.AnalysisPipeline", SuccessfulPipeline)
    monkeypatch.setattr(
        "videoscope.cli.webbrowser.open",
        record_opened_url,
    )

    result = runner.invoke(app, ["analyze", str(input_path), "--open-report"])

    assert result.exit_code == 0
    assert opened == [html_path.resolve().as_uri()]


def test_open_report_rejects_json_only(tmp_path: Path) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(
        app,
        ["analyze", str(input_path), "--json-only", "--open-report"],
    )

    assert result.exit_code == 2
    assert "cannot be used together" in result.stderr
