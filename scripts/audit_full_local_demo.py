"""Audit the full-local A/B/C/D demo without promoting review states."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from scripts.full_local_demo_contract import (
    DemoContract,
    canonical_json_bytes,
    safe_relative_path,
    stream_sha256,
)
from scripts.validate_full_local_demo import WorkflowOutcome

SCHEMA_VERSION: Literal["1"] = "1"
TOOL_VERSION = "1.0"
WORKFLOW_IDS = ("publish_ready", "video_rescue", "useful_content", "safe_sharing")
HERO_TIMESTAMPS = (2.5, 7.5, 15.0, 22.5, 28.5, 34.0, 39.0)
HERO_LABELS = (
    "Clean hook / 清晰开场",
    "Rescue evidence / 抢救证据",
    "Tutorial / 教程",
    "Review pause / 复核停顿",
    "Privacy zone / 隐私区",
    "Motion retake / 运动重拍",
    "Verified ending / 验证结尾",
)
FORBIDDEN_PUBLIC_METADATA = {
    "artist",
    "author",
    "comment",
    "creation_time",
    "description",
    "encoded_by",
    "filename",
    "location",
    "software",
    "title",
}
SECRET_KEY = re.compile(r"(?:api[_-]?key|token|password|secret)", re.IGNORECASE)
SECRET_ASSIGNMENT = re.compile(
    r"(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+", re.IGNORECASE
)
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")


class AuditError(RuntimeError):
    """Raised when the requested audit cannot be performed safely."""


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AuditWorkflow(_AuditModel):
    workflow_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    checks: tuple[dict[str, JsonValue], ...] = ()
    actions: tuple[dict[str, JsonValue], ...] = ()
    limitations: tuple[str, ...] = ()
    artifacts: dict[str, str] = Field(default_factory=dict)
    source_unchanged: bool
    final_human_review_required: bool = False

    @model_validator(mode="after")
    def validate_public_values(self) -> AuditWorkflow:
        for name, value in self.artifacts.items():
            _validate_public_key(name)
            _validate_relative_path(value)
        _reject_private_values(self.checks)
        _reject_private_values(self.actions)
        _reject_private_values(self.limitations)
        return self


class AuditSummary(_AuditModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    tool_version: str = TOOL_VERSION
    environment_versions: dict[str, str] = Field(default_factory=dict)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_generation_status: str
    workflows: dict[str, AuditWorkflow]
    overall_status: str
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_public_summary(self) -> AuditSummary:
        _reject_private_values(self.model_dump(mode="json"))
        return self


def hero_timestamps(contract: DemoContract) -> tuple[float, ...]:
    """Return the fixed midpoint of every approved scene."""
    timestamps = tuple(
        round((scene.start_seconds + scene.end_seconds) / 2.0, 3)
        for scene in contract.scenes
    )
    if timestamps != HERO_TIMESTAMPS:
        raise AuditError("demo scenes do not match the approved hero timestamps")
    return timestamps


def assemble_summary(outcomes: Sequence[WorkflowOutcome]) -> AuditSummary:
    """Assemble truthful workflow states from already-bound outcomes."""
    workflows = {item.workflow_id: _audit_workflow(item) for item in outcomes}
    overall = _overall_status(workflows)
    source_hash = outcomes[0].source_sha256_before if outcomes else "0" * 64
    return AuditSummary(
        source_sha256=source_hash,
        contract_digest="0" * 64,
        deterministic_generation_status="not_verified",
        workflows=workflows,
        overall_status=overall,
        limitations=("This summary has not been bound to a demo manifest.",),
    )


def build_contact_sheet(video: Path, timestamps: Sequence[float], output: Path) -> Path:
    """Extract exact frames and atomically build a deterministic 4x2 sheet."""
    if not video.is_file():
        raise AuditError("contact-sheet input is unavailable")
    if len(timestamps) > 7 or not timestamps:
        raise AuditError("contact sheet requires between one and seven timestamps")
    ffmpeg = os.environ.get("VIDEOSCOPE_FFMPEG", "ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".contact-sheet-", dir=output.parent
    ) as raw:
        staging = Path(raw)
        frames: list[Path] = []
        for index, timestamp in enumerate(timestamps):
            frame = staging / f"frame-{index:02d}.png"
            _run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
                    "-y",
                    str(frame),
                ]
            )
            if not frame.is_file():
                raise AuditError("FFmpeg did not create a contact-sheet frame")
            frames.append(frame)
        sheet = Image.new("RGB", (1280, 440), "#0B0E10")
        draw = ImageDraw.Draw(sheet)
        font = _font(16)
        for index, frame in enumerate(frames):
            x = (index % 4) * 320
            y = (index // 4) * 220
            with Image.open(frame) as source:
                sheet.paste(source.convert("RGB"), (x, y))
            label = HERO_LABELS[index] if len(frames) == 7 else f"Frame {index + 1}"
            draw.text(
                (x + 8, y + 186),
                f"{timestamps[index]:05.1f}s  {label}",
                fill="#F1F4EF",
                font=font,
            )
        legend_x, legend_y = 960, 220
        draw.rectangle((legend_x, legend_y, 1279, 439), fill="#151A1D")
        draw.text(
            (legend_x + 18, legend_y + 24),
            "VideoScope local audit",
            fill="#56E0D0",
            font=font,
        )
        draw.text(
            (legend_x + 18, legend_y + 58),
            "Seven fixed source scenes",
            fill="#F1F4EF",
            font=font,
        )
        draw.text(
            (legend_x + 18, legend_y + 86), "七个固定源场景", fill="#F1F4EF", font=font
        )
        temporary = staging / "contact-sheet.webp"
        sheet.save(temporary, format="WEBP", quality=90, method=6)
        os.replace(temporary, output)
    return output


def audit_source_and_results(root: Path) -> AuditSummary:
    """Audit a bound source manifest and its current A/B/C/D outcomes."""
    root = root.resolve()
    manifest = _read_object(root / "demo-manifest.json", "demo manifest")
    outcomes_document = _read_object(
        root / "execution-outcomes.json", "execution outcomes"
    )
    source_info = _mapping(manifest.get("source"), "manifest source")
    contract_info = _mapping(manifest.get("contract"), "manifest contract")
    source_path = _contained(root, _string(source_info.get("path"), "source path"))
    source_hash = _string(source_info.get("sha256"), "source hash")
    contract_digest = _string(contract_info.get("sha256"), "contract hash")
    contract_path = _string(contract_info.get("path"), "contract path")
    _verify_contract_digest(root, contract_path, contract_digest)
    if stream_sha256(source_path) != source_hash:
        raise AuditError("source hash does not match the demo manifest")
    if outcomes_document.get("source_sha256") != source_hash:
        raise AuditError("execution outcomes are not bound to the source")
    if outcomes_document.get("contract_sha256") != contract_digest:
        raise AuditError("execution outcomes are not bound to the contract")
    raw_outcomes = _mapping(outcomes_document.get("outcomes"), "workflow outcomes")
    workflows: dict[str, AuditWorkflow] = {}
    for workflow_id in WORKFLOW_IDS:
        raw = raw_outcomes.get(workflow_id)
        if raw is None:
            workflows[workflow_id] = AuditWorkflow(
                workflow_id=workflow_id,
                status="unavailable",
                source_unchanged=True,
                final_human_review_required=True,
                limitations=("No bound execution outcome was available.",),
            )
            continue
        outcome = WorkflowOutcome.model_validate(raw)
        workflows[workflow_id] = _audit_bound_workflow(
            root, outcome, source_hash=source_hash
        )
    deterministic = _deterministic_generation_status(root, source_path)
    tools = _mapping(manifest.get("tools", {}), "manifest tools")
    environment = {"python": platform.python_version()}
    for name in ("generator", "hyperframes", "ffmpeg", "ffprobe"):
        value = tools.get(name)
        if isinstance(value, str):
            environment[name] = value.splitlines()[0]
    limitations = tuple(
        limitation
        for workflow in workflows.values()
        for limitation in workflow.limitations
    )
    summary = AuditSummary(
        environment_versions=environment,
        source_sha256=source_hash,
        contract_digest=contract_digest,
        deterministic_generation_status=deterministic,
        workflows=workflows,
        overall_status=_overall_status(workflows, deterministic=deterministic),
        limitations=tuple(dict.fromkeys(limitations)),
    )
    _reject_private_values(summary.model_dump(mode="json"))
    return summary


def write_verification_summary(summary: AuditSummary, path: Path) -> None:
    """Atomically write canonical UTF-8 JSON after public-value validation."""
    payload = summary.model_dump(mode="json")
    _reject_private_values(payload)
    _atomic_write(path, canonical_json_bytes(cast(Mapping[str, object], payload)))


def render_beginner_guide(summary: AuditSummary, template: Path, output: Path) -> None:
    """Render a zero-beginner guide without claiming unverified success."""
    source = template.read_text(encoding="utf-8")
    rows = [
        "| 模块 / Module | 实际状态 / Actual status | 人工复核 / Human review |",
        "|---|---|---|",
    ]
    for workflow_id in WORKFLOW_IDS:
        workflow = summary.workflows.get(workflow_id)
        if workflow is None:
            continue
        review = (
            "需要 / Required" if workflow.final_human_review_required else "否 / No"
        )
        rows.append(f"| `{workflow_id}` | `{workflow.status}` | {review} |")
    steps = "\n".join(
        (
            "1. 使用生成的源视频，不要覆盖源文件。 / "
            "Use the generated source; never overwrite it.",
            "2. 每次只打开一个模块。 / Open one module at a time.",
            "3. 查看准备好的计划与预览。 / Review the prepared plan and preview.",
            "4. 只确认页面显示的 digest 与 action IDs。 / "
            "Confirm only the displayed digest and action IDs.",
            "5. 将独立输出与源视频逐项比较。 / "
            "Compare each separate output with the source.",
            "6. 分享前阅读 `needs_review`、`not_verified` 和限制，并完成人工复核。 / "
            "Read review states and limitations before sharing.",
        )
    )
    limitations = (
        "\n".join(f"- {item}" for item in summary.limitations)
        or "- 无新增全局限制。 / No additional global limitation."
    )
    rendered = source.format(
        title="VideoScope 完整本地四模式演示 / Full Local Four-Mode Demo",
        workflow_table="\n".join(rows),
        steps=steps,
        limitations=limitations,
    )
    _reject_private_values(rendered)
    _atomic_write(output, rendered.encode("utf-8"))


def _audit_workflow(outcome: WorkflowOutcome) -> AuditWorkflow:
    unchanged = outcome.source_sha256_before == outcome.source_sha256_after
    return AuditWorkflow(
        workflow_id=outcome.workflow_id,
        status=outcome.status,
        checks=outcome.checks,
        actions=outcome.actions,
        limitations=outcome.limitations,
        artifacts=outcome.artifacts,
        source_unchanged=unchanged,
        final_human_review_required=outcome.final_human_review_required,
    )


def _audit_bound_workflow(
    root: Path, outcome: WorkflowOutcome, *, source_hash: str
) -> AuditWorkflow:
    unchanged = (
        outcome.source_sha256_before == source_hash
        and outcome.source_sha256_after == source_hash
    )
    checks = list(outcome.checks)
    limitations = list(outcome.limitations)
    artifacts: dict[str, str] = {}
    missing: list[str] = []
    for name, relative in outcome.artifacts.items():
        resolved = _resolve_artifact(root, outcome.workflow_id, relative)
        if resolved is None or not resolved.is_file():
            missing.append(name)
            continue
        public_path = safe_relative_path(resolved, root)
        artifacts[name] = public_path
        checks.append(
            {
                "check_id": f"artifact_{name}",
                "status": "passed",
                "sha256": stream_sha256(resolved),
            }
        )
        if resolved.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            checks.append(_probe_media_check(resolved, name))
    status = outcome.status
    if outcome.workflow_id == "safe_sharing" and not artifacts:
        status = "not_verified"
        limitations.append(
            "No public Safe Sharing artifact exists; redaction, mute, and metadata "
            "checks are not verified."
        )
        checks.extend(_safe_sharing_not_verified_checks())
    elif outcome.workflow_id == "safe_sharing" and "video" in artifacts:
        safe_video = _contained(root, artifacts["video"])
        source_video = _contained(root, "VideoScope-Full-Local-Demo-Source.mp4")
        checks.extend(_audit_safe_sharing_media(source_video, safe_video))
        if any(item["status"] != "passed" for item in checks[-3:]):
            status = "needs_review"
            limitations.append(
                "One or more Safe Sharing media checks require human review."
            )
    elif missing:
        status = "not_verified"
        limitations.append("One or more declared public artifacts were unavailable.")
    if outcome.workflow_id == "useful_content":
        checks.append(_check_useful_source_mappings(outcome.actions))
        if checks[-1]["status"] != "passed":
            status = "not_verified"
    if outcome.workflow_id == "publish_ready" and "video" in artifacts:
        faststart = _faststart_check(_contained(root, artifacts["video"]))
        checks.append(faststart)
        if faststart["status"] != "passed":
            status = "not_verified"
    if not unchanged:
        status = "failed"
        limitations.append("Source hash changed during workflow execution.")
    workflow = AuditWorkflow(
        workflow_id=outcome.workflow_id,
        status=status,
        checks=tuple(checks),
        actions=outcome.actions,
        limitations=tuple(dict.fromkeys(limitations)),
        artifacts=artifacts,
        source_unchanged=unchanged,
        final_human_review_required=(
            outcome.final_human_review_required
            or outcome.workflow_id == "safe_sharing"
            or status != "completed"
        ),
    )
    return _apply_mandatory_checks(workflow, tuple(checks))


def _safe_sharing_not_verified_checks() -> list[dict[str, JsonValue]]:
    return [
        {
            "check_id": check_id,
            "status": "not_verified",
            "message": "No public artifact was available.",
        }
        for check_id in (
            "redaction_boundaries",
            "audio_mute_30db",
            "forbidden_metadata",
        )
    ]


def _verify_contract_digest(root: Path, relative: str, expected: str) -> None:
    candidate = _contained(root, relative)
    if not candidate.is_file():
        repository_candidate = _contained(Path.cwd().resolve(), relative)
        candidate = repository_candidate
    if not candidate.is_file():
        raise AuditError("contract hash does not match the referenced contract")
    try:
        value = _read_object(candidate, "demo contract")
    except AuditError as error:
        raise AuditError(
            "contract hash does not match the referenced contract"
        ) from error
    actual = hashlib.sha256(
        canonical_json_bytes(cast(Mapping[str, object], value))
    ).hexdigest()
    if actual != expected:
        raise AuditError("contract hash does not match the referenced contract")


def _deterministic_generation_status(root: Path, source: Path) -> str:
    first_source = root / ".fix1-first-source.mp4"
    first_manifest = root / ".fix1-first-manifest.json"
    current_manifest = root / "demo-manifest.json"
    if not all(
        path.is_file() for path in (first_source, first_manifest, current_manifest)
    ):
        return "not_verified"
    if stream_sha256(first_source) != stream_sha256(source):
        return "not_verified"
    if first_manifest.read_bytes() != current_manifest.read_bytes():
        return "not_verified"
    return "passed"


def _apply_mandatory_checks(
    workflow: AuditWorkflow,
    checks: Sequence[dict[str, JsonValue]],
) -> AuditWorkflow:
    statuses = {
        str(item.get("status"))
        for item in checks
        if item.get("required", True) is not False
    }
    status = workflow.status
    if "failed" in statuses:
        status = "failed"
    elif status == "completed" and (
        "not_verified" in statuses or "needs_review" in statuses
    ):
        status = "not_verified" if "not_verified" in statuses else "needs_review"
    if status == workflow.status and tuple(checks) == workflow.checks:
        return workflow
    return workflow.model_copy(
        update={
            "status": status,
            "checks": tuple(checks),
            "final_human_review_required": (
                workflow.final_human_review_required or status != "completed"
            ),
        }
    )


def _check_useful_source_mappings(
    actions: Sequence[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    observed: list[list[float]] = []
    for action in actions:
        source_range = action.get("source_range")
        if isinstance(source_range, dict):
            start = source_range.get("start_seconds")
            end = source_range.get("end_seconds")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                observed.append([float(start), float(end)])
    expected = [[0.0, 5.0], [10.0, 20.0], [36.0, 42.0]]
    return {
        "check_id": "confirmed_source_mappings",
        "status": "passed" if observed == expected else "not_verified",
        "observed": cast(JsonValue, observed),
        "expected": cast(JsonValue, expected),
    }


def _probe_media_check(path: Path, artifact: str) -> dict[str, JsonValue]:
    ffprobe = os.environ.get("VIDEOSCOPE_FFPROBE", "ffprobe")
    try:
        completed = _run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ]
        )
        value = json.loads(completed.stdout.decode("utf-8"))
        streams = value.get("streams", [])
        duration = float(value.get("format", {}).get("duration", 0.0))
        video = any(item.get("codec_type") == "video" for item in streams)
        status = "passed" if video and duration > 0 else "not_verified"
        return {
            "check_id": f"probe_{artifact}",
            "status": status,
            "video_stream": video,
            "duration_seconds": round(duration, 3),
        }
    except (AuditError, ValueError, json.JSONDecodeError, AttributeError):
        return {
            "check_id": f"probe_{artifact}",
            "status": "not_verified",
            "message": "Artifact could not be independently probed.",
        }


def _faststart_check(path: Path) -> dict[str, JsonValue]:
    with path.open("rb") as stream:
        data = stream.read(min(path.stat().st_size, 16 * 1024 * 1024))
    moov = data.find(b"moov")
    mdat = data.find(b"mdat")
    passed = moov >= 0 and mdat >= 0 and moov < mdat
    return {
        "check_id": "faststart_layout",
        "status": "passed" if passed else "not_verified",
        "moov_before_mdat": passed,
    }


def _audit_safe_sharing_media(
    source: Path, candidate: Path
) -> list[dict[str, JsonValue]]:
    visual = _safe_sharing_visual_check(source, candidate)
    audio = _safe_sharing_audio_check(candidate)
    metadata = _safe_sharing_metadata_check(candidate)
    return [visual, audio, metadata]


def _safe_sharing_visual_check(source: Path, candidate: Path) -> dict[str, JsonValue]:
    samples = (24.9, 25.1, 28.5, 31.9, 32.1)
    differences: list[float] = []
    try:
        for timestamp in samples:
            source_image = _extract_frame(source, timestamp)
            candidate_image = _extract_frame(candidate, timestamp)
            box = (
                round(source_image.width * 0.58),
                round(source_image.height * 0.18),
                round(source_image.width * 0.94),
                round(source_image.height * 0.78),
            )
            source_crop = source_image.crop(box)
            candidate_crop = candidate_image.crop(box)
            difference = ImageChops.difference(source_crop, candidate_crop)
            differences.append(sum(ImageStat.Stat(difference).mean) / 3.0)
        outside = (differences[0], differences[4])
        inside = differences[1:4]
        passed = max(outside) <= 8.0 and min(inside) >= 12.0
        return {
            "check_id": "redaction_boundaries",
            "status": "passed" if passed else "needs_review",
            "timestamps_seconds": cast(JsonValue, list(samples)),
            "roi_mean_absolute_differences": cast(
                JsonValue, [round(item, 3) for item in differences]
            ),
        }
    except AuditError:
        return {
            "check_id": "redaction_boundaries",
            "status": "not_verified",
            "message": "Boundary frames could not be extracted.",
        }


def _extract_frame(path: Path, timestamp: float) -> Image.Image:
    ffmpeg = os.environ.get("VIDEOSCOPE_FFMPEG", "ffmpeg")
    completed = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
    )
    import io

    try:
        with Image.open(io.BytesIO(completed.stdout)) as image:
            return image.convert("RGB")
    except OSError as error:
        raise AuditError("extracted frame was not a valid image") from error


def _safe_sharing_audio_check(path: Path) -> dict[str, JsonValue]:
    windows = ((24.0, 1.0), (25.25, 6.5), (32.0, 1.0))
    try:
        levels = [_audio_rms_dbfs(path, start, duration) for start, duration in windows]
    except AuditError:
        return {
            "check_id": "audio_mute_30db",
            "status": "not_verified",
            "message": "Audio windows could not be measured.",
        }
    muted, controls = levels[1], (levels[0], levels[2])
    passed = all(control - muted >= 30.0 for control in controls)
    return {
        "check_id": "audio_mute_30db",
        "status": "passed" if passed else "needs_review",
        "rms_dbfs": cast(JsonValue, [round(item, 3) for item in levels]),
        "minimum_control_delta_db": round(min(item - muted for item in controls), 3),
    }


def _audio_rms_dbfs(path: Path, start: float, duration: float) -> float:
    ffmpeg = os.environ.get("VIDEOSCOPE_FFMPEG", "ffmpeg")
    completed = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ]
    )
    samples = array.array("h")
    samples.frombytes(completed.stdout)
    if not samples:
        raise AuditError("audio window contained no samples")
    mean_square = sum(float(item) * float(item) for item in samples) / len(samples)
    if mean_square <= 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)


def _safe_sharing_metadata_check(path: Path) -> dict[str, JsonValue]:
    ffprobe = os.environ.get("VIDEOSCOPE_FFPROBE", "ffprobe")
    try:
        completed = _run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags:stream_tags",
                "-of",
                "json",
                str(path),
            ]
        )
        value = json.loads(completed.stdout.decode("utf-8"))
    except (AuditError, json.JSONDecodeError, UnicodeDecodeError):
        return {
            "check_id": "forbidden_metadata",
            "status": "not_verified",
            "message": "Candidate metadata could not be independently probed.",
        }
    keys: set[str] = set()
    format_value = value.get("format", {})
    if isinstance(format_value, Mapping):
        tags = format_value.get("tags", {})
        if isinstance(tags, Mapping):
            keys.update(str(key).lower() for key in tags)
    streams = value.get("streams", [])
    if isinstance(streams, Sequence):
        for stream in streams:
            if isinstance(stream, Mapping) and isinstance(stream.get("tags"), Mapping):
                keys.update(str(key).lower() for key in stream["tags"])
    forbidden = sorted(keys & FORBIDDEN_PUBLIC_METADATA)
    return {
        "check_id": "forbidden_metadata",
        "status": "passed" if not forbidden else "needs_review",
        "forbidden_keys": cast(JsonValue, forbidden),
    }


def _resolve_artifact(root: Path, workflow_id: str, relative: str) -> Path | None:
    bases = {
        "publish_ready": root / "publish-ready",
        "video_rescue": root / "video-rescue" / "rescue-output",
        "useful_content": root / "useful-content" / "content-output",
        "safe_sharing": root / "safe-sharing",
    }
    base = bases[workflow_id]
    try:
        return _contained(base, relative)
    except AuditError:
        return None


def _overall_status(
    workflows: Mapping[str, AuditWorkflow], *, deterministic: str = "not_verified"
) -> str:
    statuses = {workflow.status for workflow in workflows.values()}
    if "failed" in statuses:
        return "failed"
    if statuses and statuses == {"completed"} and deterministic == "passed":
        return "passed"
    if "needs_review" in statuses:
        return "needs_review"
    if (
        "not_verified" in statuses
        or "unavailable" in statuses
        or deterministic != "passed"
    ):
        return "not_verified"
    return "partial"


def _validate_public_key(value: str) -> None:
    if SECRET_KEY.search(value):
        raise ValueError("secret-like fields are forbidden in public audit data")


def _validate_relative_path(value: str) -> None:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        not value
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ValueError("audit artifacts must use contained relative paths")


def _reject_private_values(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_public_key(str(key))
            _reject_private_values(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_private_values(item)
    elif isinstance(value, str):
        if WINDOWS_ABSOLUTE.search(value) or value.startswith(("/", "\\\\")):
            raise ValueError("absolute paths are forbidden in public audit data")
        if SECRET_ASSIGNMENT.search(value):
            raise ValueError("secret assignments are forbidden in public audit data")


def _contained(root: Path, relative: str) -> Path:
    _validate_relative_path(relative)
    candidate = (root / Path(PurePosixPath(relative))).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise AuditError("artifact escaped the audit root") from error
    return candidate


def _read_object(path: Path, name: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read {name}") from error
    return _mapping(value, name)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AuditError(f"{name} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuditError(f"{name} must be a non-empty string")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _run(arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(arguments),
            shell=False,
            check=False,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AuditError("local media command could not be executed") from error
    if completed.returncode != 0:
        raise AuditError("local media command failed")
    return completed


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in (
        str(windows_fonts / "msyh.ttc"),
        str(windows_fonts / "simhei.ttf"),
        "NotoSansCJK-Regular.ttc",
        "Arial Unicode.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _create_contact_sheets(root: Path, summary: AuditSummary) -> None:
    manifest = _read_object(root / "demo-manifest.json", "demo manifest")
    source = _contained(
        root, _string(_mapping(manifest["source"], "source")["path"], "source path")
    )
    timestamps = HERO_TIMESTAMPS
    build_contact_sheet(source, timestamps, root / "source-contact-sheet.webp")
    video_keys = {"publish_ready": "video", "video_rescue": "faithful"}
    for workflow_id, key in video_keys.items():
        workflow = summary.workflows.get(workflow_id)
        if workflow is None or key not in workflow.artifacts:
            continue
        build_contact_sheet(
            _contained(root, workflow.artifacts[key]),
            timestamps,
            root / f"{workflow_id.replace('_', '-')}-contact-sheet.webp",
        )
    useful = summary.workflows.get("useful_content")
    if useful is not None and "artifact_000" in useful.artifacts:
        build_contact_sheet(
            _contained(root, useful.artifacts["artifact_000"]),
            (2.5, 10.0, 18.0),
            root / "useful-content-contact-sheet.webp",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("demos/full-local-four-mode/README-template.md"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    summary = audit_source_and_results(root)
    _create_contact_sheets(root, summary)
    write_verification_summary(summary, root / "verification-summary.json")
    render_beginner_guide(summary, args.template, root / "README-demo.md")
    print(
        json.dumps(
            {
                "overall_status": summary.overall_status,
                "workflows": {
                    key: value.status for key, value in summary.workflows.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
