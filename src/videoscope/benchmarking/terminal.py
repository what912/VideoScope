"""Rich terminal rendering for per-detector benchmark summaries."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from videoscope.benchmarking.models import BenchmarkReport


def render_benchmark_summary(
    report: BenchmarkReport,
    *,
    console: Console | None = None,
) -> None:
    """Print one row per detector/configuration without a global score."""
    output = console or Console()
    table = Table(title="VideoScope temporal detector benchmark")
    table.add_column("Configuration")
    table.add_column("Detector")
    table.add_column("Cases", justify="right")
    table.add_column("Precision", justify="right")
    table.add_column("Recall", justify="right")
    table.add_column("F1", justify="right")
    table.add_column("tIoU", justify="right")
    table.add_column("Negative FP", justify="right")
    table.add_column("Errors", justify="right")
    for profile in report.profiles:
        for detector in profile.detectors:
            metrics = detector.metrics
            table.add_row(
                profile.label,
                detector.detector_id,
                str(detector.evaluated_case_count),
                f"{metrics.event_precision:.3f}",
                f"{metrics.event_recall:.3f}",
                f"{metrics.event_f1:.3f}",
                f"{metrics.temporal_iou:.3f}",
                (
                    f"{detector.negative_false_positive_event_count} / "
                    f"{detector.negative_false_positive_duration_seconds:.3f}s"
                ),
                str(detector.detector_error_count),
            )
    output.print(table)
    output.print(
        "[yellow]Synthetic fixture metrics are engineering regression results, "
        "not real-video accuracy.[/yellow]"
    )
