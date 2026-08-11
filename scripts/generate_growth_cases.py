"""Generate verified, project-authored public growth cases without network access."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageStat

_SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_SOURCE_ROOT = _SCRIPT_REPOSITORY_ROOT / "src"
if str(_SCRIPT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_SOURCE_ROOT))

from videoscope import __version__
from videoscope.rescue.models import (
    RescueConfirmation,
    RescueStrategy,
    RescueSymptom,
    RescueVerificationStatus,
)
from videoscope.rescue.pipeline import RescueConfig, RescueStatus, VideoRescuePipeline
from videoscope.resolve.models import PublishProfileId, VerificationStatus
from videoscope.resolve.pipeline import (
    PublishReadyConfig,
    PublishReadyPipeline,
    PublishReadyStatus,
)
from videoscope.video.probe import probe_video

REPOSITORY_ROOT = _SCRIPT_REPOSITORY_ROOT
DEFAULT_DESTINATION = REPOSITORY_ROOT / "site" / "public" / "cases"
DEFAULT_MANIFEST_PATH = REPOSITORY_ROOT / "site" / "src" / "data" / "case-studies.json"
SOURCE_DURATION_SECONDS = 12.0
SOURCE_WIDTH = 640
SOURCE_HEIGHT = 360
SOURCE_FRAME_RATE = 24.0
PUBLIC_VIDEO_MAX_WIDTH = 1280
PUBLIC_VIDEO_MAX_HEIGHT = 720
PUBLIC_ASPECT_RATIO_TOLERANCE = 0.005
COMMAND_TIMEOUT_SECONDS = 300.0
PUBLIC_REPORT_KEYS = {
    "case_id",
    "schema_version",
    "actions",
    "comparison",
    "verification",
    "limitations",
    "versions",
    "output_sha256",
}
_SHA256_KEYS = ("beforeVideo", "afterVideo", "poster", "publicReport")
_ASSET_NAMES = {
    "beforeVideo": "before.mp4",
    "afterVideo": "after.mp4",
    "poster": "poster.webp",
    "publicReport": "public-report.json",
}
_PROVENANCE_FILENAME = "PROVENANCE.md"
_CASE_ORDER = {
    "timeline-rescue": 0,
    "measured-improvement": 1,
    "no-crop-vertical-publish": 2,
}
_SCALAR_TYPES = (str, int, float, bool, type(None))
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[\s\"'])(?:[a-z]:[\\/])")


class CaseGenerationError(RuntimeError):
    """A bounded, actionable failure while producing public case assets."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stderr: str
    stdout: str = ""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        shell: bool,
    ) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class CaseSpec:
    case_id: str
    slug: str
    source_name: str
    comparison_start: float
    comparison_end: float
    rescue_strategy: RescueStrategy | None
    rescue_symptoms: tuple[RescueSymptom, ...]
    include_improved: bool
    publish_profile: PublishProfileId


@dataclass(slots=True)
class CompletedCase:
    case_id: str
    slug: str
    status: str
    case_directory: Path
    manifest_path: Path
    record: dict[str, Any]
    public_report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CaseRecordSummary:
    slug: str
    status: str
    sha256: dict[str, str]


@dataclass(frozen=True, slots=True)
class CaseGenerationSummary:
    status: str
    cases: tuple[CaseRecordSummary, ...]
    sha256: dict[str, str] = field(default_factory=dict)
    public_json_strings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    after_source: Path
    actions: tuple[dict[str, Any], ...]
    checks: tuple[dict[str, Any], ...]
    limitations: tuple[dict[str, str], ...]


CASE_SPECS = (
    CaseSpec(
        case_id="growth-timeline-rescue-v1",
        slug="timeline-rescue",
        source_name="timeline-rescue-source.mkv",
        comparison_start=3.0,
        comparison_end=9.0,
        rescue_strategy=RescueStrategy.CONSERVATIVE,
        rescue_symptoms=(RescueSymptom.TIMELINE_DISCONTINUITY,),
        include_improved=False,
        publish_profile=PublishProfileId.COMPATIBLE_MP4,
    ),
    CaseSpec(
        case_id="growth-measured-improvement-v1",
        slug="measured-improvement",
        source_name="measured-improvement-source.mp4",
        comparison_start=3.0,
        comparison_end=8.0,
        rescue_strategy=RescueStrategy.BALANCED,
        rescue_symptoms=(
            RescueSymptom.DARK,
            RescueSymptom.VIDEO_NOISE,
            RescueSymptom.FLICKER,
        ),
        include_improved=True,
        publish_profile=PublishProfileId.COMPATIBLE_MP4,
    ),
    CaseSpec(
        case_id="growth-no-crop-vertical-v1",
        slug="no-crop-vertical-publish",
        source_name="no-crop-vertical-source.mp4",
        comparison_start=3.0,
        comparison_end=9.0,
        rescue_strategy=None,
        rescue_symptoms=(),
        include_improved=False,
        publish_profile=PublishProfileId.SOCIAL_VERTICAL,
    ),
)


def sanitize_diagnostic(value: str) -> str:
    """Bound external diagnostics and remove absolute path-shaped text."""
    bounded = value[-4000:].replace(str(REPOSITORY_ROOT), "<repository>")
    bounded = bounded.replace(REPOSITORY_ROOT.as_posix(), "<repository>")
    bounded = re.sub(r"(?i)[a-z]:[\\/][^\r\n\"']+", "<local-path>", bounded)
    bounded = re.sub(r"/(?:Users|home)/[^\s\"']+", "<local-path>", bounded)
    return bounded.strip()


def run_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    shell: bool = False,
) -> CommandResult:
    """Run one bounded argument-array command without invoking a shell."""
    if shell is not False:
        raise ValueError("Growth case commands never permit shell execution")
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaseGenerationError("A local media command timed out") from exc
    except OSError as exc:
        raise CaseGenerationError(
            "A required local media command could not start"
        ) from exc
    return CommandResult(
        completed.returncode,
        sanitize_diagnostic(completed.stderr[-4000:]),
        completed.stdout[-4000:],
    )


def _run_checked(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    description: str,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    arguments = list(argv)
    if not arguments or any(not isinstance(item, str) for item in arguments):
        raise CaseGenerationError("External commands must be non-empty string arrays")
    result = runner(arguments, timeout_seconds=timeout_seconds, shell=False)
    if result.returncode != 0:
        detail = f": {result.stderr}" if result.stderr else ""
        raise CaseGenerationError(f"{description} failed{detail}")
    return result


def _video_source_filter(*, bounded_condition: bool) -> str:
    base = (
        "drawgrid=width=32:height=24:thickness=2:color=white@0.65,"
        "drawbox=x='mod(t*260,iw-180)':y=55:w=180:h=110:"
        "color=0x2dd4bf:t=fill,"
        "drawbox=x='iw-180-mod(t*190,iw-180)':y=205:w=180:h=100:"
        "color=0xf97316:t=fill"
    )
    if not bounded_condition:
        return base
    return (
        f"{base},"
        "eq=brightness='if(between(t,3,8),if(mod(floor(n/5),2),0.05,-0.08),0)':"
        "eval=frame,"
        "noise=alls=8:allf=t+u:all_seed=42:enable='between(t,3,8)'"
    )


def _common_source_prefix(ffmpeg: str) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]


def _common_encode_options() -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "24",
        "-g",
        "24",
        "-threads",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-map_metadata",
        "-1",
    ]


def generate_case_sources(
    staging_directory: Path,
    *,
    runner: CommandRunner,
    ffmpeg: str = "ffmpeg",
) -> dict[str, Path]:
    """Generate three deterministic, text-free local media sources."""
    root = Path(staging_directory).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    timeline = root / "timeline-rescue-source.mkv"
    measured = root / "measured-improvement-source.mp4"
    vertical = root / "no-crop-vertical-source.mp4"

    timeline_command = [
        *_common_source_prefix(ffmpeg),
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x101820:size={SOURCE_WIDTH}x{SOURCE_HEIGHT}:rate=24:duration=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=12",
        "-filter_complex",
        f"[0:v]{_video_source_filter(bounded_condition=False)}[v];[1:a]anull[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_common_encode_options(),
        "-output_ts_offset",
        "0.08",
        "-avoid_negative_ts",
        "disabled",
        "-fflags",
        "+bitexact",
        str(timeline),
    ]
    measured_command = [
        *_common_source_prefix(ffmpeg),
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x526277:size={SOURCE_WIDTH}x{SOURCE_HEIGHT}:rate=24:duration=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=520:sample_rate=48000:duration=12",
        "-f",
        "lavfi",
        "-i",
        "anoisesrc=color=white:amplitude=0.22:sample_rate=48000:duration=12:seed=42",
        "-filter_complex",
        (
            f"[0:v]{_video_source_filter(bounded_condition=True)}[v];"
            "[2:a]volume='if(between(t,3,8),0.55,0)':eval=frame[noise];"
            "[1:a][noise]amix=inputs=2:duration=first:normalize=0[a]"
        ),
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_common_encode_options(),
        "-movflags",
        "+faststart",
        str(measured),
    ]
    vertical_filter = (
        _video_source_filter(bounded_condition=False)
        + ",drawbox=x=0:y=0:w=24:h=ih:color=0xef4444:t=fill"
        + ",drawbox=x=iw-24:y=0:w=24:h=ih:color=0x3b82f6:t=fill"
        + ",drawbox=x=0:y=0:w=iw:h=20:color=0xf59e0b:t=fill"
        + ",drawbox=x=0:y=ih-20:w=iw:h=20:color=0xa855f7:t=fill"
    )
    vertical_command = [
        *_common_source_prefix(ffmpeg),
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x111827:size={SOURCE_WIDTH}x{SOURCE_HEIGHT}:rate=24:duration=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:sample_rate=48000:duration=12",
        "-filter_complex",
        f"[0:v]{vertical_filter}[v];[1:a]anull[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_common_encode_options(),
        "-movflags",
        "+faststart",
        str(vertical),
    ]
    for description, command, output in (
        ("timeline source generation", timeline_command, timeline),
        ("measured-improvement source generation", measured_command, measured),
        ("vertical source generation", vertical_command, vertical),
    ):
        _require_below(output, root)
        _run_checked(runner, command, description=description)
        if not output.is_file() or output.stat().st_size <= 0:
            raise CaseGenerationError(f"{description} did not produce a file")
    return {
        "timeline-rescue": timeline,
        "measured-improvement": measured,
        "no-crop-vertical-publish": vertical,
    }


def extract_comparison_assets(
    before_source: Path,
    after_source: Path,
    *,
    comparison: Mapping[str, float],
    destination: Path,
    runner: CommandRunner,
    ffmpeg: str = "ffmpeg",
) -> dict[str, Path]:
    """Encode same-range before/after clips and one WebP poster."""
    start = float(comparison["startSeconds"])
    end = float(comparison["endSeconds"])
    if start < 0 or end <= start:
        raise CaseGenerationError("Comparison range must be positive and ordered")
    duration = end - start
    root = Path(destination).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    before = root / "before.mp4"
    after = root / "after.mp4"
    poster = root / "poster.webp"
    clip_outputs = ((Path(before_source), before), (Path(after_source), after))
    for source, output in clip_outputs:
        command = [
            *_common_source_prefix(ffmpeg),
            "-ss",
            _seconds(start),
            "-t",
            _seconds(duration),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            (
                f"scale=w='min({PUBLIC_VIDEO_MAX_WIDTH},iw)':"
                f"h='min({PUBLIC_VIDEO_MAX_HEIGHT},ih)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "24",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(output),
        ]
        _require_below(output, root)
        _run_checked(runner, command, description=f"{output.name} extraction")
    poster_command = [
        *_common_source_prefix(ffmpeg),
        "-ss",
        _seconds(duration / 2),
        "-i",
        str(after),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-c:v",
        "libwebp",
        "-lossless",
        "1",
        "-map_metadata",
        "-1",
        str(poster),
    ]
    _run_checked(runner, poster_command, description="poster extraction")
    for output in (before, after, poster):
        if not output.is_file() or output.stat().st_size <= 0:
            raise CaseGenerationError(f"Public asset was not produced: {output.name}")
    return {"beforeVideo": before, "afterVideo": after, "poster": poster}


def _execute_rescue(spec: CaseSpec, source: Path, output: Path) -> WorkflowResult:
    assert spec.rescue_strategy is not None
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=output,
            strategy=spec.rescue_strategy,
            symptoms=spec.rescue_symptoms,
        )
    )
    preparation = pipeline.prepare(source)
    confirmation = RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=spec.include_improved,
        accepted_action_ids=tuple(
            action.id
            for action in preparation.plan.actions
            if action.requires_confirmation
        ),
        accepted_trim_damage_ids=(),
    )
    confirmed = pipeline.confirm(preparation, confirmation)
    result = pipeline.execute(confirmed, confirmation)
    if result.status is not RescueStatus.COMPLETED:
        raise CaseGenerationError(f"Case {spec.slug} was not completed")
    if result.verification is None:
        raise CaseGenerationError(f"Case {spec.slug} has no Rescue verification")
    if result.verification.faithful_status is not RescueVerificationStatus.PASSED:
        raise CaseGenerationError(
            f"Case {spec.slug} faithful verification did not pass"
        )
    if spec.include_improved:
        if (
            result.improved_path is None
            or result.verification.improved_status
            is not RescueVerificationStatus.PASSED
        ):
            raise CaseGenerationError(
                f"Case {spec.slug} measured improvement did not pass verification"
            )
        after_source = result.improved_path
    else:
        if result.faithful_path is None:
            raise CaseGenerationError(f"Case {spec.slug} has no faithful artifact")
        after_source = result.faithful_path
    actions = tuple(
        _public_action("video-rescue", action, version=preparation.plan.schema_version)
        for action in preparation.plan.actions
    )
    checks = tuple(
        _public_check(
            f"rescue:{check.artifact}:{check.check_id}",
            check.status.value,
            check.message,
            check.measured,
        )
        for check in result.verification.checks
    )
    technical = result.technical_report
    limitations = tuple(
        _localized(item)
        for item in (() if technical is None else technical.limitations)
    )
    return WorkflowResult(after_source, actions, checks, limitations)


def _execute_publish(
    spec: CaseSpec,
    source: Path,
    output: Path,
) -> WorkflowResult:
    pipeline = PublishReadyPipeline(
        PublishReadyConfig(
            profile_id=spec.publish_profile,
            output_directory=output,
        )
    )
    preparation = pipeline.prepare(source)
    try:
        result = pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )
    finally:
        pipeline.discard(preparation)
    if result.status is not PublishReadyStatus.COMPLETED:
        raise CaseGenerationError(f"Case {spec.slug} Publish Ready was not completed")
    verification = result.technical_report.verification
    if verification.status is not VerificationStatus.PASSED:
        raise CaseGenerationError(
            f"Case {spec.slug} Publish Ready verification did not pass"
        )
    actions = tuple(
        _public_action(
            "publish-ready", action, version=preparation.plan.profile_version
        )
        for action in preparation.plan.actions
    )
    checks = tuple(
        _public_check(
            f"publish:{check.check_id}",
            check.status.value,
            check.message,
            check.measured,
        )
        for check in verification.checks
    )
    limitations = tuple(_localized(item) for item in verification.manual_review_reasons)
    return WorkflowResult(result.video_path, actions, checks, limitations)


def _run_workflows(spec: CaseSpec, source: Path, root: Path) -> WorkflowResult:
    actions: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    limitations: list[dict[str, str]] = []
    publish_input = source
    if spec.rescue_strategy is not None:
        rescued = _execute_rescue(spec, source, root / "rescue")
        publish_input = rescued.after_source
        actions.extend(rescued.actions)
        checks.extend(rescued.checks)
        limitations.extend(rescued.limitations)
    published = _execute_publish(spec, publish_input, root / "publish")
    actions.extend(published.actions)
    checks.extend(published.checks)
    limitations.extend(published.limitations)
    return WorkflowResult(
        published.after_source,
        tuple(actions),
        tuple(checks),
        tuple(limitations),
    )


def _public_action(workflow: str, action: Any, *, version: str) -> dict[str, Any]:
    dumped = action.model_dump(mode="json")
    kind = str(dumped.get("kind", "unknown"))
    action_id = str(dumped.get("id") or dumped.get("action_id") or kind)
    description = str(dumped.get("description") or f"Executed {kind}.")
    parameters = _scalar_record(dumped.get("parameters", {}))
    return {
        "workflow": workflow,
        "actionId": action_id,
        "version": str(dumped.get("version") or version),
        "kind": kind,
        "description": {
            "en": description,
            "zh-CN": f"执行已确认的 {kind} 本地处理。",
        },
        "parameters": parameters,
    }


def _public_check(
    check_id: str,
    status: str,
    message: str,
    measured: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "checkId": check_id,
        "status": status,
        "summary": {
            "en": message,
            "zh-CN": f"本地验证检查 {check_id}：{status}。",
        },
        "measured": _scalar_record(measured),
    }


def _scalar_record(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, _SCALAR_TYPES):
            if isinstance(item, float) and not (float("-inf") < item < float("inf")):
                continue
            result[key] = item
    return result


def _localized(english: str) -> dict[str, str]:
    return {"en": english, "zh-CN": f"限制：{english}"}


def _case_copy(spec: CaseSpec) -> dict[str, Any]:
    copies: dict[str, dict[str, Any]] = {
        "timeline-rescue": {
            "title": {"en": "Timeline rescue", "zh-CN": "时间线救援"},
            "summary": {
                "en": (
                    "A project-authored non-zero-timestamp source is repackaged "
                    "and independently verified before publication."
                ),
                "zh-CN": "项目原创的非零时间戳来源在发布前经过重新封装与独立验证。",
            },
            "symptom": {
                "en": (
                    "The source uses a non-zero container timeline while its "
                    "geometric motion remains intact."
                ),
                "zh-CN": "来源采用非零容器时间线，同时几何运动保持完整。",
            },
        },
        "measured-improvement": {
            "title": {"en": "Measured local improvement", "zh-CN": "实测本地改善"},
            "summary": {
                "en": (
                    "A project-authored 3–8 second luma, flicker, video-noise, "
                    "and audio-noise interval is preserved while the measured "
                    "loudness action is applied after exact confirmation."
                ),
                "zh-CN": (
                    "项目原创素材在 3–8 秒包含亮度、闪烁、视频噪声与音频噪声区间；"
                    "精确确认后仅应用实测响度动作并保留该区间。"
                ),
            },
            "symptom": {
                "en": (
                    "A bounded interval is visibly darker and alternates in "
                    "brightness with deterministic noise."
                ),
                "zh-CN": "一个有界区间明显更暗，并伴随确定性噪声与交替亮度。",
            },
        },
        "no-crop-vertical-publish": {
            "title": {"en": "No-crop vertical publish", "zh-CN": "无裁剪竖屏发布"},
            "summary": {
                "en": (
                    "A project-authored 16:9 frame with four edge markers is "
                    "scaled and padded into the verified 9:16 profile."
                ),
                "zh-CN": (
                    "带四侧边缘标记的项目原创 16:9 画面通过缩放与填充进入"
                    "经验证的 9:16 Profile。"
                ),
            },
            "symptom": {
                "en": "A direct crop would remove the colored edge markers.",
                "zh-CN": "直接裁剪会移除彩色边缘标记。",
            },
        },
    }
    return copies[spec.slug]


def _build_record(
    spec: CaseSpec,
    workflow: WorkflowResult,
    *,
    source_metadata: Any,
    versions: dict[str, str],
    public_checks: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    copy = _case_copy(spec)
    limitations = [
        {
            "en": "This is project-authored procedural media, not real-user footage.",
            "zh-CN": "这是项目原创的程序化媒体，并非真实用户视频。",
        },
        {
            "en": (
                "Results demonstrate this controlled source and current local "
                "toolchain only."
            ),
            "zh-CN": "结果仅演示该受控来源与当前本地工具链。",
        },
        *workflow.limitations,
    ]
    comparison = {
        "startSeconds": spec.comparison_start,
        "endSeconds": spec.comparison_end,
    }
    return {
        "id": spec.case_id,
        "slug": spec.slug,
        "featured": True,
        "provenance": "project-authored",
        "authorizationSummary": {
            "en": (
                "Generated locally from project-authored lavfi geometry and "
                "synthetic audio; no external footage was used."
            ),
            "zh-CN": (
                "由项目原创的 lavfi 几何画面与合成音频在本地生成，未使用外部视频。"
            ),
        },
        "title": copy["title"],
        "summary": copy["summary"],
        "observableSymptom": copy["symptom"],
        "actions": list(workflow.actions),
        "unresolved": [
            {
                "en": "This controlled case does not measure outcomes on user footage.",
                "zh-CN": "此受控案例不衡量用户视频上的结果。",
            }
        ],
        "limitations": limitations,
        "comparison": comparison,
        "media": {
            "durationSeconds": source_metadata.duration_seconds,
            "width": source_metadata.width,
            "height": source_metadata.height,
            "frameRate": source_metadata.average_frame_rate,
        },
        "versions": versions,
        "verification": {
            "status": "completed",
            "checks": [*workflow.checks, *public_checks],
        },
        "reproduction": ["python scripts/generate_growth_cases.py --force"],
    }


def _validate_public_assets(
    spec: CaseSpec,
    assets: Mapping[str, Path],
    *,
    runner: CommandRunner,
    ffmpeg: str,
    ffprobe: str,
) -> tuple[dict[str, Any], ...]:
    duration = spec.comparison_end - spec.comparison_start
    checks: list[dict[str, Any]] = []
    measurements: dict[str, Any] = {}
    for key in ("beforeVideo", "afterVideo"):
        path = assets[key]
        metadata = probe_video(path, ffprobe=ffprobe)
        if abs(metadata.duration_seconds - duration) > 0.15:
            raise CaseGenerationError(f"{spec.slug} {key} duration did not match")
        decode = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ]
        _run_checked(runner, decode, description=f"{spec.slug} {key} decode")
        measurements[f"{key}DurationSeconds"] = round(metadata.duration_seconds, 6)
        measurements[f"{key}Width"] = metadata.width
        measurements[f"{key}Height"] = metadata.height
        if not _public_video_dimensions_pass(metadata.width, metadata.height):
            raise CaseGenerationError(f"{spec.slug} {key} exceeded the media budget")
    if not _comparison_durations_match(
        float(measurements["beforeVideoDurationSeconds"]),
        float(measurements["afterVideoDurationSeconds"]),
        SOURCE_FRAME_RATE,
    ):
        raise CaseGenerationError(f"{spec.slug} comparison durations differ")
    checks.append(
        _public_check(
            "same-source-range",
            "passed",
            "Before and after clips use the identical declared source range.",
            {
                "startSeconds": spec.comparison_start,
                "endSeconds": spec.comparison_end,
                "durationSeconds": duration,
            },
        )
    )
    checks.append(
        _public_check(
            "public-media-decode",
            "passed",
            "Both public comparison clips decoded and matched the bounded duration.",
            measurements,
        )
    )
    if spec.publish_profile is PublishProfileId.SOCIAL_VERTICAL:
        if not _public_video_dimensions_pass(
            int(measurements["afterVideoWidth"]),
            int(measurements["afterVideoHeight"]),
            expected_aspect_ratio=9 / 16,
        ):
            raise CaseGenerationError(
                "Vertical public comparison geometry did not pass"
            )
        edge_markers = _measure_vertical_edge_markers(assets["poster"])
        if not all(edge_markers.values()):
            raise CaseGenerationError("Vertical output did not retain all edge markers")
        checks.append(
            _public_check(
                "vertical-scale-and-pad",
                "passed",
                (
                    "The verified Publish Ready output uses scale-and-pad; "
                    "the public comparison retains 9:16 within its media budget."
                ),
                {
                    "publicOutputWidth": measurements["afterVideoWidth"],
                    "publicOutputHeight": measurements["afterVideoHeight"],
                    "sourceAspectRatio": "16:9",
                    "operation": "scale-and-pad",
                    **edge_markers,
                },
            )
        )
    return tuple(checks)


def write_public_case_record(completed: CompletedCase) -> CaseGenerationSummary:
    """Write one allowlisted public report and update its versioned manifest."""
    if completed.status != "completed":
        raise CaseGenerationError("Only completed cases may be published")
    if set(completed.public_report) != PUBLIC_REPORT_KEYS:
        raise CaseGenerationError("Public report keys must match the allowlist")
    case_root = completed.case_directory.resolve(strict=False)
    media_hashes: dict[str, str] = {}
    for key in ("beforeVideo", "afterVideo", "poster"):
        path = case_root / _ASSET_NAMES[key]
        _require_below(path, case_root)
        if not path.is_file() or path.stat().st_size <= 0:
            raise CaseGenerationError(f"Missing public case asset: {_ASSET_NAMES[key]}")
        media_hashes[key] = _sha256_file(path)
    report = json.loads(json.dumps(completed.public_report, ensure_ascii=False))
    report["output_sha256"] = dict(media_hashes)
    _reject_private_strings(report)
    report_path = case_root / _ASSET_NAMES["publicReport"]
    _write_json_atomic(report_path, report)
    hashes = {**media_hashes, "publicReport": _sha256_file(report_path)}
    record = json.loads(json.dumps(completed.record, ensure_ascii=False))
    if record.get("id") != completed.case_id or record.get("slug") != completed.slug:
        raise CaseGenerationError("Case record identity does not match completed case")
    record["assets"] = {
        key: f"/VideoScope/cases/{completed.slug}/{name}"
        for key, name in _ASSET_NAMES.items()
    }
    record["sha256"] = hashes
    _reject_private_strings(record)

    manifest_path = completed.manifest_path
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseGenerationError("Case-study manifest could not be read") from exc
    if manifest.get("schemaVersion") != 1 or not isinstance(
        manifest.get("cases"), list
    ):
        raise CaseGenerationError("Case-study manifest has an unsupported shape")
    retained = [
        item
        for item in manifest["cases"]
        if isinstance(item, dict)
        and item.get("id") != completed.case_id
        and item.get("slug") != completed.slug
    ]
    retained.append(record)
    retained.sort(
        key=lambda item: (
            _CASE_ORDER.get(str(item.get("slug")), 99),
            str(item.get("slug")),
        )
    )
    manifest["generatedBy"] = "scripts/generate_growth_cases.py"
    manifest["cases"] = retained
    _reject_private_strings(manifest)
    _write_json_atomic(manifest_path, manifest)
    strings = tuple(_json_strings({"report": report, "record": record}))
    summary = CaseRecordSummary(completed.slug, completed.status, hashes)
    return CaseGenerationSummary(
        status=completed.status,
        cases=(summary,),
        sha256=hashes,
        public_json_strings=strings,
    )


def generate_cases(
    destination: Path,
    *,
    force: bool,
    runner: CommandRunner,
) -> CaseGenerationSummary:
    """Generate, verify, and atomically publish all three public cases."""
    target = Path(destination).resolve(strict=False)
    manifest_path = _manifest_path_for(target)
    if target.exists() and not force:
        raise CaseGenerationError("Case destination already exists; pass --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent)
    ).resolve(strict=True)
    private_root = staging / "_private"
    staging_manifest = staging / "_manifest" / "case-studies.json"
    staging_manifest.parent.mkdir(parents=True)
    _write_json_atomic(
        staging_manifest,
        {
            "schemaVersion": 1,
            "generatedBy": "scripts/generate_growth_cases.py",
            "cases": [],
        },
    )
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        shutil.rmtree(staging, ignore_errors=True)
        raise CaseGenerationError(
            "FFmpeg and ffprobe must be installed and available on PATH"
        )
    summaries: list[CaseRecordSummary] = []
    published = False
    backup: Path | None = None
    try:
        sources = generate_case_sources(
            private_root / "sources", runner=runner, ffmpeg=ffmpeg
        )
        source_metadata: dict[str, Any] = {}
        for spec in CASE_SPECS:
            metadata = probe_video(sources[spec.slug], ffprobe=ffprobe)
            if (
                abs(metadata.duration_seconds - SOURCE_DURATION_SECONDS) > 0.15
                or abs(metadata.average_frame_rate - SOURCE_FRAME_RATE) > 0.01
                or not metadata.has_audio
            ):
                raise CaseGenerationError(f"Source contract failed for {spec.slug}")
            source_metadata[spec.slug] = metadata
        version_result = _run_checked(
            runner,
            [ffmpeg, "-version"],
            description="FFmpeg version inspection",
            timeout_seconds=10,
        )
        ffmpeg_version = next(
            (
                line.strip()
                for line in version_result.stdout.splitlines()
                if line.strip()
            ),
            "FFmpeg version unavailable",
        )
        versions = {
            "videoscope": __version__,
            "ffmpeg": ffmpeg_version,
            "platform": f"{platform.system()}-{platform.machine()}",
            "configuration": "growth-case-generator-v1",
        }
        for spec in CASE_SPECS:
            source = sources[spec.slug]
            workflow = _run_workflows(
                spec, source, private_root / "workflows" / spec.slug
            )
            case_directory = staging / spec.slug
            assets = extract_comparison_assets(
                source,
                workflow.after_source,
                comparison={
                    "startSeconds": spec.comparison_start,
                    "endSeconds": spec.comparison_end,
                },
                destination=case_directory,
                runner=runner,
                ffmpeg=ffmpeg,
            )
            public_checks = _validate_public_assets(
                spec,
                assets,
                runner=runner,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
            record = _build_record(
                spec,
                workflow,
                source_metadata=source_metadata[spec.slug],
                versions=versions,
                public_checks=public_checks,
            )
            public_report = {
                "case_id": spec.case_id,
                "schema_version": "1.0",
                "actions": record["actions"],
                "comparison": record["comparison"],
                "verification": record["verification"],
                "limitations": record["limitations"],
                "versions": record["versions"],
                "output_sha256": {},
            }
            written = write_public_case_record(
                CompletedCase(
                    case_id=spec.case_id,
                    slug=spec.slug,
                    status="completed",
                    case_directory=case_directory,
                    manifest_path=staging_manifest,
                    record=record,
                    public_report=public_report,
                )
            )
            summaries.extend(written.cases)
        final_manifest = json.loads(staging_manifest.read_text(encoding="utf-8"))
        if len(final_manifest["cases"]) != len(CASE_SPECS):
            raise CaseGenerationError("Generated manifest does not contain three cases")
        provenance = target / _PROVENANCE_FILENAME
        if provenance.is_file():
            shutil.copy2(provenance, staging / _PROVENANCE_FILENAME)
        shutil.rmtree(private_root)
        shutil.rmtree(staging_manifest.parent)
        allowed_names = {spec.slug for spec in CASE_SPECS}
        if (staging / _PROVENANCE_FILENAME).is_file():
            allowed_names.add(_PROVENANCE_FILENAME)
        if {item.name for item in staging.iterdir()} != allowed_names:
            raise CaseGenerationError("Staging contained an unexpected public artifact")
        old_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
        if target.exists():
            backup = target.parent / f".{target.name}.backup-{os.getpid()}"
            if backup.exists():
                raise CaseGenerationError("A stale case publication backup exists")
            os.replace(target, backup)
        try:
            os.replace(staging, target)
            published = True
            _write_json_atomic(manifest_path, final_manifest)
        except Exception:
            if published and target.exists():
                failed = target.parent / f".{target.name}.failed-{os.getpid()}"
                os.replace(target, failed)
                shutil.rmtree(failed, ignore_errors=True)
                published = False
            if backup is not None and backup.exists():
                os.replace(backup, target)
                backup = None
            if old_manifest is not None:
                _write_bytes_atomic(manifest_path, old_manifest)
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
            backup = None
        return CaseGenerationSummary(status="completed", cases=tuple(summaries))
    except CaseGenerationError:
        raise
    except Exception as exc:
        raise CaseGenerationError("Growth case generation failed") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)


def _manifest_path_for(destination: Path) -> Path:
    if destination == DEFAULT_DESTINATION.resolve(strict=False):
        return DEFAULT_MANIFEST_PATH
    if destination.parent.name == "public" and destination.parent.parent.name == "site":
        return destination.parent.parent / "src" / "data" / "case-studies.json"
    raise CaseGenerationError("Destination must be a site/public/cases directory")


def _require_below(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve(strict=False)):
        raise CaseGenerationError("Generated path escaped the staging directory")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, item in value.items():
            result.extend(_json_strings(key))
            result.extend(_json_strings(item))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result = []
        for item in value:
            result.extend(_json_strings(item))
        return result
    return []


def _reject_private_strings(value: object) -> None:
    for item in _json_strings(value):
        normalized = item.replace("\\", "/")
        if (
            _WINDOWS_ABSOLUTE_PATH.search(item)
            or "/Users/" in normalized
            or "/home/" in normalized
            or "working_directory" in item
            or "prompt" == item.casefold()
            or "provider" in item.casefold()
        ):
            raise CaseGenerationError(
                "Public JSON contains a private or forbidden value"
            )


def _write_json_atomic(path: Path, value: object) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _write_bytes_atomic(path, encoded)


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _seconds(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".") or "0"


def _comparison_durations_match(
    before_seconds: float,
    after_seconds: float,
    frame_rate: float,
) -> bool:
    if frame_rate <= 0:
        return False
    return abs(before_seconds - after_seconds) <= (1.0 / frame_rate) + 0.005


def _public_video_dimensions_pass(
    width: int,
    height: int,
    *,
    expected_aspect_ratio: float | None = None,
) -> bool:
    if (
        width <= 0
        or height <= 0
        or width > PUBLIC_VIDEO_MAX_WIDTH
        or height > PUBLIC_VIDEO_MAX_HEIGHT
    ):
        return False
    if expected_aspect_ratio is None:
        return True
    return (
        abs((width / height) - expected_aspect_ratio) <= PUBLIC_ASPECT_RATIO_TOLERANCE
    )


def _measure_vertical_edge_markers(poster: Path) -> dict[str, bool]:
    with Image.open(poster) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if width <= 0 or height <= 0:
        return {
            "leftEdgeMarkerVisible": False,
            "rightEdgeMarkerVisible": False,
            "topEdgeMarkerVisible": False,
            "bottomEdgeMarkerVisible": False,
        }
    content_height = min(height, round(width * 9 / 16))
    content_top = (height - content_height) // 2
    content_bottom = content_top + content_height
    center_x = width // 2
    center_y = content_top + content_height // 2
    patch_radius = max(2, min(width, content_height) // 100)

    def mean_rgb(x: int, y: int) -> tuple[float, float, float]:
        left = max(0, x - patch_radius)
        top = max(0, y - patch_radius)
        right = min(width, x + patch_radius + 1)
        bottom = min(height, y + patch_radius + 1)
        mean = ImageStat.Stat(image.crop((left, top, right, bottom))).mean
        return float(mean[0]), float(mean[1]), float(mean[2])

    left = mean_rgb(max(2, width // 100), center_y)
    right = mean_rgb(width - max(3, width // 100), center_y)
    top = mean_rgb(center_x, content_top + max(3, content_height // 50))
    bottom = mean_rgb(center_x, content_bottom - max(4, content_height // 50))
    return {
        "leftEdgeMarkerVisible": left[0] > left[1] + 40 and left[0] > left[2] + 40,
        "rightEdgeMarkerVisible": right[2] > right[0] + 40 and right[2] > right[1] + 40,
        "topEdgeMarkerVisible": top[0] > top[2] + 60 and top[1] > top[2] + 30,
        "bottomEdgeMarkerVisible": bottom[0] > bottom[1] + 35
        and bottom[2] > bottom[1] + 35,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Atomically replace an existing generated cases directory.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        summary = generate_cases(
            DEFAULT_DESTINATION,
            force=bool(arguments.force),
            runner=run_command,
        )
    except CaseGenerationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for case in summary.cases:
        print(f"{case.slug}: {case.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
