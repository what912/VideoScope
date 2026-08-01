"""Tests for release archive content rules."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from scripts import audit_distribution


def make_wheel(path: Path, members: dict[str, str]) -> None:
    """Create a minimal ZIP-shaped wheel fixture."""
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_clean_wheel_passes_audit(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "videoscope/__init__.py": '__version__ = "0.1.0"\n',
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
        },
    )

    assert audit_distribution.audit_archive(wheel) == ()


def test_generated_video_and_run_output_are_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "tests/fixtures/generated/sample.mp4": "not a real video",
            "runs/demo/report.json": "{}",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("generated synthetic fixture" in item for item in violations)
    assert any("local analysis output" in item for item in violations)


def test_missing_report_template_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(wheel, {"videoscope/__init__.py": ""})

    violations = audit_distribution.audit_archive(wheel)

    assert any("required runtime asset is missing" in item for item in violations)


def test_personal_absolute_path_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    personal_path = "C:" + "\\Users\\" + "Example\\private.mp4"
    make_wheel(
        wheel,
        {
            "videoscope/debug.txt": personal_path,
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("personal absolute path" in item for item in violations)


def test_sdist_allows_sanitizer_test_examples(tmp_path: Path) -> None:
    source = tmp_path / "videoscope-0.1.0.tar.gz"
    example = "C:" + "\\Users\\" + "Example\\private.mp4"

    payload = example.encode("utf-8")
    info = tarfile.TarInfo("videoscope-0.1.0/tests/test_sanitizer.py")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))

    assert audit_distribution.audit_archive(source) == ()


def test_distribution_path_filters_supported_archives(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    source = tmp_path / "videoscope-0.1.0.tar.gz"
    ignored = tmp_path / "notes.txt"
    wheel.touch()
    source.touch()
    ignored.touch()

    assert audit_distribution.distribution_paths(tmp_path) == (wheel, source)
