"""Explicit small-grid threshold calibration without changing defaults."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import JsonValue, ValidationError

from videoscope.analysis import (
    AnalysisConfig,
    AnalysisError,
    load_analysis_config,
)
from videoscope.benchmarking import (
    BenchmarkProfile,
    DetectorBenchmarkResult,
    run_benchmark,
)
from videoscope.benchmarking.models import BenchmarkProfileResult
from videoscope.detectors import create_builtin_detector_registry

Objective = Literal[
    "event_f1",
    "event_precision",
    "event_recall",
    "temporal_iou",
    "start_time_error",
    "end_time_error",
]
MAX_DEFAULT_COMBINATIONS = 64


def expand_parameter_grid(
    grid: dict[str, list[JsonValue]],
    *,
    maximum_combinations: int,
) -> list[dict[str, JsonValue]]:
    """Expand a stable Cartesian grid with a hard safety limit."""
    if maximum_combinations < 1:
        raise ValueError("maximum_combinations must be at least 1")
    if not grid:
        raise ValueError("parameter grid must not be empty")
    keys = sorted(grid)
    if any(not grid[key] for key in keys):
        raise ValueError("every grid parameter must contain at least one value")
    combination_count = 1
    for key in keys:
        combination_count *= len(grid[key])
    if combination_count > maximum_combinations:
        raise ValueError(
            f"grid expands to {combination_count} combinations; "
            f"limit is {maximum_combinations}"
        )
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[key] for key in keys))
    ]


def objective_value(
    result: DetectorBenchmarkResult,
    objective: Objective,
) -> float | None:
    """Read one detector-local optimization target."""
    if result.detector_error_count or result.evaluated_case_count == 0:
        return None
    metrics = result.metrics
    if objective == "event_f1":
        return metrics.event_f1
    if objective == "event_precision":
        return metrics.event_precision
    if objective == "event_recall":
        return metrics.event_recall
    if objective == "temporal_iou":
        return metrics.temporal_iou
    if objective == "start_time_error":
        return metrics.start_time_error_seconds
    return metrics.end_time_error_seconds


def select_best_index(
    values: Sequence[float | None],
    *,
    minimize: bool,
) -> int:
    """Select a deterministic best candidate; earliest wins exact ties."""
    ranked = [
        (
            value if minimize else -value,
            index,
        )
        for index, value in enumerate(values)
        if value is not None
    ]
    if not ranked:
        raise ValueError("no successful candidate produced the requested objective")
    return min(ranked)[1]


def _read_grid(path: Path, detector_id: str) -> dict[str, list[JsonValue]]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read grid file: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"grid file is not valid JSON: {path.name}") from exc
    if not isinstance(raw, dict):
        raise ValueError("grid JSON root must be an object")
    payload = cast(dict[str, Any], raw)
    if detector_id in payload and isinstance(payload[detector_id], dict):
        payload = cast(dict[str, Any], payload[detector_id])
    normalized: dict[str, list[JsonValue]] = {}
    for name, values in payload.items():
        if not isinstance(name, str) or not isinstance(values, list):
            raise ValueError("grid entries must map parameter names to arrays")
        normalized[name] = cast(list[JsonValue], values)
    return normalized


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_calibration(
    *,
    manifest_path: Path,
    output_directory: Path,
    detector_id: str,
    grid: dict[str, list[JsonValue]],
    objective: Objective,
    base_config: AnalysisConfig,
    minimum_iou: float,
    maximum_combinations: int,
) -> tuple[Path, Path]:
    """Benchmark the grid and write a suggestion without modifying defaults."""
    registry = create_builtin_detector_registry()
    registry.get(detector_id)
    candidates = expand_parameter_grid(
        grid,
        maximum_combinations=maximum_combinations,
    )
    profiles: list[BenchmarkProfile] = []
    candidate_configs: list[AnalysisConfig] = []
    for index, parameters in enumerate(candidates):
        data = base_config.model_dump(mode="python")
        detector_configurations = dict(base_config.detector_configurations)
        detector_configurations[detector_id] = {
            **detector_configurations.get(detector_id, {}),
            **parameters,
        }
        data["detector_configurations"] = detector_configurations
        data["enabled_detectors"] = (detector_id,)
        candidate_config = AnalysisConfig.model_validate(data)
        candidate_configs.append(candidate_config)
        profiles.append(
            BenchmarkProfile(
                label=f"grid-{index:03d}",
                config=candidate_config,
            )
        )

    benchmark = run_benchmark(
        manifest_path,
        output_directory=output_directory,
        profiles=profiles,
        detector_ids=(detector_id,),
        minimum_iou=minimum_iou,
        progress=lambda message: print(message, file=sys.stderr),
    )
    detector_results = [
        _profile_detector_result(profile, detector_id) for profile in benchmark.profiles
    ]
    values = [objective_value(result, objective) for result in detector_results]
    best_index = select_best_index(
        values,
        minimize=objective in {"start_time_error", "end_time_error"},
    )

    suggested = candidate_configs[best_index].model_dump(mode="json")
    suggested["output_directory"] = "videoscope-output"
    suggestion_path = output_directory / "suggested-config.json"
    search_path = output_directory / "calibration-results.json"
    _write_json(suggestion_path, suggested)
    _write_json(
        search_path,
        {
            "detector_id": detector_id,
            "objective": objective,
            "best_candidate_index": best_index,
            "best_objective_value": values[best_index],
            "candidates": [
                {
                    "candidate_index": index,
                    "parameters": candidates[index],
                    "objective_value": values[index],
                    "detector_result": detector_results[index].model_dump(mode="json"),
                }
                for index in range(len(candidates))
            ],
            "benchmark": benchmark.model_dump(mode="json"),
            "limitations": [
                "Calibration output is a suggestion and does not modify built-in "
                "detector defaults.",
                "Synthetic fixture optimization is not evidence of accuracy on "
                "real generated videos.",
            ],
        },
    )
    return suggestion_path, search_path


def _profile_detector_result(
    profile: BenchmarkProfileResult,
    detector_id: str,
) -> DetectorBenchmarkResult:
    return next(
        result for result in profile.detectors if result.detector_id == detector_id
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search an explicitly supplied small detector threshold grid. "
            "Built-in defaults are never modified."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--detector", required=True)
    parser.add_argument("--grid", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-config", type=Path)
    parser.add_argument(
        "--objective",
        choices=(
            "event_f1",
            "event_precision",
            "event_recall",
            "temporal_iou",
            "start_time_error",
            "end_time_error",
        ),
        default="event_f1",
    )
    parser.add_argument("--minimum-iou", type=float, default=0.1)
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=MAX_DEFAULT_COMBINATIONS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        base = (
            load_analysis_config(arguments.base_config)
            if arguments.base_config is not None
            else AnalysisConfig()
        )
        grid = _read_grid(arguments.grid, arguments.detector)
        suggestion, search = run_calibration(
            manifest_path=arguments.manifest,
            output_directory=arguments.output,
            detector_id=arguments.detector,
            grid=grid,
            objective=arguments.objective,
            base_config=base,
            minimum_iou=arguments.minimum_iou,
            maximum_combinations=arguments.max_combinations,
        )
    except (AnalysisError, ValidationError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Calibration interrupted.", file=sys.stderr)
        return 130

    print(f"Suggested configuration: {suggestion}")
    print(f"Complete search results: {search}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
