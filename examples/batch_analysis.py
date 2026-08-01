"""Analyze each local video in a directory with the public Python API."""

from __future__ import annotations

import argparse
from pathlib import Path

from videoscope.analysis import AnalysisConfig, AnalysisPipeline

VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".webm"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_directory", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runs/batch"),
        help="Parent directory for one report folder per input.",
    )
    parser.add_argument("--sample-fps", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_directory = args.input_directory.resolve()
    if not input_directory.is_dir():
        raise SystemExit(f"Input directory not found: {args.input_directory}")

    videos = tuple(
        path
        for path in sorted(input_directory.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES
    )
    if not videos:
        print("No supported local videos found.")
        return 0

    for index, video in enumerate(videos, start=1):
        output_directory = args.output / f"{index:04d}"
        config = AnalysisConfig(
            sample_fps=args.sample_fps,
            output_directory=output_directory,
        )
        result = AnalysisPipeline(config).run(video)
        print(
            f"{video.name}: {len(result.report.findings)} finding(s) -> "
            f"{result.report_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
