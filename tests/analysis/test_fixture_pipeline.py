"""Conditional full-pipeline tests over all generated synthetic fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import generate_test_videos as fixture_factory
from videoscope.analysis import AnalysisConfig, AnalysisPipeline


def test_all_generated_fixtures_run_end_to_end(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for full pipeline fixture tests")
    assert ffmpeg is not None
    assert ffprobe is not None
    fixture_root = tmp_path / "fixtures"
    generated_root = fixture_root / "generated"
    manifest_path = fixture_root / "manifest.json"
    generated = fixture_factory.generate_fixtures(
        output_directory=generated_root,
        manifest_path=manifest_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )
    assert generated
    assert manifest_path.is_file()

    for input_path in generated:
        output = tmp_path / input_path.stem
        result = AnalysisPipeline(
            AnalysisConfig(output_directory=output),
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        ).run(input_path)

        assert result.report_path.is_file()
        assert result.report.metadata.filename == input_path.name
        assert len(result.report.detector_executions) == 4
