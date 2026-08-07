"""Real Safe Sharing fixture scenarios, separated from reusable test adapters."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from scripts.generate_test_videos import PRIVACY_QR_PAYLOAD
from tests.privacy.real_media_adapters import (
    GENERATED_ROOT,
    _cv2,
    _local_video_tools,
    _pcm_rms,
    _pixel_bounds,
    _privacy_manifest,
    _QrPayloadRescanner,
    _read_frame,
    _tag_probe_text,
    _TemplateTextRescanner,
    _TemplateTextScannerRunner,
)
from tests.privacy.real_media_runner import (
    _assert_public_package_is_separate_and_path_free,
    _nonpassing_checks,
    _run_real_safe_sharing,
)
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyJobOutcome,
)
from videoscope.privacy.scanners import PrivacyScannerRunner
from videoscope.privacy.visual import QrBarcodeScanner
from videoscope.video import compute_file_sha256
from videoscope.video.probe import probe_video_with_private_summary


def case_real_manual_redaction_delivers_verified_share_package(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = GENERATED_ROOT / "privacy_manual_visual.mp4"
    assert source.is_file(), "run scripts/generate_test_videos.py --force first"
    source_hash = compute_file_sha256(source)

    result = _run_real_safe_sharing(
        source,
        tmp_path / "浜哄伐 鑴辨晱 output",
        source.name,
        ffmpeg,
        ffprobe,
        no_visual_scan=True,
        preview=True,
    )

    assert result.status is PrivacyJobOutcome.COMPLETED, _nonpassing_checks(result)
    assert compute_file_sha256(source) == source_hash
    assert (tmp_path / "浜哄伐 鑴辨晱 output" / result.video_relative_path).is_file()
    _assert_public_package_is_separate_and_path_free(tmp_path / "浜哄伐 鑴辨晱 output")


def case_generated_qr_fixture_is_locally_decodable() -> None:
    _local_video_tools()
    source = GENERATED_ROOT / "privacy_qr.mp4"
    assert source.is_file(), "run scripts/generate_test_videos.py --force first"
    detector = _cv2().QRCodeDetector()
    for timestamp in (0.5, 1.5, 2.5, 3.5):
        decoded, points, _ = detector.detectAndDecode(_read_frame(source, timestamp))
        assert decoded == PRIVACY_QR_PAYLOAD
        assert points is not None


def case_real_qr_scan_redaction_and_native_rescan(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = GENERATED_ROOT / "privacy_qr.mp4"
    assert source.is_file(), "run scripts/generate_test_videos.py --force first"
    output = tmp_path / "QR 鎵弿 output 眉"
    source_hash = compute_file_sha256(source)

    result = _run_real_safe_sharing(
        source,
        output,
        source.name,
        ffmpeg,
        ffprobe,
        scanner_runner=PrivacyScannerRunner((QrBarcodeScanner(),)),
        qr_rescanner=_QrPayloadRescanner(),
    )

    assert result.status is PrivacyJobOutcome.COMPLETED, _nonpassing_checks(result)
    qr_check = next(
        check
        for check in result.verification.checks
        if check.check_id == "qr_redaction"
    )
    assert qr_check.measured == {
        "requested_samples": 40,
        "checked_samples": 40,
        "detections": 0,
    }
    assert compute_file_sha256(source) == source_hash
    _assert_public_package_is_separate_and_path_free(
        output,
        forbidden_strings=(PRIVACY_QR_PAYLOAD,),
    )


def case_real_sensitive_text_templates_are_removed_and_ordinary_text_remains(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    original = GENERATED_ROOT / "privacy_text.mp4"
    assert original.is_file(), "run scripts/generate_test_videos.py --force first"
    original_hash = compute_file_sha256(original)
    source = tmp_path / "杈撳叆 绌烘牸 涓枃 眉 鏂囨湰.mp4"
    shutil.copy2(original, source)
    source_hash = compute_file_sha256(source)
    assert source_hash == original_hash
    output = tmp_path / "杈撳嚭 绌烘牸 涓枃 惟 text"
    entry = _privacy_manifest()[original.name]
    cases = cast(list[dict[str, object]], entry["text_cases"])
    scanner = _TemplateTextScannerRunner(cases)

    result = _run_real_safe_sharing(
        source,
        output,
        original.name,
        ffmpeg,
        ffprobe,
        scanner_runner=scanner,
        text_rescanner=_TemplateTextRescanner(scanner),
        audience="public",
    )

    assert result.status is PrivacyJobOutcome.COMPLETED, _nonpassing_checks(result)
    assert compute_file_sha256(original) == original_hash
    assert compute_file_sha256(source) == source_hash
    candidate = output / result.video_relative_path
    ordinary = next(case for case in cases if case["sensitive"] is False)
    box = NormalizedBox.model_validate(ordinary["box"])
    source_frame = _read_frame(source, 3.5)
    candidate_frame = _read_frame(candidate, 3.5)
    x_min, y_min, x_max, y_max = _pixel_bounds(box, source_frame)
    ordinary_difference = _cv2().absdiff(
        source_frame[y_min:y_max, x_min:x_max],
        candidate_frame[y_min:y_max, x_min:x_max],
    )
    assert float(ordinary_difference.mean()) < 8.0
    _assert_public_package_is_separate_and_path_free(
        output,
        forbidden_strings=tuple(str(case["value"]) for case in cases),
    )


def case_real_metadata_and_audio_redaction_preserve_source(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = GENERATED_ROOT / "privacy_tags_av.mp4"
    assert source.is_file(), "run scripts/generate_test_videos.py --force first"
    source_hash = compute_file_sha256(source)
    output = tmp_path / "闊抽 metadata output"
    _metadata, private_probe = probe_video_with_private_summary(
        source,
        ffprobe=ffprobe,
    )
    assert private_probe.global_tags["artist"] == "PRIVATE AUTHOR"
    assert private_probe.global_tags["comment"] == "DEVICE PRIVATE CAMERA 912"
    assert private_probe.chapter_tags[0].tags["title"] == "PRIVATE CHAPTER TITLE"
    assert private_probe.attachment_tags[0].tags == {"attachment": "attached_picture"}
    source_tags = _tag_probe_text(ffprobe, source)
    assert "PRIVATE GLOBAL TITLE" in source_tags
    assert "PRIVATE AUTHOR" in source_tags
    assert "DEVICE PRIVATE CAMERA 912" in source_tags
    assert "PRIVATE CHAPTER TITLE" in source_tags
    assert "PRIVATE VIDEO STREAM" in source_tags
    assert "PRIVATE AUDIO STREAM" in source_tags
    assert '"attached_pic": 1' in source_tags

    result = _run_real_safe_sharing(
        source,
        output,
        source.name,
        ffmpeg,
        ffprobe,
        no_visual_scan=True,
    )

    assert result.status is PrivacyJobOutcome.COMPLETED, _nonpassing_checks(result)
    assert compute_file_sha256(source) == source_hash
    assert _pcm_rms(ffmpeg, output / result.video_relative_path, 1.0, 1.0) < 0.01
    assert _pcm_rms(ffmpeg, output / result.video_relative_path, 0.2, 0.4) > 0.05
    public_tags = _tag_probe_text(ffprobe, output / result.video_relative_path)
    assert "PRIVATE GLOBAL TITLE" not in public_tags
    assert "PRIVATE AUTHOR" not in public_tags
    assert "DEVICE PRIVATE CAMERA 912" not in public_tags
    assert "PRIVATE CHAPTER TITLE" not in public_tags
    assert "PRIVATE VIDEO STREAM" not in public_tags
    assert "PRIVATE AUDIO STREAM" not in public_tags
    assert '"attached_pic": 1' not in public_tags
    _assert_public_package_is_separate_and_path_free(output)


def case_real_clean_no_risk_workflow_still_verifies_public_copy(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = GENERATED_ROOT / "privacy_clean.mp4"
    assert source.is_file(), "run scripts/generate_test_videos.py --force first"
    source_hash = compute_file_sha256(source)
    output = tmp_path / "clean share output"

    result = _run_real_safe_sharing(
        source,
        output,
        source.name,
        ffmpeg,
        ffprobe,
        clean_scan=True,
    )

    assert result.status is PrivacyJobOutcome.COMPLETED, _nonpassing_checks(result)
    assert compute_file_sha256(source) == source_hash
    assert result.verification.status is PrivacyJobOutcome.COMPLETED
    _assert_public_package_is_separate_and_path_free(output)
