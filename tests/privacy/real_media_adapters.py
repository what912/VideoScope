"""Native media adapters and independent privacy rescanners for fixture tests."""

from __future__ import annotations

import importlib
import json
import math
import shutil
import subprocess
from array import array
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from videoscope.domain import Severity
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyAction,
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)
from videoscope.privacy.scanners import (
    PrivacyScannerExecution,
    PrivacyScannerRunResult,
    PrivacyScannerStatus,
)
from videoscope.privacy.verification import (
    AudioVerificationResult,
    MediaVerificationSnapshot,
    RegressionVerificationResult,
    RescanVerificationResult,
    TemporalCoverageResult,
)
from videoscope.video.probe import probe_video_with_private_summary

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
GENERATED_ROOT = FIXTURE_ROOT / "generated"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
REVIEWED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _privacy_manifest() -> dict[str, dict[str, object]]:
    payload = cast(dict[str, object], json.loads(MANIFEST_PATH.read_text("utf-8")))
    return cast(dict[str, dict[str, object]], payload["privacy"])


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip(
            "FFmpeg and ffprobe are required for native Safe Sharing fixture tests"
        )
    assert ffmpeg is not None
    assert ffprobe is not None
    return ffmpeg, ffprobe


def _run_media(arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        list(arguments),
        shell=False,
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8", errors="replace"
    )[-2000:]
    return completed


def _media_probe(ffprobe: str, path: Path) -> MediaVerificationSnapshot:
    metadata, private = probe_video_with_private_summary(path, ffprobe=ffprobe)
    structural_global = {"major_brand", "minor_version", "compatible_brands", "encoder"}
    structural_stream = {"language", "handler_name", "vendor_id", "encoder"}
    embedded = bool(
        set(private.global_tags) - structural_global
        or any(set(tags.tags) - structural_stream for tags in private.stream_tags)
        or private.chapter_tags
        or private.attachment_tags
    )
    return MediaVerificationSnapshot(
        duration_seconds=metadata.duration_seconds,
        average_frame_rate=metadata.average_frame_rate,
        video_stream_count=1,
        audio_stream_count=1 if metadata.has_audio else 0,
        has_embedded_metadata=embedded,
    )


def _cv2() -> Any:
    return cast(Any, importlib.import_module("cv2"))


def _read_frame(path: Path, timestamp: float) -> Any:
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = capture.read()
    finally:
        capture.release()
    assert ok and frame is not None, f"could not decode frame at {timestamp:.3f}s"
    return frame


def _frame_rate(path: Path) -> float:
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    try:
        rate = float(capture.get(cv2.CAP_PROP_FPS))
    finally:
        capture.release()
    assert rate > 0
    return rate


def _pixel_bounds(box: NormalizedBox, frame: Any) -> tuple[int, int, int, int]:
    height, width = frame.shape[:2]
    return (
        round(box.x_min * width),
        round(box.y_min * height),
        round(box.x_max * width),
        round(box.y_max * height),
    )


class _RealCoverageChecker:
    """Confirm every requested redaction sample changes the reviewed pixels."""

    def verify(
        self,
        source: Path,
        candidate: Path,
        action: PrivacyAction,
        timestamps: tuple[float, ...],
    ) -> TemporalCoverageResult:
        cv2 = _cv2()
        if action.box is None:
            return TemporalCoverageResult(available=False)
        uncovered: list[float] = []
        for timestamp in timestamps:
            source_frame = _read_frame(source, timestamp)
            candidate_frame = _read_frame(candidate, timestamp)
            x_min, y_min, x_max, y_max = _pixel_bounds(action.box, source_frame)
            difference = cv2.absdiff(
                source_frame[y_min:y_max, x_min:x_max],
                candidate_frame[y_min:y_max, x_min:x_max],
            )
            minimum_difference = (
                0.25 if action.parameters.get("style") == "blur" else 8.0
            )
            if float(difference.mean()) < minimum_difference:
                uncovered.append(timestamp)
        return TemporalCoverageResult(
            checked_timestamps=timestamps,
            uncovered_timestamps=tuple(uncovered),
        )


class _QrPayloadRescanner:
    """Rescan every requested native frame without publishing decoded payloads."""

    def rescan(
        self,
        candidate: Path,
        risks: tuple[PrivacyRisk, ...],
        timestamps: tuple[float, ...],
    ) -> RescanVerificationResult:
        del risks
        detector = _cv2().QRCodeDetector()
        detected: list[float] = []
        for timestamp in timestamps:
            decoded, _points, _straight = detector.detectAndDecode(
                _read_frame(candidate, timestamp)
            )
            if decoded:
                detected.append(timestamp)
        return RescanVerificationResult(
            available=True,
            checked_timestamps=timestamps,
            detected_timestamps=tuple(detected),
        )


class _TemplateTextScannerRunner:
    """Manifest-guided scanner that still requires real source-image templates."""

    scanner_id = "fixture_template_text"
    scanner_version = "1.0.0"

    def __init__(self, cases: Sequence[Mapping[str, object]]) -> None:
        self._cases = tuple(dict(case) for case in cases)
        self.templates: dict[str, Any] = {}

    def run(
        self,
        context: object,
        configurations: Mapping[str, Mapping[str, object]],
    ) -> PrivacyScannerRunResult:
        del configurations
        scan_context = cast(Any, context)
        risks: list[PrivacyRisk] = []
        for case in self._cases:
            if case["sensitive"] is not True:
                continue
            start = float(str(case["start_seconds"]))
            end = float(str(case["end_seconds"]))
            box = NormalizedBox.model_validate(case["box"])
            midpoint = (start + end) / 2.0
            sample = min(
                scan_context.frame_samples,
                key=lambda item: abs(item.timestamp_seconds - midpoint),
            )
            cv2 = _cv2()
            numpy = cast(Any, importlib.import_module("numpy"))
            frame = cv2.imdecode(
                numpy.fromfile(
                    scan_context.resolve_frame_path(sample.relative_path),
                    dtype=numpy.uint8,
                ),
                cv2.IMREAD_COLOR,
            )
            assert frame is not None
            x_min, y_min, x_max, y_max = _pixel_bounds(box, frame)
            template = frame[y_min:y_max, x_min:x_max].copy()
            assert float(template.std()) > 10.0
            risk_id = make_privacy_risk_id(
                scan_context.input_hash,
                self.scanner_id,
                PrivacyRiskType.SUSPICIOUS_TEXT,
                start,
                end,
                box,
            )
            self.templates[risk_id] = template
            risks.append(
                PrivacyRisk(
                    id=risk_id,
                    scanner_id=self.scanner_id,
                    scanner_version=self.scanner_version,
                    risk_type=PrivacyRiskType.SUSPICIOUS_TEXT,
                    title="Sensitive-looking text template proposed for review",
                    public_description=(
                        "A local image-template pass proposed a text region for "
                        "manual review."
                    ),
                    severity=Severity.MEDIUM,
                    confidence=1.0,
                    start_seconds=start,
                    end_seconds=end,
                    box=box,
                    recommended_style=RedactionStyle.SOLID_FILL,
                    limitations=(
                        "This deterministic fixture recognizer is not a general OCR "
                        "engine.",
                    ),
                    evidence=(
                        {
                            "timestamp_seconds": sample.timestamp_seconds,
                            "relative_path": sample.relative_path,
                            "box": box.model_dump(mode="json"),
                        },
                    ),
                    private_evidence=(
                        {
                            "ocr_text": str(case["value"]),
                            "kind": str(case["kind"]),
                        },
                    ),
                )
            )
        return PrivacyScannerRunResult(
            executions=(
                PrivacyScannerExecution(
                    scanner_id=self.scanner_id,
                    status=PrivacyScannerStatus.OK,
                    elapsed_seconds=0.0,
                    risks_count=len(risks),
                ),
            ),
            risks=tuple(risks),
        )


class _TemplateTextRescanner:
    """Compare candidate regions against private source-image templates."""

    def __init__(self, scanner: _TemplateTextScannerRunner) -> None:
        self._scanner = scanner

    def rescan(
        self,
        candidate: Path,
        risks: tuple[PrivacyRisk, ...],
        timestamps: tuple[float, ...],
    ) -> RescanVerificationResult:
        cv2 = _cv2()
        detected: list[float] = []
        for timestamp in timestamps:
            risk = next(
                (
                    item
                    for item in risks
                    if item.start_seconds <= timestamp < item.end_seconds
                ),
                None,
            )
            if risk is None or risk.box is None:
                continue
            frame = _read_frame(candidate, timestamp)
            x_min, y_min, x_max, y_max = _pixel_bounds(risk.box, frame)
            candidate_region = frame[y_min:y_max, x_min:x_max]
            template = self._scanner.templates[risk.id]
            resized = cv2.resize(
                candidate_region,
                (template.shape[1], template.shape[0]),
                interpolation=cv2.INTER_AREA,
            )
            if float(cv2.absdiff(resized, template).mean()) < 12.0:
                detected.append(timestamp)
        return RescanVerificationResult(
            available=True,
            checked_timestamps=timestamps,
            detected_timestamps=tuple(detected),
        )


def _pcm_rms(ffmpeg: str, path: Path, start: float, duration: float) -> float:
    completed = _run_media(
        (
            ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        )
    )
    samples = array("h")
    samples.frombytes(completed.stdout)
    assert samples
    return math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768


def _tag_probe_text(ffprobe: str, path: Path) -> str:
    completed = _run_media(
        (
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags:stream_disposition:chapter_tags",
            "-of",
            "json",
            str(path),
        )
    )
    return completed.stdout.decode("utf-8", errors="replace")


class _RealAudioAnalyzer:
    def __init__(self, ffmpeg: str) -> None:
        self._ffmpeg = ffmpeg

    def verify(
        self,
        source: Path,
        candidate: Path,
        actions: tuple[PrivacyAction, ...],
        timestamps: tuple[float, ...],
    ) -> AudioVerificationResult:
        excessive: list[float] = []
        for action in actions:
            duration = action.end_seconds - action.start_seconds
            if _pcm_rms(self._ffmpeg, candidate, action.start_seconds, duration) > 0.01:
                excessive.extend(
                    timestamp
                    for timestamp in timestamps
                    if action.start_seconds <= timestamp < action.end_seconds
                )
        retained = (
            _pcm_rms(self._ffmpeg, source, 0.2, 0.4) > 0.05
            and _pcm_rms(self._ffmpeg, candidate, 0.2, 0.4) > 0.05
        )
        return AudioVerificationResult(
            available=True,
            checked_timestamps=timestamps,
            excessive_energy_timestamps=tuple(excessive),
            outside_signal_retained=retained,
        )


def _event_summary(path: Path, detector_id: str) -> tuple[int, float]:
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    frames: list[Any] = []
    fps = float(capture.get(cv2.CAP_PROP_FPS)) or 10.0
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    finally:
        capture.release()
    if detector_id == "near_black":
        flags = [float(frame.mean()) < 4.0 for frame in frames]
    else:
        flags = [False]
        flags.extend(
            float(cv2.absdiff(previous, current).mean()) < 0.25
            for previous, current in zip(frames, frames[1:], strict=False)
        )
    count = sum(flags)
    return count, count / fps


class _RealRegressionAnalyzer:
    def compare(
        self,
        source: Path,
        candidate: Path,
        detector_id: str,
    ) -> RegressionVerificationResult:
        before_count, before_duration = _event_summary(source, detector_id)
        after_count, after_duration = _event_summary(candidate, detector_id)
        return RegressionVerificationResult(
            available=True,
            before_event_count=before_count,
            before_duration_seconds=before_duration,
            after_event_count=after_count,
            after_duration_seconds=after_duration,
        )


class _NoRiskScannerRunner:
    """Deterministic scanner boundary for the explicit clean/no-risk case."""

    def run(
        self,
        context: object,
        configurations: Mapping[str, Mapping[str, object]],
    ) -> PrivacyScannerRunResult:
        del context, configurations
        return PrivacyScannerRunResult(
            executions=tuple(
                PrivacyScannerExecution(
                    scanner_id=scanner_id,
                    status=PrivacyScannerStatus.OK,
                    elapsed_seconds=0.0,
                    risks_count=0,
                )
                for scanner_id in ("anonymous_face", "qr_barcode")
            )
        )
