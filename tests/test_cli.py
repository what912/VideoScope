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
from videoscope.privacy.errors import PrivacyMediaError
from videoscope.privacy.models import PrivacyJobOutcome
from videoscope.rescue.errors import (
    RescueConfirmationError,
    RescueMediaError,
)
from videoscope.rescue.models import (
    RescueActionKind,
    RescueConfirmation,
    RescueStrategy,
    RescueSymptom,
)
from videoscope.rescue.pipeline import RescueConfig, RescueStatus
from videoscope.resolve import (
    PublishArtifactError,
    PublishBackend,
    PublishCancelledError,
    PublishMediaError,
    PublishPreparation,
    PublishProfileId,
    PublishReadyConfig,
    PublishReadyStatus,
    PublishResult,
)

runner = CliRunner()


def test_help_lists_rescue_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "rescue" in result.stdout


class _FakeRescuePipeline:
    instances: list["_FakeRescuePipeline"] = []
    execute_status = RescueStatus.COMPLETED
    prepare_error: BaseException | None = None
    execute_error: BaseException | None = None
    action_enabled = True

    def __init__(self, config: object, *, progress: object = None) -> None:
        self.config = cast(RescueConfig, config)
        self.progress = progress
        self.cancelled = False
        self.confirmations: list[RescueConfirmation] = []
        actions = (
            (
                SimpleNamespace(
                    id="adjust-luma",
                    kind=RescueActionKind.ADJUST_LUMA,
                    strategy=RescueStrategy.BALANCED,
                    changes_content=True,
                    requires_confirmation=True,
                    parameters={"damage_ids": ["damage_" + "b" * 64]},
                ),
            )
            if self.action_enabled
            else ()
        )
        self.plan = SimpleNamespace(
            plan_digest="a" * 64,
            actions=actions,
        )
        self.preparation = SimpleNamespace(plan=self.plan)
        type(self).instances.append(self)

    def prepare(self, source: Path) -> object:
        del source
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.preparation

    def confirm(self, preparation: object, confirmation: object) -> object:
        assert preparation is self.preparation
        typed_confirmation = cast(RescueConfirmation, confirmation)
        if typed_confirmation.plan_digest != self.plan.plan_digest:
            raise RescueConfirmationError("wrong digest")
        self.confirmations.append(typed_confirmation)
        return preparation

    def execute(self, preparation: object, confirmation: object) -> object:
        assert preparation is self.preparation
        assert confirmation in self.confirmations
        if self.execute_error is not None:
            raise self.execute_error
        return SimpleNamespace(
            status=self.execute_status,
            public_root=Path("rescue-output"),
        )

    def cancel(self) -> None:
        self.cancelled = True


def _install_fake_rescue_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRescuePipeline.instances = []
    _FakeRescuePipeline.execute_status = RescueStatus.COMPLETED
    _FakeRescuePipeline.prepare_error = None
    _FakeRescuePipeline.execute_error = None
    _FakeRescuePipeline.action_enabled = True
    monkeypatch.setattr("videoscope.cli.VideoRescuePipeline", _FakeRescuePipeline)


def test_rescue_noninteractive_requires_exact_confirmation_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: False)

    missing = runner.invoke(
        app, ["rescue", str(source), "--output", str(tmp_path / "out")]
    )
    mismatch = runner.invoke(
        app,
        [
            "rescue",
            str(source),
            "--output",
            str(tmp_path / "other"),
            "--confirm-plan",
            "f" * 64,
        ],
    )

    assert missing.exit_code == 2
    assert mismatch.exit_code == 2
    assert all(
        instance.confirmations == [] for instance in _FakeRescuePipeline.instances
    )


def test_rescue_interactive_decline_is_confirmation_exit_2_and_cancels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: True)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        ["rescue", str(source), "--output", str(tmp_path / "out")],
        input="n\n",
    )

    assert result.exit_code == 2
    assert "a" * 64 in result.output
    assert _FakeRescuePipeline.instances[0].cancelled is True
    assert _FakeRescuePipeline.instances[0].confirmations == []


def test_rescue_interactive_accepts_exact_plan_and_balanced_improved_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: True)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        ["rescue", str(source), "--output", str(tmp_path / "out")],
        input="y\n",
    )

    assert result.exit_code == 0
    confirmation = _FakeRescuePipeline.instances[0].confirmations[0]
    assert confirmation.plan_digest == "a" * 64
    assert confirmation.publish_improved is True
    assert confirmation.accepted_action_ids == ("adjust-luma",)


def test_rescue_cli_parses_all_bounded_options_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: False)
    source = tmp_path / "中文 video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "rescue",
            str(source),
            "--output",
            str(tmp_path / "输出"),
            "--strategy",
            "balanced",
            "--symptom",
            "dark",
            "--locked-range",
            "0.5:1.5",
            "--preview-seconds",
            "3",
            "--keep-workspace",
            "--quiet",
            "--confirm-plan",
            "a" * 64,
        ],
    )

    assert result.exit_code == 0
    assert result.output == ""
    config = _FakeRescuePipeline.instances[0].config
    assert config.strategy is RescueStrategy.BALANCED
    assert config.symptoms == (RescueSymptom.DARK,)
    assert config.locked_ranges == ((0.5, 1.5),)
    assert config.preview_seconds == 3.0
    assert config.keep_workspace is True


@pytest.mark.parametrize(
    "symptoms",
    [
        ["--symptom", ""],
        ["--symptom", "unknown"],
        ["--symptom", "dark", "--symptom", "dark"],
        ["--symptom", "missing_audio", "--symptom", "audio_noise"],
    ],
)
def test_rescue_cli_rejects_invalid_symptom_hints_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symptoms: list[str],
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "rescue",
            str(source),
            "--output",
            str(tmp_path / "out"),
            *symptoms,
            "--confirm-plan",
            "a" * 64,
        ],
    )

    assert result.exit_code == 2
    assert _FakeRescuePipeline.instances == []


def test_rescue_cli_records_valid_hint_without_inventing_improved_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    _FakeRescuePipeline.action_enabled = False
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "rescue",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--strategy",
            "balanced",
            "--symptom",
            "dark",
            "--confirm-plan",
            "a" * 64,
        ],
    )

    assert result.exit_code == 0
    instance = _FakeRescuePipeline.instances[0]
    assert instance.config.symptoms == (RescueSymptom.DARK,)
    assert instance.confirmations[0].publish_improved is False


@pytest.mark.parametrize(
    ("status", "exit_code", "message"),
    [
        (RescueStatus.COMPLETED, 0, "Video Rescue completed"),
        (RescueStatus.PARTIAL, 5, "partial output"),
        (RescueStatus.NEEDS_REVIEW, 5, "needs review"),
        (RescueStatus.FAILED, 4, "Video Rescue failed"),
        (RescueStatus.CANCELLED, 130, "Video Rescue was cancelled"),
    ],
)
def test_rescue_cli_maps_terminal_delivery_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: RescueStatus,
    exit_code: int,
    message: str,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    _FakeRescuePipeline.execute_status = status
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "rescue",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--confirm-plan",
            "a" * 64,
        ],
    )

    assert result.exit_code == exit_code
    assert message in result.output


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (RescueConfirmationError("private confirmation detail"), 2),
        (RescueMediaError("private media detail"), 3),
        (RuntimeError("private internal detail"), 4),
    ],
)
def test_rescue_cli_sanitizes_failure_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException,
    exit_code: int,
) -> None:
    _install_fake_rescue_pipeline(monkeypatch)
    _FakeRescuePipeline.execute_error = error
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "rescue",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--confirm-plan",
            "a" * 64,
        ],
    )

    assert result.exit_code == exit_code
    assert "private" not in result.output.casefold()


class _FakePrivacyPipeline:
    instances: list["_FakePrivacyPipeline"] = []
    execute_error: BaseException | None = None

    def __init__(self, output_directory: Path, **kwargs: object) -> None:
        self.output_directory = output_directory
        self.init_kwargs = kwargs
        self.scan_calls: list[Path] = []
        self.review_calls: list[tuple[object, ...]] = []
        self.confirm_calls: list[str] = []
        self._scan = SimpleNamespace(scan_id="1" * 32)
        self._review = SimpleNamespace(review_id="2" * 32)
        self._preparation = SimpleNamespace(
            preparation_id="3" * 32,
            plan=SimpleNamespace(digest="a" * 64),
        )
        type(self).instances.append(self)

    def scan(self, *, source: Path, config: object) -> object:
        del config
        self.scan_calls.append(source)
        private = self.output_directory / "privacy-review-private"
        private.mkdir(parents=True, exist_ok=True)
        (private / "risk-map.json").write_text("{}\n", encoding="utf-8")
        return self._scan

    def resume(self, *, source: Path, config: object) -> object:
        del source, config
        return self._scan

    def current_review(self, scan_id: str) -> object | None:
        del scan_id
        return None

    def current_preparation(self, scan_id: str) -> object | None:
        del scan_id
        return None

    def review(self, scan_id: str, reviews: tuple[object, ...]) -> object:
        del scan_id
        self.review_calls.append(reviews)
        return self._review

    def prepare(self, review_id: str) -> object:
        del review_id
        return self._preparation

    def preview(self, preparation_id: str) -> Path:
        del preparation_id
        preview = (
            self.output_directory
            / "privacy-review-private"
            / "preview"
            / "privacy-preview.mp4"
        )
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"preview")
        return preview

    def confirm(self, preparation_id: str, digest: str) -> object:
        del preparation_id
        self.confirm_calls.append(digest)
        if self.execute_error is not None:
            raise self.execute_error
        return SimpleNamespace(status=PrivacyJobOutcome.COMPLETED)


def _install_fake_privacy_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakePrivacyPipeline.instances = []
    _FakePrivacyPipeline.execute_error = None
    monkeypatch.setattr("videoscope.cli.SafeSharingPipeline", _FakePrivacyPipeline)


def test_help_lists_privacy_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "privacy" in result.stdout


def test_privacy_cli_scan_only_writes_private_risk_map(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_privacy_pipeline(monkeypatch)
    source = tmp_path / "中文 source.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "审查 output"

    result = runner.invoke(
        app,
        ["privacy", str(source), "--output", str(output), "--scan-only"],
    )

    assert result.exit_code == 0
    assert (output / "privacy-review-private" / "risk-map.json").is_file()
    assert not (output / "share-package" / "share-safe.mp4").exists()
    assert str(source.resolve()) not in result.output


def test_privacy_cli_review_prepares_without_implicit_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_privacy_pipeline(monkeypatch)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    review = tmp_path / "review.json"
    review.write_text('{"reviews": []}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "privacy",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--review-file",
            str(review),
        ],
    )

    assert result.exit_code == 0
    assert "a" * 64 in result.stdout
    assert _FakePrivacyPipeline.instances[0].confirm_calls == []


def test_privacy_cli_executes_only_exact_explicit_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_privacy_pipeline(monkeypatch)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    review = tmp_path / "review.json"
    review.write_text('{"reviews": []}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "privacy",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--review-file",
            str(review),
            "--confirm-digest",
            "a" * 64,
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    assert _FakePrivacyPipeline.instances[0].confirm_calls == ["a" * 64]


def test_privacy_cli_invalid_config_uses_exit_code_2(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    config = tmp_path / "bad.json"
    config.write_text("{bad", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "privacy",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--config",
            str(config),
            "--scan-only",
        ],
    )

    assert result.exit_code == 2
    assert "invalid" in result.stderr.casefold()


def test_privacy_cli_maps_media_failure_to_exit_code_3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_privacy_pipeline(monkeypatch)
    _FakePrivacyPipeline.execute_error = PrivacyMediaError("private path")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    review = tmp_path / "review.json"
    review.write_text('{"reviews": []}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "privacy",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--review-file",
            str(review),
            "--confirm-digest",
            "a" * 64,
        ],
    )

    assert result.exit_code == 3
    assert "private path" not in result.output


def test_privacy_cli_maps_unexpected_failure_to_exit_code_4_without_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_privacy_pipeline(monkeypatch)
    _FakePrivacyPipeline.execute_error = RuntimeError("private internal details")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    review = tmp_path / "review.json"
    review.write_text('{"reviews": []}', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "privacy",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--review-file",
            str(review),
            "--confirm-digest",
            "a" * 64,
        ],
    )

    assert result.exit_code == 4
    assert "private internal details" not in result.output


def test_privacy_cli_injects_one_offline_runtime_when_ocr_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_privacy_pipeline(monkeypatch)
    runtime = object()
    monkeypatch.setattr("videoscope.cli.create_model_runtime", lambda config: runtime)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        [
            "privacy",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--enable-ocr",
            "--scan-only",
        ],
    )

    assert result.exit_code == 0
    assert _FakePrivacyPipeline.instances[0].init_kwargs["model_runtime"] is runtime


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


class _FakePublishPipeline:
    """Small offline adapter double; pipeline behavior remains core-owned."""

    instances: list["_FakePublishPipeline"] = []
    execute_error: BaseException | None = None
    result_status = PublishReadyStatus.COMPLETED

    def __init__(self, config: PublishReadyConfig, **kwargs: object) -> None:
        self.config = config
        self.kwargs = kwargs
        self.prepare_calls: list[Path] = []
        self.execute_calls: list[str] = []
        self.discard_calls: list[PublishPreparation] = []
        self.publish_preview_calls: list[PublishPreparation] = []
        type(self).instances.append(self)

    def prepare(self, input_path: Path) -> PublishPreparation:
        self.prepare_calls.append(input_path)
        return cast(
            PublishPreparation,
            SimpleNamespace(
                plan=SimpleNamespace(
                    profile_id=self.config.profile_id,
                    backend=PublishBackend.NATIVE_LOCAL,
                    actions=(
                        SimpleNamespace(
                            action_id="transcode",
                            description="Create compatible MP4 output.",
                        ),
                        SimpleNamespace(
                            action_id="faststart",
                            description="Move MP4 metadata to the file start.",
                        ),
                    ),
                    output_filename="publish-ready.mp4",
                    plan_digest="a" * 64,
                ),
                preview_path=Path("preview") / "publish-preview.mp4",
            ),
        )

    def execute(
        self,
        preparation: PublishPreparation,
        confirmed_plan_digest: str,
    ) -> PublishResult:
        del preparation
        self.execute_calls.append(confirmed_plan_digest)
        error = self.execute_error
        if error is not None:
            raise error
        output = self.config.output_directory
        output.mkdir(parents=True)
        video_path = output / "publish-ready.mp4"
        video_path.write_bytes(b"published")
        return cast(
            PublishResult,
            SimpleNamespace(
                status=type(self).result_status,
                video_path=video_path,
                technical_report_path=output / "technical-report.json",
            ),
        )

    def discard(self, preparation: PublishPreparation) -> None:
        self.discard_calls.append(preparation)

    def publish_preview(self, preparation: PublishPreparation) -> Path:
        self.publish_preview_calls.append(preparation)
        preview = self.config.output_directory / "preview" / "publish-preview.mp4"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"preview")
        return preview


def _reset_fake_publish_pipeline() -> None:
    _FakePublishPipeline.instances = []
    _FakePublishPipeline.execute_error = None
    _FakePublishPipeline.result_status = PublishReadyStatus.COMPLETED


def _install_fake_publish_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_fake_publish_pipeline()
    monkeypatch.setattr(
        "videoscope.cli.PublishReadyPipeline",
        _FakePublishPipeline,
    )


def _publish_args(input_path: Path, output: Path, *extra: str) -> list[str]:
    return [
        "publish",
        str(input_path),
        "--profile",
        "social_vertical_9_16",
        "--output",
        str(output),
        *extra,
    ]


def test_help_lists_publish_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "publish" in result.stdout


def test_publish_help_lists_all_profile_values() -> None:
    result = runner.invoke(app, ["publish", "--help"])

    assert result.exit_code == 0
    compact_help = "".join(result.stdout.split())
    assert "compatible_mp4" in compact_help
    assert "_vertical_9_16" in compact_help
    assert "horizontal_16_9" in compact_help


def test_publish_passes_each_exact_profile_to_core_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for profile in PublishProfileId:
        _install_fake_publish_pipeline(monkeypatch)
        input_path = tmp_path / f"输入 {profile.value}.mp4"
        input_path.write_bytes(b"video")
        output = tmp_path / f"output {profile.value}"

        result = runner.invoke(
            app,
            [
                "publish",
                str(input_path),
                "--profile",
                profile.value,
                "--output",
                str(output),
                "--yes",
                "--quiet",
            ],
        )

        assert result.exit_code == 0
        assert _FakePublishPipeline.instances[0].config.profile_id is profile


def test_publish_displays_prepared_plan_then_processes_with_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    input_path = tmp_path / "中文 source video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "发布 output"

    result = runner.invoke(app, _publish_args(input_path, output, "--yes"))

    assert result.exit_code == 0
    assert output.joinpath("publish-ready.mp4").is_file()
    assert "Profile: social_vertical_9_16" in result.stdout
    assert "Backend: native_local" in result.stdout
    assert "transcode: Create compatible MP4 output." in result.stdout
    assert "faststart: Move MP4 metadata to the file start." in result.stdout
    assert "Output: publish-ready.mp4" in result.stdout
    assert f"Preview: {Path('preview') / 'publish-preview.mp4'}" in result.stdout
    assert str(input_path.resolve()) not in result.output
    assert _FakePublishPipeline.instances[0].execute_calls == ["a" * 64]


def test_publish_interactively_confirms_prepared_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: True)
    confirmations: list[str] = []

    def confirm(message: str) -> bool:
        confirmations.append(message)
        return True

    monkeypatch.setattr(
        "videoscope.cli.typer.confirm",
        confirm,
    )
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "output"

    result = runner.invoke(app, _publish_args(input_path, output))

    assert result.exit_code == 0
    assert confirmations == ["Process the full video with this plan?"]
    assert _FakePublishPipeline.instances[0].execute_calls == ["a" * 64]


def test_publish_rejects_noninteractive_processing_without_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: False)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(app, _publish_args(input_path, tmp_path / "output"))

    assert result.exit_code == 2
    assert "review the plan" in result.stderr
    assert "--yes" in result.stderr
    assert _FakePublishPipeline.instances[0].execute_calls == []
    assert len(_FakePublishPipeline.instances[0].discard_calls) == 1


def test_publish_preview_only_prepares_without_processing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    output = tmp_path / "output"
    result = runner.invoke(
        app,
        _publish_args(input_path, output, "--preview-only"),
    )

    assert result.exit_code == 0
    assert len(_FakePublishPipeline.instances[0].prepare_calls) == 1
    assert _FakePublishPipeline.instances[0].execute_calls == []
    assert len(_FakePublishPipeline.instances[0].publish_preview_calls) == 1
    assert output.joinpath("preview", "publish-preview.mp4").is_file()
    assert str(output / "preview" / "publish-preview.mp4") in result.stdout


def test_publish_declined_interactive_confirmation_cleans_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: True)
    monkeypatch.setattr("videoscope.cli.typer.confirm", lambda _message: False)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(app, _publish_args(input_path, tmp_path / "output"))

    assert result.exit_code == 2
    assert len(_FakePublishPipeline.instances[0].discard_calls) == 1
    assert _FakePublishPipeline.instances[0].execute_calls == []


def test_publish_quiet_suppresses_plan_and_completion_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    result = runner.invoke(
        app,
        _publish_args(input_path, tmp_path / "output", "--yes", "--quiet"),
    )

    assert result.exit_code == 0
    assert result.output == ""


def test_publish_missing_input_uses_exit_code_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        _publish_args(tmp_path / "missing.mp4", tmp_path / "output", "--yes"),
    )

    assert result.exit_code == 2
    assert str(tmp_path.resolve()) not in result.output


def test_publish_maps_core_errors_to_contract_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for error, expected_code in (
        (PublishMediaError("private FFmpeg details"), 3),
        (PublishArtifactError("private orchestration details"), 4),
        (RuntimeError("private unexpected details"), 4),
        (PublishCancelledError("cancelled"), 130),
    ):
        _install_fake_publish_pipeline(monkeypatch)
        _FakePublishPipeline.execute_error = error
        input_path = tmp_path / f"video-{expected_code}.mp4"
        input_path.write_bytes(b"video")

        result = runner.invoke(
            app,
            _publish_args(
                input_path,
                tmp_path / f"output-{expected_code}",
                "--yes",
                "--quiet",
            ),
        )

        assert result.exit_code == expected_code
        assert str(tmp_path.resolve()) not in result.output


def test_publish_needs_review_is_exit_code_5_not_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_publish_pipeline(monkeypatch)
    _FakePublishPipeline.result_status = PublishReadyStatus.NEEDS_REVIEW
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    output = tmp_path / "output"

    result = runner.invoke(app, _publish_args(input_path, output, "--yes"))

    assert result.exit_code == 5
    assert output.joinpath("publish-ready.mp4").is_file()
    assert "needs review" in result.stdout.lower()
