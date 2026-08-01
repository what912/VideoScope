"""Reproducible temporal benchmarking for CPU detectors."""

from videoscope.benchmarking.manifest import (
    BenchmarkManifest,
    load_benchmark_manifest,
)
from videoscope.benchmarking.metrics import (
    evaluate_intervals,
    temporal_iou,
)
from videoscope.benchmarking.models import (
    BenchmarkInterval,
    BenchmarkProfile,
    BenchmarkReport,
    DetectorBenchmarkResult,
    EventMetrics,
)
from videoscope.benchmarking.runner import BenchmarkRunner, run_benchmark
from videoscope.benchmarking.serialization import (
    benchmark_to_json,
    write_benchmark_json,
)

__all__ = [
    "BenchmarkInterval",
    "BenchmarkManifest",
    "BenchmarkProfile",
    "BenchmarkReport",
    "BenchmarkRunner",
    "DetectorBenchmarkResult",
    "EventMetrics",
    "benchmark_to_json",
    "evaluate_intervals",
    "load_benchmark_manifest",
    "run_benchmark",
    "temporal_iou",
    "write_benchmark_json",
]
