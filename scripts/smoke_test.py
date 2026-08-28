"""Install a built wheel into a clean environment and exercise the public CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

EXPECTED_VERSION = "VideoScope 0.8.2"
EXPECTED_DISTRIBUTION_PREFIX = "genvideoscope-0.8.2-"
SMOKE_COMMAND_TIMEOUT_SECONDS = 1800.0
MAX_DIAGNOSTIC_CHARACTERS = 12_000
PERSONAL_PATH_PATTERNS = (
    re.compile(
        r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"']+(?:[\\/][^\r\n\"']*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9._/-])/(?:(?:Users|home)/[^/\r\n\"']+"
        r"(?:/[^\r\n\"']*)?|root(?:/[^\r\n\"']*)?)"
    ),
)
PRIVACY_PUBLIC_FILES = frozenset(
    {
        "changes.json",
        "manifest.json",
        "privacy-summary.json",
        "share-safe.mp4",
        "technical-report.json",
        "verification.json",
    }
)
RESCUE_PUBLIC_DOCUMENTS = frozenset(
    {
        "changes.json",
        "damaged-segments.json",
        "report.html",
        "rescue-plan.json",
        "technical-report.json",
        "verification-report.json",
    }
)

PRIVACY_SMOKE_DRIVER = r"""from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from videoscope.privacy.executor import NativePrivacyExecutor
from videoscope.privacy.manual import (
    ManualVisualRegionInput,
    build_manual_visual_risk,
)
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyDecision,
    PrivacyJobOutcome,
    PrivacyReviewDecision,
    PrivacyRiskType,
    RedactionStyle,
)
from videoscope.privacy.pipeline import SafeSharingConfig, SafeSharingPipeline
from videoscope.privacy.scanners import PrivacyScannerRunner
from videoscope.privacy.verification import (
    PrivacyVerifier,
    RegressionVerificationResult,
    TemporalCoverageResult,
)
from videoscope.video import compute_file_sha256


def read_frame(path: Path, timestamp: float) -> np.ndarray:
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise RuntimeError("smoke frame could not be decoded")
    return frame


class Coverage:
    def verify(self, source, candidate, action, timestamps):
        del source
        box = action.box
        if box is None:
            return TemporalCoverageResult(available=False)
        checked = []
        uncovered = []
        for timestamp in timestamps:
            frame = read_frame(candidate, timestamp)
            height, width = frame.shape[:2]
            x0 = max(0, min(width - 1, round(box.x_min * width)))
            x1 = max(x0 + 1, min(width, round(box.x_max * width)))
            y0 = max(0, min(height - 1, round(box.y_min * height)))
            y1 = max(y0 + 1, min(height, round(box.y_max * height)))
            region = frame[y0:y1, x0:x1]
            checked.append(timestamp)
            if float(region.mean()) > 8.0:
                uncovered.append(timestamp)
        return TemporalCoverageResult(
            checked_timestamps=tuple(checked),
            uncovered_timestamps=tuple(uncovered),
        )


def gray_frames(path: Path):
    capture = cv2.VideoCapture(str(path))
    fps = float(capture.get(cv2.CAP_PROP_FPS)) or 10.0
    frames = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    finally:
        capture.release()
    return fps, frames


def regression_summary(frames, fps: float, detector_id: str):
    if detector_id == "near_black":
        marked = [float(frame.mean()) < 3.0 for frame in frames]
    else:
        marked = [False]
        marked.extend(
            float(cv2.absdiff(before, after).mean()) < 0.1
            for before, after in zip(frames, frames[1:])
        )
    count = sum(1 for value in marked if value)
    return count, count / fps


class Regression:
    def compare(self, source, candidate, detector_id):
        source_fps, source_frames = gray_frames(source)
        candidate_fps, candidate_frames = gray_frames(candidate)
        for source_frame, candidate_frame in zip(source_frames, candidate_frames):
            height, width = candidate_frame.shape[:2]
            x0 = round(0.1 * width)
            x1 = round(0.8 * width)
            y0 = round(0.15 * height)
            y1 = round(0.75 * height)
            candidate_frame[y0:y1, x0:x1] = source_frame[y0:y1, x0:x1]
        before_count, before_duration = regression_summary(
            source_frames, source_fps, detector_id
        )
        after_count, after_duration = regression_summary(
            candidate_frames, candidate_fps, detector_id
        )
        return RegressionVerificationResult(
            available=True,
            before_event_count=before_count,
            before_duration_seconds=before_duration,
            after_event_count=after_count,
            after_duration_seconds=after_duration,
        )


def review_for(risk):
    if risk.risk_type is PrivacyRiskType.METADATA:
        decision = PrivacyDecision.REDACT
        style = RedactionStyle.REMOVE_METADATA
    elif risk.risk_type is PrivacyRiskType.MANUAL_VISUAL:
        decision = PrivacyDecision.REDACT
        style = RedactionStyle.SOLID_FILL
    else:
        decision = PrivacyDecision.ALLOW
        style = None
    return PrivacyReviewDecision(
        risk_id=risk.id,
        decision=decision,
        style=style,
        reviewed_at=datetime.now(timezone.utc),
    )


source = Path(sys.argv[1])
output = Path(sys.argv[2])
source_digest = compute_file_sha256(source)
manual = ManualVisualRegionInput(
    start_seconds=0.4,
    end_seconds=3.6,
    box=NormalizedBox(x_min=0.1, y_min=0.15, x_max=0.8, y_max=0.75),
    style=RedactionStyle.SOLID_FILL,
)
pipeline = SafeSharingPipeline(
    output,
    scene_detector=lambda _source, _duration: (),
    scanner_runner=PrivacyScannerRunner(()),
    executor=NativePrivacyExecutor(),
    preview_executor=NativePrivacyExecutor(),
    verifier=PrivacyVerifier(
        coverage_checker=Coverage(),
        regression_analyzer=Regression(),
    ),
)
scan = pipeline.scan(
    source=source,
    config=SafeSharingConfig(audience="family", sample_fps=5.0),
)
manual_risk = build_manual_visual_risk(scan.risk_map.input_hash, manual)
reviews = tuple(review_for(risk) for risk in (*scan.risk_map.risks, manual_risk))
reviewed = pipeline.review(
    scan.scan_id,
    reviews,
    manual_visual_regions=(manual,),
)
prepared = pipeline.prepare(reviewed.review_id)
preview = pipeline.preview(prepared.preparation_id)
if not preview.is_file():
    raise RuntimeError("private redaction preview was not created")
result = pipeline.confirm(prepared.preparation_id, prepared.plan.digest)
if result.status is not PrivacyJobOutcome.COMPLETED:
    check_statuses = ", ".join(
        f"{check.check_id}={check.status.value}"
        for check in result.verification.checks
    )
    raise RuntimeError(
        f"privacy verification status was {result.status.value}; "
        f"checks: {check_statuses or 'none'}"
    )
if compute_file_sha256(source) != source_digest:
    raise RuntimeError("Safe Sharing modified the source video")
print(f"privacy-plan-digest={prepared.plan.digest}")
print("privacy-status=completed")
"""

RESCUE_SMOKE_DRIVER = r"""from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from videoscope.rescue.models import (
    RescueActionKind,
    RescueConfirmation,
    RescueStrategy,
    RescueVerificationStatus,
)
from videoscope.rescue.pipeline import RescueConfig, VideoRescuePipeline


IMPROVEMENT_KINDS = {
    RescueActionKind.ADJUST_LUMA,
    RescueActionKind.DENOISE_VIDEO,
    RescueActionKind.SHARPEN,
    RescueActionKind.DEFLICKER,
    RescueActionKind.STABILIZE,
    RescueActionKind.NORMALIZE_AUDIO,
    RescueActionKind.DENOISE_AUDIO,
    RescueActionKind.CORRECT_FIXED_AV_OFFSET,
}


def safe_measurement(value: object) -> object:
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))[:32]
        return {str(key): safe_measurement(item) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [safe_measurement(item) for item in value[:32]]
    if isinstance(value, str):
        bounded = value[:200] + ("..." if len(value) > 200 else "")
        normalized = bounded.replace("\\", "/")
        for pattern in (
            re.compile(r"(?i)(?<![A-Za-z0-9._-])[A-Z]:/[^,\r\n\"']+"),
            re.compile(r"(?<![A-Za-z0-9._-])//[^,\r\n\"']+"),
            re.compile(r"(?<![A-Za-z0-9._/-])/[^,\r\n\"']+"),
        ):
            normalized = pattern.sub("<absolute-path>", normalized)
        return normalized
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<unsupported-{type(value).__name__}>"


def verification_diagnostic(report: object, artifact: str) -> str:
    selected = [
        {
            "check_id": check.check_id,
            "status": check.status.value,
            "required": check.required,
            "measured": safe_measurement(check.measured),
        }
        for check in report.checks
        if check.artifact == artifact
    ][:32]
    payload = {"artifact": artifact, "checks": selected}
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    if len(encoded) <= 8000:
        return encoded
    fallback = {
        "artifact": artifact,
        "checks": [
            {
                "check_id": check["check_id"],
                "status": check["status"],
                "required": check["required"],
            }
            for check in selected
        ],
        "measurements_omitted": True,
    }
    return json.dumps(
        fallback, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(source: Path, output: Path, strategy: RescueStrategy) -> None:
    pipeline = VideoRescuePipeline(
        RescueConfig(output_directory=output, strategy=strategy)
    )
    preparation = pipeline.prepare(source)
    accepted = tuple(
        action.id for action in preparation.plan.actions if action.requires_confirmation
    )
    trimmed = tuple(
        damage_id
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.TRIM_DAMAGED_EDGES
        and action.id in accepted
        for damage_id in action.parameters.get("damage_ids", ())
        if isinstance(damage_id, str)
    )
    publish_improved = any(
        action.id in accepted and action.kind in IMPROVEMENT_KINDS
        for action in preparation.plan.actions
    )
    confirmation = RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=publish_improved,
        accepted_action_ids=accepted,
        accepted_trim_damage_ids=trimmed,
    )
    pipeline.confirm(preparation, confirmation)
    result = pipeline.execute(preparation, confirmation)
    if result.verification is None:
        raise RuntimeError("Rescue did not return verification")
    if result.verification.faithful_status is not RescueVerificationStatus.PASSED:
        diagnostic = verification_diagnostic(result.verification, "faithful")
        raise RuntimeError(
            f"faithful Rescue verification did not pass; verification={diagnostic}"
        )
    if strategy is RescueStrategy.BALANCED:
        if not publish_improved:
            raise RuntimeError(
                "Balanced fixture produced no evidence-backed improvement"
            )
        if result.verification.improved_status is not RescueVerificationStatus.PASSED:
            diagnostic = verification_diagnostic(result.verification, "improved")
            raise RuntimeError(
                f"improved Rescue verification did not pass; verification={diagnostic}"
            )


source = Path(sys.argv[1])
root = Path(sys.argv[2])
source_digest = digest(source)
run(source, root / "conservative", RescueStrategy.CONSERVATIVE)
run(source, root / "balanced", RescueStrategy.BALANCED)
if digest(source) != source_digest:
    raise RuntimeError("Video Rescue modified the source video")
print("rescue-status=confirmed-conservative-and-balanced")
"""

CONTENT_SMOKE_DRIVER = r"""from __future__ import annotations

import shutil
import sys
from pathlib import Path

from videoscope.content import (
    ContentConfig,
    ContentGoal,
    ContentPipelineConfig,
    ContentPipelineDependencies,
    ContentStatus,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    LongVideoContentPipeline,
    StructuralFeatureConfig,
    make_user_range_id,
)
from videoscope.content.executor import NativeContentExecutor, probe_content_duration
from videoscope.content.preview import ContentPreviewBuilder
from videoscope.video import compute_file_sha256


def user_range(source_hash, kind, start, end, label):
    interval = ContentTimeRange(start_seconds=start, end_seconds=end)
    return ContentUserRange(
        id=make_user_range_id(source_hash, kind, interval),
        kind=kind,
        source_range=interval,
        label=label,
    )


def run(source, root, goal, ranges):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg tools are unavailable")
    pipeline = LongVideoContentPipeline(
        ContentPipelineConfig(
            output_directory=root,
            content=ContentConfig(
                goal=goal,
                minimum_chapter_duration_seconds=1,
                export_clips=goal is ContentGoal.SELECTED_CLIPS,
            ),
            features=StructuralFeatureConfig(ffmpeg=ffmpeg, ffprobe=ffprobe),
            user_ranges=ranges,
        ),
        dependencies=ContentPipelineDependencies(
            preview_builder=ContentPreviewBuilder(ffmpeg_executable=ffmpeg),
            executor=NativeContentExecutor(
                ffmpeg=ffmpeg,
                duration_probe=lambda path: probe_content_duration(
                    path, ffprobe=ffprobe
                ),
            ),
        ),
    )
    prepared = pipeline.prepare(source)
    review = pipeline.preview(prepared)
    accepted = tuple(
        action.id
        for action in review.plan.actions
        if action.changes_content and action.requires_confirmation
    )
    result = pipeline.execute(
        review,
        pipeline.confirm(review, accepted_action_ids=accepted),
    )
    if result.status is not ContentStatus.COMPLETED or result.public_root is None:
        raise RuntimeError(f"content goal did not complete: {goal.value}")
    if not (result.public_root / "technical-report.json").is_file():
        raise RuntimeError("content technical report is missing")


source = Path(sys.argv[1])
root = Path(sys.argv[2])
source_hash = compute_file_sha256(source)
ffprobe = shutil.which("ffprobe")
if not ffprobe:
    raise RuntimeError("ffprobe is unavailable")
duration = probe_content_duration(source, ffprobe=ffprobe)
if duration < 3:
    raise RuntimeError("content smoke input must be at least three seconds long")
third = duration / 3
sixth = duration / 6
run(
    source,
    root / "faithful-clean",
    ContentGoal.FAITHFUL_CLEAN,
    (
        user_range(
            source_hash,
            ContentUserRangeKind.EXCLUDE,
            third,
            duration / 2,
            "Remove",
        ),
        user_range(
            source_hash,
            ContentUserRangeKind.LOCKED_KEEP,
            duration * 0.4,
            duration * 0.45,
            "Lock",
        ),
    ),
)
run(
    source,
    root / "chaptered-full",
    ContentGoal.CHAPTERED_FULL,
    tuple(
        user_range(source_hash, ContentUserRangeKind.CHAPTER, start, end, label)
        for start, end, label in (
            (0, third, "Start"),
            (third, third * 2, "Middle"),
            (third * 2, duration, "End"),
        )
    ),
)
run(
    source,
    root / "selected-clips",
    ContentGoal.SELECTED_CLIPS,
    tuple(
        user_range(source_hash, ContentUserRangeKind.KEEP, start, end, label)
        for start, end, label in (
            (0, sixth, "One"),
            (third, duration / 2, "Two"),
            (third * 2, sixth * 5, "Three"),
        )
    ),
)
print("content-status=faithful-clean-chaptered-full-selected-clips")
"""


class SmokeTestError(RuntimeError):
    """Actionable wheel smoke-test failure."""


def select_wheel(dist: Path) -> Path:
    """Select the single release-candidate wheel from a distribution directory."""
    wheels = sorted(dist.glob(f"{EXPECTED_DISTRIBUTION_PREFIX}*.whl"))
    if len(wheels) != 1:
        raise SmokeTestError(
            f"Expected exactly one {EXPECTED_DISTRIBUTION_PREFIX} wheel in {dist}; "
            f"found {len(wheels)}."
        )
    return wheels[0].resolve()


def environment_python(environment: Path) -> Path:
    """Return the virtual environment interpreter on the current platform."""
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def run_command(
    args: list[str],
    *,
    cwd: Path,
    label: str,
    timeout_seconds: float = SMOKE_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one smoke-test command without a shell and require success."""
    print(f"==> {label}", flush=True)
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeTestError(
            f"{label} timed out after {timeout_seconds:g} seconds."
        ) from exc
    if completed.stdout:
        print(_safe_diagnostic(completed.stdout, cwd).rstrip())
    if completed.stderr:
        print(_safe_diagnostic(completed.stderr, cwd).rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise SmokeTestError(f"{label} exited with status {completed.returncode}.")
    return completed


def _safe_diagnostic(value: str, cwd: Path) -> str:
    """Bound child diagnostics and remove the private smoke workspace path."""
    sanitized = value
    try:
        resolved = cwd.resolve()
    except OSError:
        resolved = cwd
    for private in (str(resolved), resolved.as_posix()):
        if private:
            sanitized = sanitized.replace(private, "<smoke-workspace>")
    for pattern in PERSONAL_PATH_PATTERNS:
        sanitized = pattern.sub("<private-path>", sanitized)
    if len(sanitized) > MAX_DIAGNOSTIC_CHARACTERS:
        sanitized = sanitized[:MAX_DIAGNOSTIC_CHARACTERS] + "\n[diagnostic truncated]"
    return sanitized


def _contains_personal_path(value: object) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in PERSONAL_PATH_PATTERNS)
    if isinstance(value, dict):
        return any(
            _contains_personal_path(key) or _contains_personal_path(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_personal_path(item) for item in value)
    return False


def validate_verified_privacy_package(output: Path) -> None:
    """Require one completed manual-region package with full visual coverage."""
    public_root = output / "share-package"
    if not public_root.is_dir():
        raise SmokeTestError("Safe Sharing did not create share-package.")
    entries = tuple(public_root.iterdir())
    names = {path.name for path in entries}
    missing = sorted(PRIVACY_PUBLIC_FILES - names)
    unexpected = sorted(names - PRIVACY_PUBLIC_FILES)
    invalid_entries = sorted(
        path.name for path in entries if path.is_symlink() or not path.is_file()
    )
    if missing or unexpected or invalid_entries:
        raise SmokeTestError(
            "Safe Sharing public package mismatch; "
            f"missing={missing!r}, unexpected={unexpected!r}, "
            f"invalid_entries={invalid_entries!r}."
        )
    try:
        payload = json.loads(
            (public_root / "verification.json").read_text(encoding="utf-8")
        )
        status = payload["status"]
        checks = payload["checks"]
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise SmokeTestError("Safe Sharing verification report is unreadable.") from exc
    if status != "completed":
        raise SmokeTestError(
            f"Safe Sharing verification was not completed: {status!r}."
        )
    coverage = next(
        (
            item
            for item in checks
            if isinstance(item, dict) and item.get("check_id") == "visual_coverage"
        ),
        None,
    )
    if not isinstance(coverage, dict) or coverage.get("status") != "passed":
        raise SmokeTestError("Safe Sharing visual coverage did not pass.")
    measured = coverage.get("measured")
    if (
        not isinstance(measured, dict)
        or measured.get("actions") != 1
        or not isinstance(measured.get("checked_samples"), int)
        or measured["checked_samples"] <= 0
        or measured.get("missing_samples") != 0
        or measured.get("uncovered_samples") != 0
    ):
        raise SmokeTestError("Safe Sharing visual coverage evidence is incomplete.")
    forbidden = (
        "privacy-review-private",
        "private_evidence",
        str(output.resolve()),
        output.resolve().as_posix(),
    )
    for path in public_root.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        try:
            public_payload = json.loads(text)
        except ValueError as exc:
            raise SmokeTestError(
                f"Safe Sharing public JSON is invalid: {path.name}."
            ) from exc
        if any(
            value and value.casefold() in text.casefold() for value in forbidden
        ) or _contains_personal_path(public_payload):
            raise SmokeTestError(
                f"Safe Sharing public JSON contains private data: {path.name}."
            )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_verified_rescue_package(output: Path, *, require_improved: bool) -> None:
    """Require independently verified faithful and optional improved media."""
    public_root = output / "rescue-output"
    if not public_root.is_dir():
        raise SmokeTestError("Video Rescue did not create rescue-output.")
    faithful = public_root / "faithful-rescue.mp4"
    improved = public_root / "improved-viewing.mp4"
    if not faithful.is_file():
        raise SmokeTestError("Video Rescue did not create faithful-rescue.mp4.")
    if require_improved:
        if not improved.is_file():
            raise SmokeTestError("Balanced Rescue did not create improved-viewing.mp4.")
        if _file_sha256(faithful) == _file_sha256(improved):
            raise SmokeTestError(
                "Balanced Rescue outputs are not independent encoded artifacts."
            )
    elif improved.exists():
        raise SmokeTestError("Conservative Rescue unexpectedly created improved media.")

    expected_names = set(RESCUE_PUBLIC_DOCUMENTS)
    expected_names.add("faithful-rescue.mp4")
    if require_improved:
        expected_names.add("improved-viewing.mp4")
    entries = tuple(public_root.iterdir())
    names = {path.name for path in entries}
    invalid_entries = sorted(
        path.name for path in entries if path.is_symlink() or not path.is_file()
    )
    if names != expected_names or invalid_entries:
        raise SmokeTestError(
            "Video Rescue public package mismatch; "
            f"missing={sorted(expected_names - names)!r}, "
            f"unexpected={sorted(names - expected_names)!r}, "
            f"invalid_entries={invalid_entries!r}."
        )
    try:
        payload = json.loads(
            (public_root / "verification-report.json").read_text(encoding="utf-8")
        )
        faithful_status = payload["faithful_status"]
        improved_status = payload["improved_status"]
        roles = {
            item["relative_path"]: item["artifact_role"]
            for item in payload["artifacts"]
            if isinstance(item, dict)
        }
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise SmokeTestError("Video Rescue verification report is unreadable.") from exc
    if faithful_status != "passed" or roles.get("faithful-rescue.mp4") != "faithful":
        raise SmokeTestError("Faithful Rescue verification did not pass.")
    if require_improved and (
        improved_status != "passed" or roles.get("improved-viewing.mp4") != "improved"
    ):
        raise SmokeTestError("Improved Rescue verification did not pass.")
    if not require_improved and improved_status is not None:
        raise SmokeTestError("Conservative Rescue reported an improved artifact.")


def prepare_smoke_inputs(
    *,
    root: Path,
    requested_video: Path | None,
) -> tuple[Path, Path, Path]:
    """Prepare purpose-built local inputs for each public workflow."""
    publish_video = root / "publish-input.mp4"
    privacy_video = root / "privacy-input.mp4"
    rescue_video = root / "rescue-input.mp4"

    if requested_video is not None:
        shutil.copy2(requested_video.resolve(strict=True), publish_video)
    else:
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=10:duration=6",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=6",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-pix_fmt",
                "yuv420p",
                "-shortest",
                "-y",
                str(publish_video),
            ],
            cwd=root,
            label="Generate Publish smoke fixture",
        )

    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=4",
            "-an",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            "-map_metadata",
            "-1",
            "-y",
            str(privacy_video),
        ],
        cwd=root,
        label="Generate Safe Sharing smoke fixture",
    )
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-vf",
            # Keep the guarded lift clear of both luma qualification bounds
            # across one-code-value differences between FFmpeg builds.
            "eq=brightness=-0.47,noise=alls=22:allf=t:all_seed=42",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-flags:a",
            "+bitexact",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "10",
            "-t",
            "6",
            "-g",
            "1",
            "-shortest",
            "-threads",
            "1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-map_metadata",
            "-1",
            "-metadata",
            "creation_time=1970-01-01T00:00:00Z",
            "-movflags",
            "+faststart",
            "-video_track_timescale",
            "10000",
            "-y",
            str(rescue_video),
        ],
        cwd=root,
        label="Generate Video Rescue smoke fixture",
    )
    return publish_video, privacy_video, rescue_video


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=Path("dist"),
        help="Directory containing the wheel.",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="Exact wheel to install instead of selecting one from --dist.",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=None,
        help=(
            "Optional local Publish fixture; Safe Sharing and Video Rescue use "
            "purpose-built local FFmpeg fixtures."
        ),
    )
    parser.add_argument(
        "--ffmpeg-bin",
        type=Path,
        default=None,
        help="Directory containing ffmpeg and ffprobe when PATH is unavailable.",
    )
    parser.add_argument(
        "--offline-installed-dependencies",
        action="store_true",
        help=(
            "Create the temporary environment with already-installed base "
            "dependencies and install the candidate wheel with --no-index "
            "--no-deps. This is an offline local smoke, not a clean dependency "
            "resolution check."
        ),
    )
    return parser.parse_args()


def wheel_install_command(
    python: Path, wheel: Path, *, offline_installed_dependencies: bool
) -> list[str]:
    """Build the explicit wheel-install command for the chosen smoke mode."""
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    ]
    if offline_installed_dependencies:
        command.extend(["--no-index", "--no-deps"])
    command.append(str(wheel))
    return command


def installed_dependency_path() -> str:
    """Return only installed package roots for the explicit offline smoke mode."""
    package_roots: list[str] = []
    for raw_path in sys.path:
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.name.casefold() not in {"site-packages", "dist-packages"}:
            continue
        resolved = str(path.resolve(strict=True))
        if resolved not in package_roots:
            package_roots.append(resolved)
    if not package_roots:
        raise SmokeTestError(
            "Offline smoke mode could not find installed dependency packages."
        )
    return os.pathsep.join(package_roots)


def isolate_smoke_cache(root: Path) -> None:
    """Keep smoke-test cache probes inside the disposable test workspace."""
    os.environ["LOCALAPPDATA"] = str(root / "local-app-data")
    os.environ["XDG_CACHE_HOME"] = str(root / "cache")
    os.environ["VIDEOSCOPE_CACHE_DIR"] = str(root / "videoscope-cache")


def main() -> int:
    """Run the clean-environment installation and CLI smoke test."""
    args = parse_args()
    try:
        ffmpeg_bin = getattr(args, "ffmpeg_bin", None)
        if ffmpeg_bin is not None:
            tool_directory = ffmpeg_bin.resolve(strict=True)
            executable_suffix = ".exe" if sys.platform == "win32" else ""
            for tool in ("ffmpeg", "ffprobe"):
                if not (tool_directory / f"{tool}{executable_suffix}").is_file():
                    raise SmokeTestError(
                        f"--ffmpeg-bin does not contain required {tool}."
                    )
            os.environ["PATH"] = (
                str(tool_directory) + os.pathsep + os.environ.get("PATH", "")
            )
        wheel = (
            args.wheel.resolve(strict=True)
            if args.wheel is not None
            else select_wheel(args.dist)
        )
        offline_installed_dependencies = bool(
            getattr(args, "offline_installed_dependencies", False)
        )
        if offline_installed_dependencies:
            os.environ["PYTHONPATH"] = installed_dependency_path()
        with tempfile.TemporaryDirectory(prefix="videoscope-smoke-") as temporary:
            root = Path(temporary)
            isolate_smoke_cache(root)
            environment = root / "venv"
            output = root / "publish-smoke"
            privacy_output = root / "privacy-smoke"
            rescue_output = root / "rescue-smoke"
            content_output = root / "content-smoke"
            check_output = root / "check-smoke"
            publish_video, privacy_video, rescue_video = prepare_smoke_inputs(
                root=root,
                requested_video=args.video,
            )
            venv.EnvBuilder(
                with_pip=True,
                clear=True,
            ).create(environment)
            python = environment_python(environment)

            run_command(
                wheel_install_command(
                    python,
                    wheel,
                    offline_installed_dependencies=offline_installed_dependencies,
                ),
                cwd=root,
                label="Install wheel",
            )
            version = run_command(
                [str(python), "-m", "videoscope", "--version"],
                cwd=root,
                label="Check version",
            )
            if EXPECTED_VERSION not in version.stdout:
                raise SmokeTestError(
                    f"Unexpected version output: {version.stdout.strip()!r}"
                )
            run_command(
                [str(python), "-m", "videoscope", "doctor"],
                cwd=root,
                label="Run doctor",
            )
            run_command(
                [
                    str(python),
                    "-m",
                    "videoscope",
                    "analyze",
                    str(publish_video),
                    "--output",
                    str(check_output),
                    "--json-only",
                    "--quiet",
                ],
                cwd=root,
                label="Run CPU Check workflow",
            )
            if not (check_output / "report.json").is_file():
                raise SmokeTestError("Check did not create report.json.")
            run_command(
                [
                    str(python),
                    "-m",
                    "videoscope",
                    "publish",
                    str(publish_video),
                    "--profile",
                    "compatible_mp4",
                    "--output",
                    str(output),
                    "--yes",
                ],
                cwd=root,
                label="Publish compatible fixture",
            )
            required_outputs = (
                output / "publish-ready.mp4",
                output / "cover.jpg",
                output / "changes.json",
                output / "technical-report.json",
            )
            missing = [path.name for path in required_outputs if not path.is_file()]
            if missing:
                raise SmokeTestError(
                    "Publish did not create required outputs: " + ", ".join(missing)
                )
            try:
                technical_report = json.loads(
                    (output / "technical-report.json").read_text(encoding="utf-8")
                )
                verification_status = technical_report["verification"]["status"]
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise SmokeTestError(
                    "Publish technical report has no readable verification status."
                ) from exc
            if verification_status != "passed":
                raise SmokeTestError(
                    f"Publish verification did not pass: {verification_status!r}."
                )
            driver = root / "privacy_smoke_driver.py"
            driver.write_text(PRIVACY_SMOKE_DRIVER, encoding="utf-8", newline="\n")
            run_command(
                [str(python), str(driver), str(privacy_video), str(privacy_output)],
                cwd=root,
                label="Run manual-region Safe Sharing lifecycle",
            )
            validate_verified_privacy_package(privacy_output)
            rescue_driver = root / "rescue_smoke_driver.py"
            rescue_driver.write_text(
                RESCUE_SMOKE_DRIVER, encoding="utf-8", newline="\n"
            )
            run_command(
                [
                    str(python),
                    str(rescue_driver),
                    str(rescue_video),
                    str(rescue_output),
                ],
                cwd=root,
                label="Run confirmed Conservative and Balanced Video Rescue",
            )
            validate_verified_rescue_package(
                rescue_output / "conservative", require_improved=False
            )
            validate_verified_rescue_package(
                rescue_output / "balanced", require_improved=True
            )
            content_driver = root / "content_smoke_driver.py"
            content_driver.write_text(
                CONTENT_SMOKE_DRIVER, encoding="utf-8", newline="\n"
            )
            run_command(
                [
                    str(python),
                    str(content_driver),
                    str(publish_video),
                    str(content_output),
                ],
                cwd=root,
                label="Run three confirmed useful-content goals",
            )
    except (OSError, SmokeTestError) as exc:
        diagnostic = _safe_diagnostic(str(exc), Path.cwd())
        print(f"Smoke test failed: {diagnostic}", file=sys.stderr)
        return 1

    print(
        "PASS clean base-wheel installation, CPU Check, Publish Ready, and verified "
        "manual-region Safe Sharing, confirmed Video Rescue, and three "
        "confirmed useful-content goals"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
