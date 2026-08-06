"""Real FFmpeg Safe Sharing acceptance over deterministic local fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.privacy import real_media_adapters
from tests.privacy.real_media_cases import (
    case_generated_qr_fixture_is_locally_decodable,
    case_real_clean_no_risk_workflow_still_verifies_public_copy,
    case_real_manual_redaction_delivers_verified_share_package,
    case_real_metadata_and_audio_redaction_preserve_source,
    case_real_qr_scan_redaction_and_native_rescan,
    case_real_sensitive_text_templates_are_removed_and_ordinary_text_remains,
)


def test_qr_rescanner_rejects_any_nonempty_decoded_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Detector:
        def detectAndDecode(self, frame: object) -> tuple[str, object, object]:
            del frame
            return "UNEXPECTED-PRIVATE-PAYLOAD", object(), object()

    class Cv2:
        @staticmethod
        def QRCodeDetector() -> Detector:
            return Detector()

    monkeypatch.setattr(real_media_adapters, "_cv2", lambda: Cv2())
    monkeypatch.setattr(
        real_media_adapters,
        "_read_frame",
        lambda _path, _timestamp: object(),
    )

    result = real_media_adapters._QrPayloadRescanner().rescan(
        tmp_path / "candidate.mp4",
        (),
        (0.0, 0.1),
    )

    assert result.checked_timestamps == (0.0, 0.1)
    assert result.detected_timestamps == (0.0, 0.1)


def test_real_manual_redaction_delivers_verified_share_package(tmp_path: Path) -> None:
    case_real_manual_redaction_delivers_verified_share_package(tmp_path)


def test_generated_qr_fixture_is_locally_decodable() -> None:
    case_generated_qr_fixture_is_locally_decodable()


def test_real_qr_scan_redaction_and_native_rescan(tmp_path: Path) -> None:
    case_real_qr_scan_redaction_and_native_rescan(tmp_path)


def test_real_sensitive_text_templates_are_removed_and_ordinary_text_remains(
    tmp_path: Path,
) -> None:
    case_real_sensitive_text_templates_are_removed_and_ordinary_text_remains(tmp_path)


def test_real_metadata_and_audio_redaction_preserve_source(tmp_path: Path) -> None:
    case_real_metadata_and_audio_redaction_preserve_source(tmp_path)


def test_real_clean_no_risk_workflow_still_verifies_public_copy(
    tmp_path: Path,
) -> None:
    case_real_clean_no_risk_workflow_still_verifies_public_copy(tmp_path)
