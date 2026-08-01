"""Structural and privacy tests for the offline HTML report."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from tests.domain.test_models import make_finding, make_report
from videoscope.domain import (
    AnalysisReport,
    DetectorExecution,
    DetectorStatus,
    Evidence,
    Severity,
)
from videoscope.reporting import HTMLReportRenderer


class ReportHTMLParser(HTMLParser):
    """Collect structural HTML data without comparing a brittle snapshot."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append((tag, dict(attrs)))

    def with_attribute(
        self,
        attribute: str,
    ) -> list[tuple[str, dict[str, str | None]]]:
        return [item for item in self.tags if attribute in item[1]]


def _render(
    tmp_path: Path,
    report: AnalysisReport | None = None,
) -> tuple[str, ReportHTMLParser]:
    selected_report = make_report() if report is None else report
    output = HTMLReportRenderer().render(selected_report, tmp_path)
    content = output.read_text(encoding="utf-8")
    parser = ReportHTMLParser()
    parser.feed(content)
    return content, parser


def test_html_contains_findings_and_exact_timeline_intervals(
    tmp_path: Path,
) -> None:
    first = make_finding(
        start_seconds=0.5,
        end_seconds=1.75,
        detector_id="core.first",
        severity=Severity.HIGH,
    )
    second = make_finding(
        start_seconds=2.0,
        end_seconds=3.0,
        detector_id="core.second",
        severity=Severity.LOW,
    )
    report = make_report([first, second])

    content, parser = _render(tmp_path, report)

    cards = parser.with_attribute("data-finding-id")
    rendered_ids = {attributes["data-finding-id"] for _, attributes in cards}
    assert rendered_ids == {first.id, second.id}
    marker = next(
        attributes
        for tag, attributes in cards
        if tag == "button" and attributes["data-finding-id"] == first.id
    )
    assert marker["data-start"] == "0.5"
    assert marker["data-end"] == "1.75"
    assert first.title in content
    assert second.title in content


def test_dynamic_html_is_escaped(tmp_path: Path) -> None:
    dangerous_title = "<script>alert('finding')</script>"
    finding = make_finding(title=dangerous_title)
    report = make_report([finding]).model_copy(
        update={"prompt": '<img src="x" onerror="alert(1)">'}
    )

    content, _ = _render(tmp_path, report)

    assert dangerous_title not in content
    assert "&lt;script&gt;alert" in content
    assert '<img src="x" onerror="alert(1)">' not in content
    assert "&lt;img src=&#34;x&#34;" in content


def test_html_has_no_remote_resources(tmp_path: Path) -> None:
    content, _ = _render(tmp_path)

    assert re.search(r"""(?:src|href)=["']https?://""", content) is None
    assert "<link " not in content
    assert "font-face" not in content


def test_detector_error_is_separate_from_empty_success(tmp_path: Path) -> None:
    report = make_report([]).model_copy(
        update={
            "detector_executions": [
                DetectorExecution(
                    detector_id="core.failed",
                    status=DetectorStatus.DETECTOR_ERROR,
                    elapsed_seconds=0.2,
                    findings_count=0,
                    error_type="RuntimeError",
                    error_message="sanitized failure",
                ),
                DetectorExecution(
                    detector_id="core.empty",
                    status=DetectorStatus.OK,
                    elapsed_seconds=0.1,
                    findings_count=0,
                ),
            ]
        }
    )

    content, parser = _render(tmp_path, report)

    failures = parser.with_attribute("data-detector-error")
    assert [attributes["data-detector-error"] for _, attributes in failures] == [
        "core.failed"
    ]
    assert "These checks failed" in content
    assert "sanitized failure" in content
    assert "core.empty" in content


def test_unsafe_or_missing_evidence_path_does_not_break_report(
    tmp_path: Path,
) -> None:
    finding = make_finding()
    finding.evidence[0].relative_path = "C:/Users/private/frame.jpg"
    report = make_report([finding])

    content, parser = _render(tmp_path, report)

    image_sources = [
        attributes["src"]
        for tag, attributes in parser.tags
        if tag == "img" and attributes.get("src")
    ]
    assert image_sources == []
    assert "C:/Users/private" not in content
    assert "No local evidence image is available" in content


def test_bundled_video_is_only_rendered_when_explicitly_supplied(
    tmp_path: Path,
) -> None:
    _, default_parser = _render(tmp_path / "default")
    report = make_report()
    bundled_output = tmp_path / "bundled"
    path = HTMLReportRenderer().render(
        report,
        bundled_output,
        bundled_video_relative_path="media/bundled-video.mp4",
    )
    content = path.read_text(encoding="utf-8")

    assert not any(tag == "video" for tag, _ in default_parser.tags)
    assert 'src="media/bundled-video.mp4"' in content


def test_ocr_evidence_boxes_are_drawn_from_normalized_metadata(
    tmp_path: Path,
) -> None:
    finding = make_finding()
    finding.evidence = [
        Evidence(
            evidence_type="ocr_frame",
            timestamp_seconds=0.5,
            relative_path="evidence/frame.png",
            description="OCR frame",
            metadata={
                "ocr_boxes": [
                    {
                        "text": "<unstable>",
                        "confidence": 0.91,
                        "bounding_box": {
                            "x_min": 0.1,
                            "y_min": 0.6,
                            "x_max": 0.8,
                            "y_max": 0.9,
                        },
                    }
                ]
            },
        )
    ]

    content, parser = _render(tmp_path, make_report([finding]))

    overlays = parser.with_attribute("data-ocr-box")
    assert len(overlays) == 1
    _, attributes = overlays[0]
    assert "left: 10.000000%" in str(attributes["style"])
    assert "width: 70.000000%" in str(attributes["style"])
    assert attributes["title"] == "<unstable> (0.91)"
    assert "<unstable>" not in content
    assert "&lt;unstable&gt;" in content
