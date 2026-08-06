"""Command-line interface for VideoScope."""

import importlib.util
import ipaddress
import json
import sys
import webbrowser
from pathlib import Path
from typing import Literal, cast

import typer
from pydantic import ValidationError

from videoscope import __version__
from videoscope.ai import (
    DevicePreference,
    ModelHealth,
    ModelRuntimeConfig,
    ModelSpec,
    create_model_runtime,
)
from videoscope.ai.diagnostics import (
    render_model_doctor,
    render_model_list,
    run_model_doctor,
)
from videoscope.analysis import (
    AnalysisConfig,
    AnalysisConfigError,
    AnalysisError,
    AnalysisInternalError,
    AnalysisPipeline,
    load_analysis_config,
)
from videoscope.benchmarking import BenchmarkProfile, run_benchmark
from videoscope.benchmarking.terminal import render_benchmark_summary
from videoscope.content import (
    ContentConfig,
    ContentError,
    ContentGoal,
    ContentInputError,
    ContentPipelineConfig,
    ContentStatus,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    LongVideoContentPipeline,
    make_user_range_id,
    read_content_plan_json,
)
from videoscope.detectors import (
    create_builtin_detector_registry,
    create_optional_detector_registry,
)
from videoscope.doctor import has_failures, render_doctor, run_doctor
from videoscope.intelligence import (
    AdvancedAIConfig,
    AdvancedAIContentPipeline,
    AIReviewDecision,
    IntelligenceError,
    ReviewDecisionKind,
    read_review_manifest,
    read_suggestion_batch,
    reviewed_content_ranges,
)
from videoscope.privacy.errors import PrivacyError, PrivacyInputError
from videoscope.privacy.models import (
    PrivacyJobOutcome,
    PrivacyReviewDecision,
)
from videoscope.privacy.pipeline import SafeSharingConfig, SafeSharingPipeline
from videoscope.rescue import (
    RescueActionKind,
    RescueConfirmation,
    RescueError,
    RescueInputError,
    RescueStrategy,
    RescueSymptom,
)
from videoscope.rescue.pipeline import RescueConfig, RescueStatus, VideoRescuePipeline
from videoscope.resolve import (
    PublishInputError,
    PublishPreparation,
    PublishProfileId,
    PublishReadyConfig,
    PublishReadyPipeline,
    PublishReadyStatus,
    ResolveError,
)
from videoscope.video.hashing import compute_file_sha256

app = typer.Typer(
    add_completion=False,
    help="Local-first diagnostics for generated video quality.",
    no_args_is_help=True,
)
models_app = typer.Typer(
    help="Inspect optional model providers without loading or downloading them.",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"VideoScope {__version__}")
        raise typer.Exit


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        help="Show the VideoScope version and exit.",
        is_eager=True,
    ),
) -> None:
    """Inspect VideoScope and its local runtime dependencies."""


@app.command()
def doctor() -> None:
    """Check local runtime dependencies without installing anything."""
    checks = run_doctor()
    render_doctor(checks)
    if has_failures(checks):
        raise typer.Exit(code=1)


@models_app.command("list")
def models_list() -> None:
    """List lazily registered optional providers."""
    runtime = create_model_runtime()
    render_model_list(runtime.list_models())


@models_app.command("doctor")
def models_doctor(
    device: DevicePreference = typer.Option(
        DevicePreference.AUTO,
        "--device",
        help="Requested device policy; no GPU probe occurs without a provider.",
    ),
    cache_directory: Path | None = typer.Option(
        None,
        "--cache-directory",
        help="Override the local embedding cache directory for this check.",
    ),
    allow_model_download: bool = typer.Option(
        False,
        "--allow-model-download",
        help="Explicitly permit a future provider download; doctor downloads nothing.",
    ),
) -> None:
    """Check the shared runtime without importing heavy optional packages."""
    config = ModelRuntimeConfig(
        device=device,
        disk_cache_directory=cache_directory,
        allow_model_download=allow_model_download,
        interactive=True,
    )
    runtime = create_model_runtime(config)
    checks = run_model_doctor(
        config=config,
        models=runtime.list_models(),
    )
    render_model_doctor(checks)
    if has_failures(checks):
        raise typer.Exit(code=1)


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address; loopback is required unless --allow-network is set.",
    ),
    port: int = typer.Option(
        0,
        "--port",
        min=0,
        max=65535,
        help="TCP port. Zero asks the operating system for an available port.",
    ),
    job_directory: Path | None = typer.Option(
        None,
        "--job-directory",
        help="Override the platform application-data directory for local jobs.",
    ),
    max_upload_mib: int = typer.Option(
        1024,
        "--max-upload-mib",
        min=1,
        help="Maximum accepted upload size in MiB.",
    ),
    cpu_concurrency: int = typer.Option(
        2,
        "--cpu-concurrency",
        min=1,
        max=64,
        help="Maximum concurrent CPU analysis jobs.",
    ),
    heavy_ai_concurrency: int = typer.Option(
        1,
        "--heavy-ai-concurrency",
        min=1,
        max=16,
        help="Maximum concurrent optional model jobs.",
    ),
    job_ttl_hours: float = typer.Option(
        24.0,
        "--job-ttl-hours",
        min=0.001,
        help="Retention time for completed, failed, or cancelled jobs.",
    ),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Explicitly permit binding to a non-loopback address.",
    ),
) -> None:
    """Run the optional local API and packaged React dashboard."""
    if not host.strip():
        typer.echo("Error: --host must not be blank", err=True)
        raise typer.Exit(code=2)
    if not _is_loopback_host(host) and not allow_network:
        typer.echo(
            "Error: non-loopback binding requires --allow-network",
            err=True,
        )
        raise typer.Exit(code=2)
    missing = [
        package
        for package in ("fastapi", "uvicorn", "multipart")
        if importlib.util.find_spec(package) is None
    ]
    if missing:
        typer.echo(
            "Error: local Web API dependencies are not installed. Run "
            '`python -m pip install "genvideoscope[web]"`.',
            err=True,
        )
        raise typer.Exit(code=2)
    from videoscope.web.server import run_server

    run_server(
        host=host,
        port=port,
        job_directory=job_directory,
        max_upload_bytes=max_upload_mib * 1024 * 1024,
        cpu_concurrency=cpu_concurrency,
        heavy_ai_concurrency=heavy_ai_concurrency,
        job_ttl_seconds=job_ttl_hours * 60 * 60,
        allow_network=allow_network,
    )


@app.command()
def benchmark(
    manifest_path: Path = typer.Argument(
        ...,
        metavar="MANIFEST",
        help="UTF-8 JSON manifest containing local video annotations.",
    ),
    output: Path = typer.Option(
        Path("videoscope-benchmark"),
        "--output",
        "-o",
        help="Directory for benchmark.json.",
    ),
    detector: list[str] | None = typer.Option(
        None,
        "--detector",
        help="Benchmark this detector; repeat for multiple IDs.",
    ),
    config_path: list[Path] | None = typer.Option(
        None,
        "--config",
        help="Analysis JSON config; repeat to compare threshold profiles.",
    ),
    minimum_iou: float = typer.Option(
        0.1,
        "--minimum-iou",
        min=0.0,
        max=1.0,
        help="Minimum temporal IoU for one-to-one event matching.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress progress and terminal summary.",
    ),
) -> None:
    """Benchmark temporal Findings against explicit local annotations."""
    try:
        registry = create_builtin_detector_registry()
        selected = tuple(
            detector or [item.id for item in registry.list_default_enabled()]
        )
        paths = list(config_path or [])
        if paths:
            profiles = [
                BenchmarkProfile(
                    label=(
                        path.stem
                        if sum(candidate.stem == path.stem for candidate in paths) == 1
                        else f"{path.stem}-{index + 1}"
                    ),
                    config=load_analysis_config(path),
                )
                for index, path in enumerate(paths)
            ]
        else:
            profiles = [BenchmarkProfile(label="default", config=AnalysisConfig())]
        progress = (
            None
            if quiet
            else lambda message: typer.echo(
                message,
                err=True,
            )
        )
        report = run_benchmark(
            manifest_path,
            output_directory=output,
            profiles=profiles,
            detector_ids=selected,
            minimum_iou=minimum_iou,
            progress=progress,
        )
    except ValidationError as exc:
        typer.echo(f"Error: invalid benchmark options: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except AnalysisError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except KeyboardInterrupt as exc:
        if not quiet:
            typer.echo("Benchmark interrupted.", err=True)
        raise typer.Exit(code=130) from exc

    if not quiet:
        render_benchmark_summary(report)
        typer.echo(f"Benchmark: {output / 'benchmark.json'}")


@app.command()
def analyze(
    input_path: Path = typer.Argument(
        ...,
        metavar="INPUT",
        help="Local video file to analyze.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Directory for report.json, report.html, and evidence.",
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Optional local text prompt recorded in the JSON report.",
    ),
    enable_ai: bool = typer.Option(
        False,
        "--enable-ai",
        help="Enable optional local AI detectors and their lazy providers.",
    ),
    enable_ocr: bool = typer.Option(
        False,
        "--enable-ocr",
        help="Enable the optional local PaddleOCR text stability detector.",
    ),
    allow_model_download: bool = typer.Option(
        False,
        "--allow-model-download",
        help="Permit downloading missing model weights for this invocation.",
    ),
    ai_device: DevicePreference = typer.Option(
        DevicePreference.AUTO,
        "--ai-device",
        help="AI device policy: auto, cpu, or cuda.",
    ),
    sample_fps: float | None = typer.Option(
        None,
        "--sample-fps",
        min=0.000001,
        help="Fixed analysis sampling rate in frames per second.",
    ),
    detector: list[str] | None = typer.Option(
        None,
        "--detector",
        help="Enable only this detector; repeat for multiple IDs.",
    ),
    disable_detector: list[str] | None = typer.Option(
        None,
        "--disable-detector",
        help="Disable a detector after other selection; repeatable.",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="UTF-8 JSON analysis configuration file.",
    ),
    keep_workspace: bool = typer.Option(
        False,
        "--keep-workspace",
        help="Keep extracted analysis frames under the output directory.",
    ),
    json_only: bool = typer.Option(
        False,
        "--json-only",
        help="Generate report.json without report.html.",
    ),
    open_report: bool = typer.Option(
        False,
        "--open-report",
        help="Open report.html in the system browser after analysis.",
    ),
    bundle_video: bool = typer.Option(
        False,
        "--bundle-video",
        help="Copy the source video into the report directory.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress progress and completion output.",
    ),
) -> None:
    """Analyze one video with CPU and explicitly enabled optional detectors."""
    try:
        optional_runtime_enabled = enable_ai or enable_ocr
        if allow_model_download and not optional_runtime_enabled:
            raise AnalysisConfigError(
                "--allow-model-download requires --enable-ai or --enable-ocr"
            )
        if ai_device is not DevicePreference.AUTO and not optional_runtime_enabled:
            raise AnalysisConfigError(
                "--ai-device requires --enable-ai or --enable-ocr"
            )
        if json_only and open_report:
            raise AnalysisConfigError(
                "--open-report cannot be used together with --json-only"
            )
        if json_only and bundle_video:
            raise AnalysisConfigError(
                "--bundle-video cannot be used together with --json-only"
            )
        base_config = (
            load_analysis_config(config_path)
            if config_path is not None
            else AnalysisConfig()
        )
        ai_detector_ids = {"prompt_alignment", "visual_semantic_drift"}
        requested_ai = (
            bool(ai_detector_ids.intersection(detector or ()))
            or bool(ai_detector_ids.intersection(disable_detector or ()))
            or bool(
                ai_detector_ids.intersection(
                    base_config.enabled_detectors or (),
                )
            )
            or bool(
                ai_detector_ids.intersection(
                    base_config.detector_configurations,
                )
            )
        )
        if requested_ai and not enable_ai:
            raise AnalysisConfigError(
                "prompt_alignment requires --enable-ai; "
                "visual_semantic_drift requires --enable-ai"
            )
        ocr_detector_ids = {"text_stability"}
        requested_ocr = (
            bool(ocr_detector_ids.intersection(detector or ()))
            or bool(ocr_detector_ids.intersection(disable_detector or ()))
            or bool(
                ocr_detector_ids.intersection(
                    base_config.enabled_detectors or (),
                )
            )
            or bool(
                ocr_detector_ids.intersection(
                    base_config.detector_configurations,
                )
            )
        )
        if requested_ocr and not enable_ocr:
            raise AnalysisConfigError("text_stability requires --enable-ocr")
        registry = (
            create_optional_detector_registry(
                enable_ai=enable_ai,
                enable_ocr=enable_ocr,
            )
            if optional_runtime_enabled
            else create_builtin_detector_registry()
        )
        available = {item.id for item in registry.list_available()}
        disabled = tuple(disable_detector or ())
        unknown_disabled = set(disabled) - available
        if unknown_disabled:
            names = ", ".join(sorted(unknown_disabled))
            raise AnalysisConfigError(f"Unknown disabled detector ID(s): {names}")

        if detector:
            selected: tuple[str, ...] | None = tuple(detector)
        elif base_config.enabled_detectors is not None:
            selected = base_config.enabled_detectors
        elif disabled:
            selected = tuple(item.id for item in registry.list_default_enabled())
        else:
            selected = None
        effective_config = base_config.with_cli_overrides(
            output_directory=output,
            sample_fps=sample_fps,
            enabled_detectors=selected,
            disabled_detectors=disabled,
            keep_workspace=keep_workspace,
            json_only=json_only,
            bundle_video=bundle_video,
        )
        if effective_config.json_only and open_report:
            raise AnalysisConfigError(
                "--open-report requires HTML output; disable json_only"
            )
        progress = (
            None
            if quiet
            else lambda message: typer.echo(
                message,
                err=True,
            )
        )
        model_runtime = None
        if optional_runtime_enabled:
            runtime_config = ModelRuntimeConfig(
                device=ai_device,
                allow_model_download=allow_model_download,
                interactive=sys.stdin.isatty(),
            )

            def confirm_download(spec: ModelSpec, health: ModelHealth) -> bool:
                typer.echo(
                    f"Model {spec.provider_id}/{spec.model_id} is not cached. "
                    f"{health.message}",
                    err=True,
                )
                return cast(bool, typer.confirm("Download model weights now?"))

            model_runtime = create_model_runtime(
                runtime_config,
                confirm_download=confirm_download,
            )
        result = AnalysisPipeline(
            effective_config,
            registry=registry,
            progress=progress,
            model_runtime=model_runtime,
        ).run(input_path, prompt=prompt)
        if open_report:
            if result.html_report_path is None:
                raise AnalysisInternalError("HTML report was not generated")
            if not webbrowser.open(result.html_report_path.resolve().as_uri()):
                raise AnalysisInternalError("Could not open report in system browser")
    except ValidationError as exc:
        typer.echo(f"Error: invalid analysis options: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except AnalysisError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except KeyboardInterrupt as exc:
        if not quiet:
            typer.echo(
                "Analysis interrupted; partial artifacts were cleaned.", err=True
            )
        raise typer.Exit(code=130) from exc

    if not quiet:
        typer.echo(
            f"Report: {result.report_path} ({len(result.report.findings)} finding(s))"
        )
        if result.html_report_path is not None:
            typer.echo(f"HTML: {result.html_report_path}")


def _render_publish_plan(
    preparation: PublishPreparation,
    *,
    preview_path: Path | None = None,
) -> None:
    """Render only public, output-relative plan details before confirmation."""
    plan = preparation.plan
    typer.echo(f"Profile: {plan.profile_id.value}")
    typer.echo(f"Backend: {plan.backend.value}")
    typer.echo("Actions:")
    for action in plan.actions:
        typer.echo(f"{action.action_id}: {action.description}")
    typer.echo(f"Output: {plan.output_filename}")
    typer.echo(f"Preview: {preview_path or preparation.preview_path}")


def _is_interactive_stdin() -> bool:
    """Keep the confirmation policy explicit and independently testable."""
    return sys.stdin.isatty()


def _parse_rescue_ranges(values: list[str]) -> tuple[tuple[float, float], ...]:
    """Parse explicit source-time locks without accepting ambiguous input."""
    ranges: list[tuple[float, float]] = []
    for value in values:
        try:
            start_text, end_text = value.split(":", 1)
            start, end = float(start_text), float(end_text)
        except ValueError as exc:
            raise RescueInputError("locked ranges use START:END seconds") from exc
        if start < 0 or end < start:
            raise RescueInputError("locked ranges must be ordered non-negative seconds")
        ranges.append((start, end))
    return tuple(ranges)


def _parse_rescue_symptoms(values: list[str]) -> tuple[RescueSymptom, ...]:
    symptoms: list[RescueSymptom] = []
    for value in values:
        try:
            symptoms.append(RescueSymptom(value))
        except ValueError as exc:
            raise RescueInputError("unsupported Rescue symptom hint") from exc
    return tuple(symptoms)


def _parse_content_ranges(
    values: list[str],
    *,
    kind: ContentUserRangeKind,
    input_hash: str,
) -> tuple[ContentUserRange, ...]:
    ranges: list[ContentUserRange] = []
    for value in values:
        parts = value.split(":", 2)
        if len(parts) not in {2, 3}:
            raise ContentInputError("content ranges use START:END[:LABEL] seconds")
        try:
            source_range = ContentTimeRange(
                start_seconds=float(parts[0]),
                end_seconds=float(parts[1]),
            )
        except (ValueError, ValidationError) as exc:
            raise ContentInputError(
                "content ranges use ordered non-negative seconds"
            ) from exc
        label = parts[2].strip() if len(parts) == 3 else None
        if len(parts) == 3 and not label:
            raise ContentInputError("content range labels cannot be empty")
        ranges.append(
            ContentUserRange(
                id=make_user_range_id(input_hash, kind, source_range),
                kind=kind,
                source_range=source_range,
                label=label,
            )
        )
    return tuple(ranges)


def _render_content_review(review: object) -> None:
    from videoscope.content import ContentReview

    if not isinstance(review, ContentReview):
        raise TypeError("expected a ContentReview")
    typer.echo(f"Goal: {review.plan.goal.value}")
    typer.echo(f"Plan digest: {review.plan.plan_digest}")
    typer.echo(
        "Estimated output: "
        f"{review.plan.storyboard.estimated_output_duration_seconds:.3f}s"
    )
    typer.echo("Actions:")
    for action in review.plan.actions:
        marker = "CONFIRM" if action.changes_content else "KEEP"
        ranges = ", ".join(
            f"{item.start_seconds:.3f}-{item.end_seconds:.3f}s"
            for item in action.source_ranges
        )
        typer.echo(f"[{marker}] {action.id} {ranges} {action.description}")
    if review.previews:
        typer.echo(f"Private previews: {len(review.previews)}")


@app.command()
def assist(
    input_path: Path = typer.Argument(..., metavar="INPUT"),
    output: Path = typer.Option(..., "--output", "-o"),
    semantic_model: str = typer.Option(..., "--semantic-model"),
    transcript: Path | None = typer.Option(None, "--transcript"),
    asr_model: str = typer.Option("small", "--asr-model"),
    asr_language: str | None = typer.Option(None, "--asr-language"),
    ollama_endpoint: str = typer.Option("http://127.0.0.1:11434", "--ollama-endpoint"),
    locale: str = typer.Option("en", "--locale"),
    ai_device: DevicePreference = typer.Option(DevicePreference.AUTO, "--ai-device"),
    allow_model_download: bool = typer.Option(False, "--allow-model-download"),
    accept_all: bool = typer.Option(False, "--accept-all"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
) -> None:
    """Prepare grounded local AI suggestions for explicit human review."""
    if locale not in {"en", "zh-CN"}:
        typer.echo("Error: locale must be en or zh-CN.", err=True)
        raise typer.Exit(code=2)
    pipeline = AdvancedAIContentPipeline(
        AdvancedAIConfig(
            output_directory=output,
            transcript_path=transcript,
            asr_model_id=asr_model,
            asr_language=asr_language,
            semantic_model_id=semantic_model,
            ollama_endpoint=ollama_endpoint,
            locale=cast("Literal['en', 'zh-CN']", locale),
            device=ai_device,
            allow_model_download=allow_model_download,
            keep_workspace=keep_workspace,
        )
    )
    try:
        preparation = pipeline.prepare(input_path)
        typer.echo(f"Suggestion batch: {preparation.suggestions.batch_digest}")
        for suggestion in preparation.suggestions.suggestions:
            ranges = ", ".join(
                f"{item.start_seconds:.3f}-{item.end_seconds:.3f}s"
                for item in suggestion.evidence.source_ranges
            )
            typer.echo(
                f"{suggestion.id} [{suggestion.kind.value}] {ranges} "
                f"{suggestion.content}"
            )
            typer.echo(f"  Evidence: {suggestion.rationale}")
            for limitation in suggestion.limitations:
                typer.echo(f"  Limitation: {limitation}")
        if not accept_all and not _is_interactive_stdin():
            typer.echo(
                "Suggestions prepared only. Review the private JSON before applying."
            )
            return
        decisions: list[AIReviewDecision] = []
        for suggestion in preparation.suggestions.suggestions:
            accepted = accept_all or typer.confirm(
                f"Accept {suggestion.kind.value}: {suggestion.content}?",
                default=False,
            )
            decisions.append(
                AIReviewDecision(
                    suggestion_id=suggestion.id,
                    decision=(
                        ReviewDecisionKind.ACCEPT
                        if accepted
                        else ReviewDecisionKind.REJECT
                    ),
                )
            )
        review = pipeline.review(preparation, tuple(decisions))
        typer.echo(f"Review manifest: {review.manifest.review_digest}")
        typer.echo(
            "Apply it through videoscope content --ai-batch ... --ai-review ...; "
            "the ordinary C preview and exact confirmation are still required."
        )
    except (ValueError, ValidationError) as exc:
        typer.echo("Error: invalid Advanced AI input or configuration.", err=True)
        raise typer.Exit(code=2) from exc
    except IntelligenceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=4) from exc
    except (KeyboardInterrupt, typer.Abort) as exc:
        typer.echo("Advanced AI assist was cancelled.", err=True)
        raise typer.Exit(code=130) from exc
    except Exception as exc:
        typer.echo("Error: Advanced AI provider or orchestration failed.", err=True)
        raise typer.Exit(code=4) from exc


@app.command()
def content(
    input_path: Path = typer.Argument(..., metavar="INPUT"),
    goal: ContentGoal = typer.Option(ContentGoal.FAITHFUL_CLEAN, "--goal"),
    output: Path = typer.Option(..., "--output", "-o"),
    transcript: Path | None = typer.Option(None, "--transcript"),
    keep_range: list[str] = typer.Option([], "--keep-range"),
    exclude_range: list[str] = typer.Option([], "--exclude-range"),
    locked_keep_range: list[str] = typer.Option([], "--locked-keep-range"),
    locked_exclude_range: list[str] = typer.Option([], "--locked-exclude-range"),
    chapter: list[str] = typer.Option([], "--chapter"),
    target_duration: float | None = typer.Option(
        None,
        "--target-duration",
        min=0.001,
    ),
    reviewed_plan: Path | None = typer.Option(None, "--reviewed-plan"),
    yes: bool = typer.Option(False, "--yes"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    html_report: bool = typer.Option(True, "--html-report/--json-only"),
    export_subtitles: bool = typer.Option(False, "--export-subtitles"),
    export_clips: bool = typer.Option(False, "--export-clips"),
    ai_batch: Path | None = typer.Option(None, "--ai-batch"),
    ai_review: Path | None = typer.Option(None, "--ai-review"),
) -> None:
    """Review, confirm, and create useful content from a local long video."""
    pipeline: LongVideoContentPipeline | None = None
    try:
        if not input_path.is_file():
            raise ContentInputError("source video does not exist")
        input_hash = compute_file_sha256(input_path)
        if (ai_batch is None) is not (ai_review is None):
            raise ContentInputError("--ai-batch and --ai-review must be used together")
        ai_ranges: tuple[ContentUserRange, ...] = ()
        if ai_batch is not None and ai_review is not None:
            suggestion_batch = read_suggestion_batch(ai_batch)
            review_manifest = read_review_manifest(ai_review)
            if suggestion_batch.input_hash != input_hash:
                raise ContentInputError("AI review does not match the source video")
            ai_ranges = reviewed_content_ranges(
                suggestion_batch,
                review_manifest,
            )
        user_ranges = (
            *_parse_content_ranges(
                keep_range,
                kind=ContentUserRangeKind.KEEP,
                input_hash=input_hash,
            ),
            *_parse_content_ranges(
                exclude_range,
                kind=ContentUserRangeKind.EXCLUDE,
                input_hash=input_hash,
            ),
            *_parse_content_ranges(
                locked_keep_range,
                kind=ContentUserRangeKind.LOCKED_KEEP,
                input_hash=input_hash,
            ),
            *_parse_content_ranges(
                locked_exclude_range,
                kind=ContentUserRangeKind.LOCKED_EXCLUDE,
                input_hash=input_hash,
            ),
            *_parse_content_ranges(
                chapter,
                kind=ContentUserRangeKind.CHAPTER,
                input_hash=input_hash,
            ),
            *ai_ranges,
        )
        config = ContentPipelineConfig(
            output_directory=output,
            transcript_path=transcript,
            user_ranges=user_ranges,
            keep_workspace=keep_workspace,
            content=ContentConfig(
                goal=goal,
                target_duration_seconds=target_duration,
                export_subtitles=export_subtitles,
                export_clips=export_clips,
                generate_html_report=html_report,
            ),
        )
        progress = None if quiet else lambda status: typer.echo(status.value, err=True)
        pipeline = LongVideoContentPipeline(config, progress=progress)
        review = pipeline.preview(pipeline.prepare(input_path))
        if not quiet:
            _render_content_review(review)
        required = tuple(
            action.id
            for action in review.plan.actions
            if action.changes_content and action.requires_confirmation
        )
        if required:
            if yes:
                if reviewed_plan is None:
                    raise ContentInputError(
                        "content-changing --yes requires --reviewed-plan"
                    )
                supplied = read_content_plan_json(reviewed_plan)
                if supplied.plan_digest != review.plan.plan_digest:
                    raise ContentInputError(
                        "reviewed plan does not match the current exact plan"
                    )
            else:
                if reviewed_plan is not None:
                    raise ContentInputError("--reviewed-plan requires --yes")
                if not _is_interactive_stdin():
                    raise ContentInputError(
                        "review the plan interactively or pass --reviewed-plan --yes"
                    )
                if not typer.confirm(f"Execute exact plan {review.plan.plan_digest}?"):
                    raise ContentInputError("useful-content plan was not confirmed")
        elif reviewed_plan is not None:
            supplied = read_content_plan_json(reviewed_plan)
            if supplied.plan_digest != review.plan.plan_digest:
                raise ContentInputError("reviewed plan does not match the current plan")
        confirmation = pipeline.confirm(review, accepted_action_ids=required)
        result = pipeline.execute(review, confirmation)
    except FileNotFoundError as exc:
        typer.echo("Error: useful-content input file was not found.", err=True)
        raise typer.Exit(code=2) from exc
    except ValidationError as exc:
        typer.echo("Error: invalid useful-content options.", err=True)
        raise typer.Exit(code=2) from exc
    except ContentError as exc:
        typer.echo(f"Error: {exc.public_message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except (KeyboardInterrupt, typer.Abort) as exc:
        if pipeline is not None:
            pipeline.cancel()
        typer.echo("Long Video to Useful Content was cancelled.", err=True)
        raise typer.Exit(code=130) from exc
    except Exception as exc:
        typer.echo("Error: useful-content CLI orchestration failed.", err=True)
        raise typer.Exit(code=4) from exc
    finally:
        if pipeline is not None:
            pipeline.close()

    if result.status is ContentStatus.NEEDS_REVIEW:
        if not quiet:
            typer.echo("Useful-content output needs review.", err=True)
        raise typer.Exit(code=5)
    if result.status is ContentStatus.FAILED:
        if not quiet:
            typer.echo("Useful-content verification failed.", err=True)
        raise typer.Exit(code=5)
    if not quiet:
        typer.echo(f"Useful-content result: {result.status.value}")
        if result.public_root is not None:
            typer.echo(f"Output: {result.public_root}")


@app.command()
def rescue(
    input_path: Path = typer.Argument(..., metavar="INPUT"),
    output: Path = typer.Option(..., "--output", "-o"),
    strategy: RescueStrategy = typer.Option(RescueStrategy.CONSERVATIVE, "--strategy"),
    symptom: list[str] = typer.Option([], "--symptom"),
    locked_range: list[str] = typer.Option([], "--locked-range"),
    preview_seconds: float = typer.Option(
        10.0, "--preview-seconds", min=0.001, max=10.0
    ),
    confirm_plan: str | None = typer.Option(None, "--confirm-plan"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Prepare a local Rescue plan, then process only after exact confirmation."""
    try:
        config = RescueConfig(
            output_directory=output,
            strategy=strategy,
            symptoms=_parse_rescue_symptoms(symptom),
            locked_ranges=_parse_rescue_ranges(locked_range),
            preview_seconds=preview_seconds,
            keep_workspace=keep_workspace,
        )
        progress = None if quiet else lambda status: typer.echo(status.value, err=True)
        pipeline = VideoRescuePipeline(config, progress=progress)
        preparation = pipeline.prepare(input_path)
        digest = preparation.plan.plan_digest
        if not quiet:
            typer.echo(f"Plan digest: {digest}")
        if confirm_plan is None:
            if not _is_interactive_stdin():
                raise RescueInputError(
                    "Non-interactive Rescue requires --confirm-plan with the exact "
                    "digest"
                )
            if not typer.confirm("Process the full video with this Rescue plan?"):
                pipeline.cancel()
                raise RescueInputError("the Rescue plan was not confirmed")
            confirm_plan = digest
        confirmable_actions = tuple(
            action.id
            for action in preparation.plan.actions
            if action.requires_confirmation
        )
        trim_damage_ids_list: list[str] = []
        for action in preparation.plan.actions:
            if action.kind is not RescueActionKind.TRIM_DAMAGED_EDGES:
                continue
            values = action.parameters.get("damage_ids")
            if isinstance(values, list):
                trim_damage_ids_list.extend(
                    value for value in values if isinstance(value, str)
                )
        trim_damage_ids = tuple(trim_damage_ids_list)
        improvement_kinds = {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
            RescueActionKind.DEFLICKER,
            RescueActionKind.STABILIZE,
            RescueActionKind.NORMALIZE_AUDIO,
            RescueActionKind.DENOISE_AUDIO,
            RescueActionKind.CORRECT_FIXED_AV_OFFSET,
        }
        confirmation = RescueConfirmation(
            plan_digest=confirm_plan,
            publish_faithful=True,
            publish_improved=any(
                action.kind in improvement_kinds for action in preparation.plan.actions
            ),
            accepted_action_ids=confirmable_actions,
            accepted_trim_damage_ids=trim_damage_ids,
        )
        pipeline.confirm(preparation, confirmation)
        result = pipeline.execute(preparation, confirmation)
    except ValidationError as exc:
        typer.echo("Error: invalid Rescue configuration.", err=True)
        raise typer.Exit(code=2) from exc
    except RescueError as exc:
        typer.echo(f"Error: {exc.public_message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        typer.echo("Error: Video Rescue CLI orchestration failed.", err=True)
        raise typer.Exit(code=4) from exc
    if result.status is RescueStatus.CANCELLED:
        typer.echo("Video Rescue was cancelled.", err=True)
        raise typer.Exit(code=130)
    if result.status is RescueStatus.FAILED:
        typer.echo("Video Rescue failed.", err=True)
        raise typer.Exit(code=4)
    if result.status is RescueStatus.PARTIAL:
        typer.echo(
            "Video Rescue completed with partial output: rescue-output", err=True
        )
        raise typer.Exit(code=5)
    if result.status is RescueStatus.NEEDS_REVIEW:
        typer.echo("Video Rescue output needs review: rescue-output", err=True)
        raise typer.Exit(code=5)
    if not quiet and result.public_root is not None:
        typer.echo("Video Rescue completed: rescue-output")


def _load_privacy_config(path: Path | None) -> SafeSharingConfig:
    if path is None:
        return SafeSharingConfig()
    try:
        return SafeSharingConfig.model_validate_json(Path(path).read_bytes())
    except OSError as exc:
        raise PrivacyInputError("Safe Sharing config could not be read") from exc


def _load_privacy_reviews(path: Path) -> tuple[PrivacyReviewDecision, ...]:
    try:
        payload: object = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("reviews")
        if not isinstance(payload, list):
            raise ValueError("review document must contain a reviews array")
        return tuple(PrivacyReviewDecision.model_validate(item) for item in payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PrivacyInputError("Safe Sharing review file is invalid") from exc


@app.command()
def privacy(
    input_path: Path = typer.Argument(
        ...,
        metavar="INPUT",
        help="Local source video to scan without modifying it.",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="New directory containing physically separated private/public roots.",
    ),
    audience: str | None = typer.Option(
        None,
        "--audience",
        help=(
            "Sharing audience profile: public, work_client, school, family, "
            "external_ai."
        ),
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="UTF-8 JSON Safe Sharing configuration.",
    ),
    scan_only: bool = typer.Option(
        False,
        "--scan-only",
        help="Stop after writing the private risk map; never change video content.",
    ),
    review_file: Path | None = typer.Option(
        None,
        "--review-file",
        help="UTF-8 JSON human decisions for risks in the private risk map.",
    ),
    confirm_digest: str | None = typer.Option(
        None,
        "--confirm-digest",
        help="Execute only when this exact 64-character plan digest matches.",
    ),
    preview_only: bool = typer.Option(
        False,
        "--preview-only",
        help="Create a private reviewed preview without consuming confirmation.",
    ),
    enable_ocr: bool = typer.Option(
        False,
        "--enable-ocr",
        help="Enable optional local OCR proposals; model download remains disabled.",
    ),
    keep_workspace: bool = typer.Option(
        False,
        "--keep-workspace",
        help="Retain sampled private frames after a terminal result.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress lifecycle and completion output.",
    ),
) -> None:
    """Review privacy risks and create a separate local sharing copy."""
    try:
        if preview_only and confirm_digest is not None:
            raise PrivacyInputError(
                "--preview-only cannot be combined with --confirm-digest"
            )
        config = _load_privacy_config(config_path)
        overrides: dict[str, object] = {}
        if audience is not None:
            overrides["audience"] = audience
        if enable_ocr:
            overrides["enable_ocr"] = True
        if keep_workspace:
            overrides["keep_workspace"] = True
        if overrides:
            config = SafeSharingConfig.model_validate(
                {**config.model_dump(mode="python"), **overrides}
            )
        model_runtime = (
            create_model_runtime(
                ModelRuntimeConfig(
                    allow_model_download=False,
                    interactive=False,
                )
            )
            if config.enable_ocr
            else None
        )
        pipeline = SafeSharingPipeline(output, model_runtime=model_runtime)
        private_state = output / "privacy-review-private" / "pipeline-state.json"
        scan = (
            pipeline.resume(source=input_path, config=config)
            if private_state.is_file()
            else pipeline.scan(source=input_path, config=config)
        )
        if scan_only:
            if not quiet:
                typer.echo("Private risk map: privacy-review-private/risk-map.json")
            return

        reviewed = pipeline.current_review(scan.scan_id)
        if review_file is not None:
            reviewed = pipeline.review(
                scan.scan_id,
                _load_privacy_reviews(review_file),
            )
        if reviewed is None:
            raise PrivacyInputError(
                "Human review is required before Safe Sharing can prepare a plan"
            )
        preparation = pipeline.current_preparation(scan.scan_id)
        if preparation is None or review_file is not None:
            preparation = pipeline.prepare(reviewed.review_id)
        if preview_only:
            pipeline.preview(preparation.preparation_id)
            if not quiet:
                typer.echo("Private preview: preview/privacy-preview.mp4")
                typer.echo(f"Plan digest: {preparation.plan.digest}")
            return
        if confirm_digest is None:
            if not quiet:
                typer.echo(f"Plan digest: {preparation.plan.digest}")
                typer.echo(
                    "Review the private plan, then rerun with --confirm-digest "
                    "using that exact value."
                )
            return
        result = pipeline.confirm(preparation.preparation_id, confirm_digest)
    except ValidationError as exc:
        typer.echo("Error: invalid Safe Sharing configuration or review.", err=True)
        raise typer.Exit(code=2) from exc
    except PrivacyError as exc:
        typer.echo(f"Error: {exc.public_message}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except (KeyboardInterrupt, typer.Abort) as exc:
        typer.echo("Safe Sharing was cancelled.", err=True)
        raise typer.Exit(code=130) from exc
    except Exception as exc:
        typer.echo("Error: Safe Sharing CLI orchestration failed.", err=True)
        raise typer.Exit(code=4) from exc

    if result.status is PrivacyJobOutcome.NEEDS_REVIEW:
        if not quiet:
            typer.echo(
                "Independent checks need review; no public share package was created."
            )
        raise typer.Exit(code=5)
    if result.status is PrivacyJobOutcome.PARTIAL:
        if not quiet:
            typer.echo(
                "Optional checks were incomplete; no public share package was created."
            )
        raise typer.Exit(code=5)
    if result.status is PrivacyJobOutcome.FAILED:
        typer.echo("Error: Safe Sharing verification failed.", err=True)
        raise typer.Exit(code=4)
    if not quiet:
        typer.echo("Safe Sharing completed: share-package/share-safe.mp4")


@app.command()
def publish(
    input_path: Path = typer.Argument(
        ...,
        metavar="INPUT",
        help="Local source video to process without modifying it.",
    ),
    profile: PublishProfileId = typer.Option(
        ...,
        "--profile",
        help="Versioned local compatibility profile to apply.",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="New directory for Publish Ready artifacts.",
    ),
    preview_only: bool = typer.Option(
        False,
        "--preview-only",
        help="Prepare and display the plan and preview without full processing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm the prepared plan for non-interactive processing.",
    ),
    keep_workspace: bool = typer.Option(
        False,
        "--keep-workspace",
        help="Retain unpublished staging artifacts after a processing failure.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress plan and completion output.",
    ),
) -> None:
    """Prepare, confirm, and locally produce a separate Publish Ready video."""
    try:
        config = PublishReadyConfig(
            profile_id=profile,
            output_directory=output,
            keep_workspace=keep_workspace,
        )
        pipeline = PublishReadyPipeline(config)
        preparation = pipeline.prepare(input_path)
        try:
            if preview_only:
                published_preview = pipeline.publish_preview(preparation)
                if not quiet:
                    _render_publish_plan(
                        preparation,
                        preview_path=published_preview,
                    )
                return
            if not quiet:
                _render_publish_plan(preparation)
            if not yes:
                if not _is_interactive_stdin():
                    raise PublishInputError(
                        "Non-interactive processing requires you to review the plan "
                        "and pass --yes"
                    )
                if not typer.confirm("Process the full video with this plan?"):
                    raise PublishInputError(
                        "Publish Ready processing was not confirmed"
                    )
            result = pipeline.execute(
                preparation,
                confirmed_plan_digest=preparation.plan.plan_digest,
            )
        finally:
            pipeline.discard(preparation)
    except ValidationError as exc:
        typer.echo(f"Error: invalid publish options: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ResolveError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except (KeyboardInterrupt, typer.Abort) as exc:
        typer.echo("Publish Ready processing was cancelled.", err=True)
        raise typer.Exit(code=130) from exc
    except Exception as exc:
        typer.echo("Error: Publish Ready CLI orchestration failed", err=True)
        raise typer.Exit(code=4) from exc

    if result.status is PublishReadyStatus.NEEDS_REVIEW:
        if not quiet:
            typer.echo("Publish Ready output needs review.")
        raise typer.Exit(code=5)
    if not quiet:
        typer.echo("Publish Ready output completed: publish-ready.mp4")
