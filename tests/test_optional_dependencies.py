"""Packaging boundaries for optional AI, OCR, and Web runtimes."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast


def project_metadata() -> dict[str, Any]:
    """Read pyproject metadata using the Python standard library only."""
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], document["project"])


def test_base_dependencies_exclude_heavy_optional_runtimes() -> None:
    metadata = project_metadata()
    dependencies = {
        str(requirement).partition(">")[0].partition("=")[0].casefold()
        for requirement in metadata["dependencies"]
    }

    assert dependencies.isdisjoint(
        {
            "torch",
            "torchvision",
            "open-clip-torch",
            "dinov2",
            "paddleocr",
            "paddlepaddle",
            "fastapi",
            "uvicorn",
            "python-multipart",
        }
    )


def test_expected_optional_dependency_groups_are_declared() -> None:
    metadata = project_metadata()
    extras = cast(dict[str, list[str]], metadata["optional-dependencies"])

    assert {"ai", "ocr", "web", "all"} <= extras.keys()
    assert set(extras["all"]) == set(extras["ai"] + extras["ocr"] + extras["web"])


def test_development_extra_uses_official_httpx_test_client() -> None:
    metadata = project_metadata()
    extras = cast(dict[str, list[str]], metadata["optional-dependencies"])

    assert "httpx>=0.27" in extras["dev"]
    assert not any(requirement.startswith("httpx2") for requirement in extras["dev"])
