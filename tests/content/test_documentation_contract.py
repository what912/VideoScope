"""The stable public task-mode nomenclature and C contract stay aligned."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NORMATIVE_DOCUMENTS = (
    REPOSITORY_ROOT / "docs" / "product-spec.md",
    REPOSITORY_ROOT / "docs" / "architecture.md",
    REPOSITORY_ROOT / "docs" / "roadmap.md",
)


def _normative_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in NORMATIVE_DOCUMENTS)


def test_public_portfolio_letters_are_stable() -> None:
    text = _normative_text()

    for expected in (
        "A：Publish Ready",
        "B：Video Rescue",
        "C：Long Video to Useful Content",
        "D：Safe Sharing",
    ):
        assert expected in text


def test_historical_development_letters_are_not_normative_headings() -> None:
    text = _normative_text()

    assert "Resolve B：Safe Sharing" not in text
    assert "Resolve C：Video Rescue" not in text
    assert "开发线 B：Safe Sharing" not in text
    assert "开发线 C：Video Rescue" not in text


def test_c_contract_documents_required_safety_boundaries() -> None:
    schema = (REPOSITORY_ROOT / "docs" / "content-schema.md").read_text(
        encoding="utf-8"
    )
    guide = (REPOSITORY_ROOT / "docs" / "long-video-content.md").read_text(
        encoding="utf-8"
    )
    combined = f"{schema}\n{guide}"

    for required in (
        "Faithful Clean",
        "Chaptered Full",
        "Selected Clips",
        "content-review-private/",
        "content-output/",
        "source-map.json",
        "AnalysisReport",
        "Advanced AI",
    ):
        assert required in combined
