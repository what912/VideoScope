"""Offline diagnostics for the optional shared model runtime."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from rich.console import Console
from rich.table import Table

from videoscope.ai.models import ModelRuntimeConfig, ModelSpec
from videoscope.ai.runtime import default_embedding_cache_directory
from videoscope.doctor import DoctorCheck, DoctorStatus

OPTIONAL_RUNTIME_PACKAGES = (
    ("torch", "ai"),
    ("open_clip", "ai"),
    ("paddleocr", "ocr"),
    ("fastapi", "web"),
)


def check_model_cache_directory(path: Path | None = None) -> DoctorCheck:
    """Verify the embedding cache can be written without retaining test data."""
    cache_directory = path or default_embedding_cache_directory()
    try:
        cache_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(
            dir=cache_directory,
            prefix=".videoscope-model-cache-test-",
        ) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        return DoctorCheck(
            name="Embedding cache",
            status=DoctorStatus.FAIL,
            message=f"Embedding cache is not writable: {type(exc).__name__}.",
        )
    return DoctorCheck(
        name="Embedding cache",
        status=DoctorStatus.PASS,
        message="The local embedding cache directory is writable.",
    )


def check_optional_packages() -> DoctorCheck:
    """Inspect optional package metadata without importing heavy runtimes."""
    installed = [
        module
        for module, _ in OPTIONAL_RUNTIME_PACKAGES
        if importlib.util.find_spec(module) is not None
    ]
    if not installed:
        return DoctorCheck(
            name="Optional packages",
            status=DoctorStatus.WARN,
            message=(
                "No optional AI/OCR/Web packages are installed; "
                "CPU analysis is unaffected."
            ),
        )
    return DoctorCheck(
        name="Optional packages",
        status=DoctorStatus.PASS,
        message=f"Available without import: {', '.join(installed)}.",
    )


def run_model_doctor(
    *,
    config: ModelRuntimeConfig,
    models: tuple[ModelSpec, ...],
) -> tuple[DoctorCheck, ...]:
    """Run diagnostics without loading providers, probing CUDA, or networking."""
    provider_check = DoctorCheck(
        name="Model providers",
        status=DoctorStatus.PASS,
        message=(
            f"{len(models)} provider model(s) registered."
            if models
            else "No real providers registered; this is valid for the CPU build."
        ),
    )
    policy_check = DoctorCheck(
        name="Download policy",
        status=DoctorStatus.PASS,
        message=(
            "Model download is explicitly allowed for this invocation."
            if config.allow_model_download
            else "Implicit model download is disabled."
        ),
    )
    return (
        DoctorCheck(
            name="Shared runtime",
            status=DoctorStatus.PASS,
            message="Provider protocols, batching, and cache runtime are available.",
        ),
        check_model_cache_directory(config.disk_cache_directory),
        check_optional_packages(),
        provider_check,
        policy_check,
    )


def render_model_doctor(
    checks: tuple[DoctorCheck, ...],
    *,
    console: Console | None = None,
) -> None:
    """Render optional runtime diagnostics."""
    output = console or Console()
    table = Table(title="VideoScope models doctor")
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Details")
    styles = {
        DoctorStatus.PASS: "green",
        DoctorStatus.WARN: "yellow",
        DoctorStatus.FAIL: "red",
    }
    for check in checks:
        table.add_row(
            check.name,
            f"[{styles[check.status]}]{check.status}[/{styles[check.status]}]",
            check.message,
        )
    output.print(table)


def render_model_list(
    models: tuple[ModelSpec, ...],
    *,
    console: Console | None = None,
) -> None:
    """Render registered provider metadata without constructing providers."""
    output = console or Console()
    if not models:
        output.print(
            "No real model providers are registered. "
            "The CPU analysis commands remain fully available."
        )
        return
    table = Table(title="VideoScope model providers")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Capabilities")
    table.add_column("Extra")
    for model in models:
        table.add_row(
            model.provider_id,
            model.model_id,
            ", ".join(model.capabilities),
            model.required_extra,
        )
    output.print(table)
