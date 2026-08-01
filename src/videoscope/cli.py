"""Command-line interface for VideoScope."""

import importlib.util
import ipaddress
import sys
import webbrowser
from pathlib import Path
from typing import cast

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
from videoscope.detectors import (
    create_builtin_detector_registry,
    create_optional_detector_registry,
)
from videoscope.doctor import has_failures, render_doctor, run_doctor

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
