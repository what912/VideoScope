"""Prepare and execute the full-local Publish Ready and Rescue demo safely."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from scripts.full_local_demo_contract import safe_relative_path, stream_sha256
from videoscope.content import (
    ContentConfig,
    ContentGoal,
    ContentPipelineConfig,
    ContentPipelineDependencies,
    ContentPreviewBuilder,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    LongVideoContentPipeline,
    NativeContentExecutor,
    StructuralFeatureConfig,
    make_user_range_id,
)
from videoscope.privacy import (
    NormalizedBox,
    PrivacyDecision,
    PrivacyJobOutcome,
    PrivacyReviewDecision,
    RedactionStyle,
)
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.pipeline import (
    PrivacyScanResult,
    SafeSharingConfig,
    SafeSharingPipeline,
)
from videoscope.rescue.models import (
    RescueConfirmation,
    RescueStrategy,
    RescueSymptom,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    VideoRescuePipeline,
)
from videoscope.resolve import (
    PublishProfileId,
    PublishReadyConfig,
    PublishReadyPipeline,
    VerificationStatus,
)

_SHA256 = r"^[0-9a-f]{64}$"
_WORKFLOWS = ("publish_ready", "video_rescue", "useful_content", "safe_sharing")
_SOURCE_NAME = "VideoScope-Full-Local-Demo-Source.mp4"
_MANIFEST_NAME = "demo-manifest.json"
_REVIEW_PREVIEW_ROOT = PurePosixPath("review-previews")
_IMPROVEMENT_KINDS = frozenset(
    {
        "adjust_luma",
        "denoise_video",
        "sharpen",
        "deflicker",
        "stabilize",
        "normalize_audio",
        "denoise_audio",
    }
)
_USEFUL_KEEP_RANGES = ((0.0, 5.0), (10.0, 20.0), (36.0, 42.0))
_PRIVACY_START_SECONDS = 25.0
_PRIVACY_END_SECONDS = 32.0


class DemoConfirmationError(ValueError):
    """A user confirmation does not bind to freshly prepared local evidence."""


class _DemoModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or PureWindowsPath(value).root
        or ".." in path.parts
        or "\\" in value
        or value != path.as_posix()
    ):
        raise ValueError("public path must be a normalized relative POSIX path")
    return value


def _reject_path_leakage(value: object) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_path_leakage(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_path_leakage(item)
    elif isinstance(value, str) and (
        Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).drive)
    ):
        raise ValueError("public data cannot contain an absolute path")


def _public_json_value(value: object) -> JsonValue:
    """Normalize public data deterministically without stringifying private paths."""
    if value is None or isinstance(value, (bool, int, float, str)):
        _reject_path_leakage(value)
        return value
    if isinstance(value, Enum):
        return _public_json_value(value.value)
    if isinstance(value, Path):
        raise ValueError("public data cannot contain a path value")
    if hasattr(value, "model_dump"):
        return _public_json_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("public JSON mapping keys must be strings")
            normalized[key] = _public_json_value(item)
        return normalized
    if isinstance(value, (set, frozenset)):
        items = [_public_json_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_public_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return _public_json_value(vars(value))
    raise ValueError(f"public data has unsupported type: {type(value).__name__}")


def _safe_artifacts(artifacts: Mapping[str, str]) -> dict[str, str]:
    return {key: _relative_path(value) for key, value in artifacts.items()}


class WorkflowCandidate(_DemoModel):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    ranges: tuple[tuple[float, float], ...] = ()
    requires_confirmation: bool
    evidence: tuple[dict[str, JsonValue], ...] = ()
    preview_relative_path: str | None = None
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_public_path_and_ranges(self) -> WorkflowCandidate:
        if self.preview_relative_path is not None:
            _relative_path(self.preview_relative_path)
        for start, end in self.ranges:
            if start < 0 or end < start:
                raise ValueError("candidate range must be ordered and non-negative")
        object.__setattr__(
            self,
            "evidence",
            tuple(
                cast(dict[str, JsonValue], _public_json_value(item))
                for item in self.evidence
            ),
        )
        object.__setattr__(
            self,
            "limitations",
            tuple(cast(str, _public_json_value(item)) for item in self.limitations),
        )
        return self


class PreparedWorkflow(_DemoModel):
    workflow_id: Literal[
        "publish_ready", "video_rescue", "useful_content", "safe_sharing"
    ]
    plan_digest: str | None = Field(default=None, pattern=_SHA256)
    candidates: tuple[WorkflowCandidate, ...]
    confirmation_required: Literal[True] = True
    preparation_status: str = Field(min_length=1)


class PreparedReview(_DemoModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str = Field(pattern=_SHA256)
    contract_sha256: str = Field(pattern=_SHA256)
    workflows: dict[str, PreparedWorkflow]

    @model_validator(mode="after")
    def validate_workflow_keys(self) -> PreparedReview:
        if any(key != value.workflow_id for key, value in self.workflows.items()):
            raise ValueError("workflow map keys must match workflow IDs")
        return self


@dataclass(frozen=True, slots=True)
class SafeSharingScanPreparation:
    scan: PrivacyScanResult | object
    scan_digest: str
    manual_visual_regions: tuple[ManualVisualRegionInput, ...]
    manual_audio_intervals: tuple[ManualAudioIntervalInput, ...]


class PrivacyReviewChoice(_DemoModel):
    risk_id: str = Field(pattern=r"^privacy_risk_[0-9a-f]{64}$")
    decision: Literal["allow", "redact"]
    style: str | None = None

    @model_validator(mode="after")
    def validate_decision_style(self) -> PrivacyReviewChoice:
        if self.decision == "redact" and self.style is None:
            raise ValueError("redact privacy choice requires a style")
        if self.decision == "allow" and self.style is not None:
            raise ValueError("allow privacy choice forbids a style")
        return self


class PrivacyReviewFile(_DemoModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str = Field(pattern=_SHA256)
    contract_sha256: str = Field(pattern=_SHA256)
    scan_digest: str = Field(pattern=_SHA256)
    reviewed_at: datetime
    choices: tuple[PrivacyReviewChoice, ...]

    @model_validator(mode="after")
    def validate_choices(self) -> PrivacyReviewFile:
        if self.reviewed_at.tzinfo is None:
            raise ValueError("privacy review timestamp must include a timezone")
        ids = tuple(item.risk_id for item in self.choices)
        if len(ids) != len(set(ids)):
            raise ValueError("privacy review choices must have unique risk IDs")
        return self


class ConfirmablePlan(_DemoModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str = Field(pattern=_SHA256)
    contract_sha256: str = Field(pattern=_SHA256)
    workflows: dict[str, PreparedWorkflow]

    @model_validator(mode="after")
    def validate_workflow_keys(self) -> ConfirmablePlan:
        if any(key != value.workflow_id for key, value in self.workflows.items()):
            raise ValueError("workflow map keys must match workflow IDs")
        return self


class WorkflowConfirmation(_DemoModel):
    workflow_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256)
    accepted_action_ids: tuple[str, ...] = ()
    accepted_trim_damage_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_ids(self) -> WorkflowConfirmation:
        if len(self.accepted_action_ids) != len(set(self.accepted_action_ids)):
            raise ValueError("accepted action IDs must be unique")
        if len(self.accepted_trim_damage_ids) != len(
            set(self.accepted_trim_damage_ids)
        ):
            raise ValueError("accepted trim IDs must be unique")
        return self


class ExecutionConfirmation(_DemoModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str = Field(pattern=_SHA256)
    contract_sha256: str = Field(pattern=_SHA256)
    workflows: dict[str, WorkflowConfirmation]


class WorkflowOutcome(_DemoModel):
    workflow_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    source_sha256_before: str = Field(pattern=_SHA256)
    source_sha256_after: str = Field(pattern=_SHA256)
    actions: tuple[dict[str, JsonValue], ...] = ()
    checks: tuple[dict[str, JsonValue], ...] = ()
    artifacts: dict[str, str] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    final_human_review_required: bool = False

    @model_validator(mode="after")
    def validate_artifacts(self) -> WorkflowOutcome:
        object.__setattr__(self, "artifacts", _safe_artifacts(self.artifacts))
        object.__setattr__(
            self,
            "actions",
            tuple(
                cast(dict[str, JsonValue], _public_json_value(item))
                for item in self.actions
            ),
        )
        object.__setattr__(
            self,
            "checks",
            tuple(
                cast(dict[str, JsonValue], _public_json_value(item))
                for item in self.checks
            ),
        )
        object.__setattr__(
            self,
            "limitations",
            tuple(cast(str, _public_json_value(item)) for item in self.limitations),
        )
        return self


PublishFactory = Callable[[PublishReadyConfig], Any]
RescueFactory = Callable[[RescueConfig], Any]
OtherFactory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class DemoPipelineDependencies:
    """Injectable factories keep tests offline while production uses real cores."""

    publish_factory: PublishFactory
    rescue_factory: RescueFactory
    content_factory: OtherFactory
    privacy_factory: OtherFactory


def _unavailable_factory(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("this workflow is added by the later demo task")


def _default_content_factory(config: ContentPipelineConfig) -> LongVideoContentPipeline:
    ffmpeg = config.features.ffmpeg
    return LongVideoContentPipeline(
        config,
        dependencies=ContentPipelineDependencies(
            preview_builder=ContentPreviewBuilder(ffmpeg_executable=ffmpeg),
            executor=NativeContentExecutor(ffmpeg=ffmpeg),
        ),
    )


def default_dependencies() -> DemoPipelineDependencies:
    return DemoPipelineDependencies(
        publish_factory=PublishReadyPipeline,
        rescue_factory=VideoRescuePipeline,
        content_factory=_default_content_factory,
        privacy_factory=SafeSharingPipeline,
    )


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _status(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _dump(value: object) -> dict[str, JsonValue]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, dict):
            return cast(dict[str, JsonValue], _public_json_value(dumped))
    if isinstance(value, Mapping):
        return cast(dict[str, JsonValue], _public_json_value(value))
    return {"value": _public_json_value(value)}


def _manifest_binding(source: Path, manifest_path: Path) -> tuple[str, str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_source = manifest["source"]["sha256"]
        contract_digest = manifest["contract"]["sha256"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise DemoConfirmationError("demo manifest is invalid") from exc
    source_hash = stream_sha256(source)
    if source_hash != expected_source:
        raise DemoConfirmationError("source hash does not match demo manifest")
    if not isinstance(contract_digest, str) or len(contract_digest) != 64:
        raise DemoConfirmationError("contract digest is invalid")
    return source_hash, contract_digest


def _copy_review_preview(source: Path, output: Path, relative_path: str) -> str:
    relative = _relative_path(relative_path)
    if not source.is_file():
        raise DemoConfirmationError("prepared preview is unavailable")
    output = output.absolute()
    destination = output / Path(relative)
    _validate_review_destination(output, destination, parent_must_exist=False)
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _validate_review_destination(output, destination, parent_must_exist=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=".preview-copy-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.close(descriptor)
        shutil.copy2(source, temporary)
        _validate_review_destination(output, destination, parent_must_exist=True)
        _validate_temporary_preview(output, destination.parent, temporary)
        os.replace(temporary, destination)
        temporary = None
    except DemoConfirmationError:
        raise
    except OSError as exc:
        raise DemoConfirmationError("prepared preview could not be preserved") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    if not destination.is_file():
        raise DemoConfirmationError("prepared preview could not be preserved")
    normalized = safe_relative_path(destination, output)
    if not isinstance(normalized, str):
        raise DemoConfirmationError("prepared preview path could not be normalized")
    return normalized


def _validate_review_destination(
    output: Path,
    destination: Path,
    *,
    parent_must_exist: bool,
) -> None:
    try:
        output = output.absolute()
        destination = destination.absolute()
        destination.relative_to(output)
        _reject_symlink_components(output)
        _reject_symlink_components(destination)
        if output.exists() and not output.is_dir():
            raise DemoConfirmationError("preview output root is not a directory")
        if destination.exists() and not destination.is_file():
            raise DemoConfirmationError("preview destination is not a regular file")
        resolved_output = output.resolve(strict=parent_must_exist)
        resolved_parent = destination.parent.resolve(strict=parent_must_exist)
        resolved_parent.relative_to(resolved_output)
        if parent_must_exist and (
            not resolved_output.is_dir() or not resolved_parent.is_dir()
        ):
            raise DemoConfirmationError("preview destination parent is invalid")
    except DemoConfirmationError:
        raise
    except (OSError, ValueError) as exc:
        raise DemoConfirmationError(
            "preview destination is not safely contained"
        ) from exc


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise DemoConfirmationError("preview path cannot contain a symbolic link")


def _validate_temporary_preview(
    output: Path,
    destination_parent: Path,
    temporary: Path,
) -> None:
    try:
        _reject_symlink_components(temporary)
        resolved_output = output.resolve(strict=True)
        resolved_parent = destination_parent.resolve(strict=True)
        resolved_temporary = temporary.resolve(strict=True)
        resolved_parent.relative_to(resolved_output)
        if resolved_temporary.parent != resolved_parent or not temporary.is_file():
            raise DemoConfirmationError("temporary preview is not safely contained")
    except DemoConfirmationError:
        raise
    except (OSError, ValueError) as exc:
        raise DemoConfirmationError(
            "temporary preview is not safely contained"
        ) from exc


def _publish_preview_relative(preparation: object) -> str:
    preview_path = Path(getattr(preparation, "preview_path"))
    return (_REVIEW_PREVIEW_ROOT / "publish-ready" / preview_path.name).as_posix()


def _publish_workflow(
    preparation: object, *, preview_relative_path: str | None = None
) -> PreparedWorkflow:
    plan = getattr(preparation, "plan")
    preview_relative_path = preview_relative_path or _publish_preview_relative(
        preparation
    )
    candidates = tuple(
        WorkflowCandidate(
            id=str(getattr(action, "action_id")),
            kind=_status(getattr(action, "kind")),
            ranges=(),
            requires_confirmation=bool(getattr(action, "confirmation_required")),
            evidence=(
                {
                    "affects": list(getattr(action, "affects", ())),
                    "description": str(getattr(action, "description", "")),
                },
            ),
            preview_relative_path=_relative_path(preview_relative_path),
        )
        for action in getattr(plan, "actions")
    )
    return PreparedWorkflow(
        workflow_id="publish_ready",
        plan_digest=str(getattr(plan, "plan_digest")),
        candidates=candidates,
        preparation_status="awaiting_confirmation",
    )


def _rescue_preview_evidence(
    preparation: object,
    output: Path,
    *,
    preserve: bool,
) -> tuple[dict[str, JsonValue], ...]:
    previews = getattr(preparation, "previews", None)
    evidence: list[dict[str, JsonValue]] = []
    for variant_name in ("source", "faithful", "improved"):
        variant = getattr(previews, variant_name, None)
        if variant is None:
            continue
        paths = tuple(Path(path) for path in getattr(variant, "paths", ()))
        ranges = tuple(getattr(variant, "time_ranges", ()))
        if len(paths) != len(ranges):
            raise DemoConfirmationError("rescue preview paths and ranges do not match")
        for path, source_range in zip(paths, ranges, strict=True):
            relative = (
                _REVIEW_PREVIEW_ROOT / "video-rescue" / variant_name / path.name
            ).as_posix()
            if preserve:
                relative = _copy_review_preview(path, output, relative)
            else:
                relative = _relative_path(relative)
            evidence.append(
                {
                    "source": "preview",
                    "variant": variant_name,
                    "relative_path": relative,
                    "ranges": [list(source_range)],
                }
            )
    return tuple(evidence)


def _action_evidence(
    action: object,
    plan: object,
    preview_evidence: tuple[dict[str, JsonValue], ...],
) -> tuple[dict[str, JsonValue], ...]:
    parameters = dict(getattr(action, "parameters", {}))
    evidence: list[dict[str, JsonValue]] = []
    assessment = parameters.pop("assessment_evidence", ())
    if isinstance(assessment, Mapping):
        assessment = (assessment,)
    if isinstance(assessment, Sequence) and not isinstance(assessment, (str, bytes)):
        for item in assessment:
            if isinstance(item, Mapping):
                evidence.append({"source": "assessment_evidence", **dict(item)})
            else:
                evidence.append({"source": "assessment_evidence", "value": item})
    for name in sorted(parameters):
        evidence.append(
            {
                "source": "action_parameter",
                "name": name,
                "value": parameters[name],
            }
        )
    damage_ids = {
        value for value in parameters.get("damage_ids", ()) if isinstance(value, str)
    }
    for interval in getattr(plan, "damage_intervals", ()):
        if getattr(interval, "id", None) in damage_ids:
            evidence.append({"source": "damage_interval", "value": _dump(interval)})
    evidence.extend(preview_evidence)
    return tuple(evidence)


def _rescue_workflow(
    preparation: object,
    *,
    preview_evidence: tuple[dict[str, JsonValue], ...] | None = None,
) -> PreparedWorkflow:
    plan = getattr(preparation, "plan")
    limitations = tuple(getattr(plan, "assessment_limitations", ()))
    preview_evidence = preview_evidence or ()
    candidates = tuple(
        WorkflowCandidate(
            id=str(getattr(action, "id")),
            kind=_status(getattr(action, "kind")),
            ranges=tuple(tuple(item) for item in getattr(action, "source_ranges", ())),
            requires_confirmation=bool(getattr(action, "requires_confirmation")),
            evidence=_action_evidence(action, plan, preview_evidence),
            preview_relative_path=next(
                (
                    str(item["relative_path"])
                    for item in preview_evidence
                    if item.get("variant") == "improved"
                ),
                None,
            ),
            limitations=limitations,
        )
        for action in getattr(plan, "actions")
    )
    return PreparedWorkflow(
        workflow_id="video_rescue",
        plan_digest=str(getattr(plan, "plan_digest")),
        candidates=candidates,
        preparation_status=_status(getattr(preparation, "status")),
    )


def _write_json(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value.model_dump(mode="json")))


def prepare_all(
    source: Path,
    output: Path,
    *,
    manifest_path: Path,
    dependencies: DemoPipelineDependencies | None = None,
) -> PreparedReview:
    """Prepare both real workflows, then release private in-process state."""
    dependencies = dependencies or default_dependencies()
    source_hash, contract_hash = _manifest_binding(source, manifest_path)
    publish = dependencies.publish_factory(
        PublishReadyConfig(
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            output_directory=output / "publish-ready",
        )
    )
    prepared_publish: object | None = None
    try:
        prepared_publish = publish.prepare(source)
        publish_preview = _copy_review_preview(
            Path(getattr(prepared_publish, "preview_path")),
            output,
            _publish_preview_relative(prepared_publish),
        )
        publish_workflow = _publish_workflow(
            prepared_publish, preview_relative_path=publish_preview
        )
        prepared_plan = getattr(prepared_publish, "plan")
        if getattr(prepared_plan, "input_hash") != source_hash:
            raise DemoConfirmationError("publish plan source hash mismatch")
    finally:
        if prepared_publish is not None:
            publish.discard(prepared_publish)

    rescue = dependencies.rescue_factory(
        RescueConfig(
            output_directory=output / "video-rescue",
            strategy=RescueStrategy.BALANCED,
            symptoms=(
                RescueSymptom.DARK,
                RescueSymptom.SOFT_DETAIL,
                RescueSymptom.FLICKER,
                RescueSymptom.SHAKE,
                RescueSymptom.AUDIO_NOISE,
            ),
        )
    )
    prepared_rescue: object | None = None
    try:
        prepared_rescue = rescue.prepare(source)
        rescue_previews = _rescue_preview_evidence(
            prepared_rescue, output, preserve=True
        )
        rescue_workflow = _rescue_workflow(
            prepared_rescue, preview_evidence=rescue_previews
        )
        if getattr(prepared_rescue, "source_hash") != source_hash:
            raise DemoConfirmationError("rescue plan source hash mismatch")
    finally:
        if prepared_rescue is not None:
            rescue.abort(prepared_rescue)

    content_workflow = prepare_useful_content(source, output, dependencies=dependencies)
    safe_sharing_scan = scan_safe_sharing(source, output, dependencies=dependencies)
    safe_sharing_workflow = _privacy_scan_workflow(safe_sharing_scan)

    review = PreparedReview(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        workflows={
            "publish_ready": publish_workflow,
            "video_rescue": rescue_workflow,
            "useful_content": content_workflow,
            "safe_sharing": safe_sharing_workflow,
        },
    )
    _write_json(output / "prepared-review.json", review)
    return review


def prepare_publish_ready(source: Path, output: Path) -> PreparedWorkflow:
    """Prepare only Publish Ready for callers that already own source binding."""
    pipeline = default_dependencies().publish_factory(
        PublishReadyConfig(
            profile_id=PublishProfileId.COMPATIBLE_MP4, output_directory=output
        )
    )
    preparation = pipeline.prepare(source)
    try:
        review_root = output.parent
        preview = _copy_review_preview(
            Path(getattr(preparation, "preview_path")),
            review_root,
            _publish_preview_relative(preparation),
        )
        return _publish_workflow(preparation, preview_relative_path=preview)
    finally:
        pipeline.discard(preparation)


def prepare_rescue(source: Path, output: Path) -> PreparedWorkflow:
    """Prepare only Video Rescue for callers that already own source binding."""
    pipeline = default_dependencies().rescue_factory(
        RescueConfig(
            output_directory=output,
            strategy=RescueStrategy.BALANCED,
            symptoms=(
                RescueSymptom.DARK,
                RescueSymptom.SOFT_DETAIL,
                RescueSymptom.FLICKER,
                RescueSymptom.SHAKE,
                RescueSymptom.AUDIO_NOISE,
            ),
        )
    )
    preparation = pipeline.prepare(source)
    try:
        previews = _rescue_preview_evidence(preparation, output.parent, preserve=True)
        return _rescue_workflow(preparation, preview_evidence=previews)
    finally:
        pipeline.abort(preparation)


def _content_user_ranges(source_hash: str) -> tuple[ContentUserRange, ...]:
    return tuple(
        ContentUserRange(
            id=make_user_range_id(source_hash, ContentUserRangeKind.KEEP, source_range),
            kind=ContentUserRangeKind.KEEP,
            source_range=source_range,
            label=f"Reviewed keep {index}",
        )
        for index, (start, end) in enumerate(_USEFUL_KEEP_RANGES, start=1)
        for source_range in (ContentTimeRange(start_seconds=start, end_seconds=end),)
    )


def _content_pipeline_config(source_hash: str, output: Path) -> ContentPipelineConfig:
    ffmpeg = os.environ.get("VIDEOSCOPE_FFMPEG", "ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_FFPROBE", "ffprobe")
    return ContentPipelineConfig(
        output_directory=output / "useful-content",
        content=ContentConfig(
            goal=ContentGoal.SELECTED_CLIPS,
            export_clips=True,
            minimum_chapter_duration_seconds=1.0,
        ),
        features=StructuralFeatureConfig(ffmpeg=ffmpeg, ffprobe=ffprobe),
        user_ranges=_content_user_ranges(source_hash),
    )


def _content_preview_evidence(
    review: object, output: Path, *, preserve: bool
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    private_root = output / "useful-content" / "content-review-private"
    evidence: dict[str, tuple[dict[str, JsonValue], ...]] = {}
    for preview in getattr(review, "previews", ()):
        action_id = str(getattr(preview, "action_id"))
        items: list[dict[str, JsonValue]] = []
        for value in getattr(preview, "relative_paths", ()):
            source = private_root / Path(str(value))
            relative = (
                _REVIEW_PREVIEW_ROOT / "useful-content" / action_id / source.name
            ).as_posix()
            if preserve:
                relative = _copy_review_preview(source, output, relative)
            items.append(
                {
                    "source": "preview",
                    "relative_path": _relative_path(relative),
                    "identity": str(getattr(preview, "identity")),
                }
            )
        evidence[action_id] = tuple(items)
    return evidence


def _content_workflow(
    review: object,
    *,
    preview_evidence: Mapping[str, tuple[dict[str, JsonValue], ...]] | None = None,
) -> PreparedWorkflow:
    plan = getattr(review, "plan")
    preparation = getattr(review, "preparation")
    preview_evidence = preview_evidence or {}
    keep_candidates = tuple(
        WorkflowCandidate(
            id=str(getattr(item, "id")),
            kind="keep",
            ranges=(
                (
                    float(item.source_range.start_seconds),
                    float(item.source_range.end_seconds),
                ),
            ),
            requires_confirmation=False,
            evidence=(
                {"source": "user_range", "label": str(getattr(item, "label", ""))},
            ),
        )
        for item in getattr(
            getattr(preparation, "content_map", None), "user_ranges", ()
        )
        if _status(getattr(item, "kind", "")) == "keep"
    )
    if not keep_candidates:
        content_map_hash = str(
            getattr(getattr(preparation, "content_map"), "input_hash")
        )
        keep_candidates = tuple(
            WorkflowCandidate(
                id=item.id,
                kind="keep",
                ranges=(
                    (item.source_range.start_seconds, item.source_range.end_seconds),
                ),
                requires_confirmation=True,
                evidence=({"source": "user_range", "label": item.label or ""},),
            )
            for item in _content_user_ranges(content_map_hash)
        )
    action_candidates = tuple(
        WorkflowCandidate(
            id=str(getattr(action, "id")),
            kind=_status(getattr(action, "kind")),
            ranges=tuple(
                (float(item.start_seconds), float(item.end_seconds))
                for item in getattr(action, "source_ranges", ())
            ),
            requires_confirmation=bool(getattr(action, "requires_confirmation")),
            evidence=(
                {"source": "content_action", "value": _dump(action)},
                *preview_evidence.get(str(getattr(action, "id")), ()),
            ),
            preview_relative_path=next(
                (
                    str(item["relative_path"])
                    for item in preview_evidence.get(str(getattr(action, "id")), ())
                ),
                None,
            ),
            limitations=tuple(getattr(preparation, "warnings", ())),
        )
        for action in getattr(plan, "actions", ())
    )
    workflow = PreparedWorkflow(
        workflow_id="useful_content",
        plan_digest=str(getattr(plan, "plan_digest")),
        candidates=(*keep_candidates, *action_candidates),
        preparation_status="ready_to_confirm",
    )
    keep_ranges = tuple(
        candidate.ranges[0]
        for candidate in workflow.candidates
        if candidate.kind == "keep"
    )
    if keep_ranges != _USEFUL_KEEP_RANGES:
        raise DemoConfirmationError("Useful Content keep ranges changed")
    return workflow


def prepare_useful_content(
    source: Path,
    output: Path,
    *,
    dependencies: DemoPipelineDependencies | None = None,
) -> PreparedWorkflow:
    """Prepare and preview exact reviewed keep ranges without confirming them."""
    dependencies = dependencies or default_dependencies()
    source_hash = stream_sha256(source)
    pipeline = dependencies.content_factory(
        _content_pipeline_config(source_hash, output)
    )
    try:
        preparation = pipeline.prepare(source)
        review = pipeline.preview(preparation)
        if getattr(getattr(preparation, "content_map"), "input_hash") != source_hash:
            raise DemoConfirmationError("content plan source hash mismatch")
        previews = _content_preview_evidence(review, output, preserve=True)
        return _content_workflow(review, preview_evidence=previews)
    finally:
        pipeline.close()


def _manual_privacy_inputs() -> tuple[
    tuple[ManualVisualRegionInput, ...], tuple[ManualAudioIntervalInput, ...]
]:
    return (
        (
            ManualVisualRegionInput(
                start_seconds=_PRIVACY_START_SECONDS,
                end_seconds=_PRIVACY_END_SECONDS,
                box=NormalizedBox(x_min=0.58, y_min=0.18, x_max=0.94, y_max=0.78),
                style=RedactionStyle.SOLID_FILL,
            ),
        ),
        (
            ManualAudioIntervalInput(
                start_seconds=_PRIVACY_START_SECONDS,
                end_seconds=_PRIVACY_END_SECONDS,
                style=RedactionStyle.MUTE,
            ),
        ),
    )


def _scan_digest(scan: object) -> str:
    risk_map = getattr(scan, "risk_map")
    executions = []
    for item in getattr(scan, "scanner_executions", ()):
        dumped = _dump(item)
        dumped.pop("elapsed_seconds", None)
        executions.append(dumped)
    payload = {
        "risk_map": _dump(risk_map),
        "scanner_executions": executions,
        "warnings": list(getattr(scan, "warnings", ())),
    }
    import hashlib

    return hashlib.sha256(_canonical(payload)).hexdigest()


def _privacy_risks(preparation: SafeSharingScanPreparation) -> tuple[object, ...]:
    risk_map = getattr(preparation.scan, "risk_map")
    source_hash = str(getattr(risk_map, "input_hash"))
    duration = float(getattr(risk_map, "duration_seconds", 42.0))
    visual = tuple(
        build_manual_visual_risk(
            source_hash,
            item.model_copy(update={"source_duration_seconds": duration}),
        )
        for item in preparation.manual_visual_regions
    )
    audio = tuple(
        build_manual_audio_risk(
            source_hash,
            item.model_copy(update={"source_duration_seconds": duration}),
        )
        for item in preparation.manual_audio_intervals
    )
    return (*tuple(getattr(risk_map, "risks", ())), *visual, *audio)


def _privacy_scan_workflow(preparation: SafeSharingScanPreparation) -> PreparedWorkflow:
    candidates = tuple(
        WorkflowCandidate(
            id=str(getattr(risk, "id")),
            kind=_status(getattr(risk, "risk_type")),
            ranges=(
                (
                    float(getattr(risk, "start_seconds")),
                    float(getattr(risk, "end_seconds")),
                ),
            ),
            requires_confirmation=True,
            evidence=(
                {
                    "source": "privacy_risk",
                    "scanner_id": str(getattr(risk, "scanner_id")),
                    "public_description": str(getattr(risk, "public_description")),
                    "recommended_style": (
                        _status(getattr(risk, "recommended_style"))
                        if getattr(risk, "recommended_style", None) is not None
                        else None
                    ),
                    "box": (
                        getattr(risk, "box").model_dump(mode="json")
                        if getattr(risk, "box", None) is not None
                        else None
                    ),
                },
            ),
            limitations=tuple(getattr(risk, "limitations", ())),
        )
        for risk in _privacy_risks(preparation)
    )
    workflow = PreparedWorkflow(
        workflow_id="safe_sharing",
        plan_digest=preparation.scan_digest,
        candidates=candidates,
        preparation_status="awaiting_privacy_review",
    )
    manual_visual = tuple(
        item for item in workflow.candidates if item.kind == "manual_visual"
    )
    manual_audio = tuple(
        item for item in workflow.candidates if item.kind == "manual_audio"
    )
    expected_range = ((_PRIVACY_START_SECONDS, _PRIVACY_END_SECONDS),)
    expected_box = {
        "x_min": 0.58,
        "y_min": 0.18,
        "x_max": 0.94,
        "y_max": 0.78,
    }
    if (
        len(manual_visual) != 1
        or manual_visual[0].ranges != expected_range
        or manual_visual[0].evidence[0].get("box") != expected_box
        or len(manual_audio) != 1
        or manual_audio[0].ranges != expected_range
    ):
        raise DemoConfirmationError("Safe Sharing manual selections changed")
    return workflow


def scan_safe_sharing(
    source: Path,
    output: Path,
    *,
    dependencies: DemoPipelineDependencies | None = None,
) -> SafeSharingScanPreparation:
    """Scan and propose exact manual risks; never review or implicitly allow."""
    dependencies = dependencies or default_dependencies()
    output.mkdir(parents=True, exist_ok=True)
    lifecycle_root = Path(tempfile.mkdtemp(prefix=".safe-sharing-scan-", dir=output))
    pipeline = dependencies.privacy_factory(lifecycle_root)
    scan: object | None = None
    try:
        scan = pipeline.scan(
            source=source,
            config=SafeSharingConfig(audience="public", sample_fps=5.0),
        )
        if getattr(getattr(scan, "risk_map"), "profile", "public") != "public":
            raise DemoConfirmationError("Safe Sharing demo requires public audience")
        if getattr(getattr(scan, "risk_map"), "input_hash") != stream_sha256(source):
            raise DemoConfirmationError("Safe Sharing scan source hash mismatch")
        visual, audio = _manual_privacy_inputs()
        return SafeSharingScanPreparation(
            scan=scan,
            scan_digest=_scan_digest(scan),
            manual_visual_regions=visual,
            manual_audio_intervals=audio,
        )
    finally:
        if scan is not None:
            pipeline.discard(str(getattr(scan, "scan_id")))
        shutil.rmtree(lifecycle_root, ignore_errors=True)


def _validate_privacy_review(
    prepared: PreparedWorkflow,
    review: PrivacyReviewFile,
    *,
    source_hash: str,
    contract_hash: str,
) -> None:
    if review.source_sha256 != source_hash or review.contract_sha256 != contract_hash:
        raise DemoConfirmationError(
            "privacy review binding does not match source or contract"
        )
    if prepared.plan_digest != review.scan_digest:
        raise DemoConfirmationError(
            "privacy review scan digest does not match preparation"
        )
    expected_ids = {item.id for item in prepared.candidates}
    choice_ids = [item.risk_id for item in review.choices]
    if len(choice_ids) != len(set(choice_ids)) or set(choice_ids) != expected_ids:
        raise DemoConfirmationError(
            "privacy review requires exactly one choice per risk"
        )
    candidate_by_id = {item.id: item for item in prepared.candidates}
    for choice in review.choices:
        candidate = candidate_by_id[choice.risk_id]
        if candidate.kind == "metadata" and (
            choice.decision != "redact" or choice.style != "remove_metadata"
        ):
            raise DemoConfirmationError("metadata risk requires remove_metadata")
        if candidate.kind == "manual_visual" and (
            choice.decision != "redact" or choice.style != "solid_fill"
        ):
            raise DemoConfirmationError("manual visual risk requires solid_fill")
        if candidate.kind == "manual_audio" and (
            choice.decision != "redact" or choice.style != "mute"
        ):
            raise DemoConfirmationError("manual audio risk requires mute")


def _review_decisions(review: PrivacyReviewFile) -> tuple[PrivacyReviewDecision, ...]:
    return tuple(
        PrivacyReviewDecision(
            risk_id=item.risk_id,
            decision=PrivacyDecision(item.decision),
            style=RedactionStyle(item.style) if item.style is not None else None,
            reviewed_at=review.reviewed_at,
        )
        for item in review.choices
    )


def _privacy_plan_workflow(
    preparation: object, preview_relative: str
) -> PreparedWorkflow:
    plan = getattr(preparation, "plan")
    candidates = tuple(
        WorkflowCandidate(
            id=str(getattr(action, "id")),
            kind=_status(getattr(action, "kind")),
            ranges=(
                (
                    float(getattr(action, "start_seconds")),
                    float(getattr(action, "end_seconds")),
                ),
            ),
            requires_confirmation=bool(getattr(action, "requires_confirmation")),
            evidence=(
                {
                    "source": "privacy_action",
                    "box": (
                        getattr(action, "box").model_dump(mode="json")
                        if getattr(action, "box", None) is not None
                        else None
                    ),
                    "parameters": _dump(getattr(action, "parameters", {})),
                },
            ),
            preview_relative_path=preview_relative,
        )
        for action in getattr(plan, "actions", ())
    )
    return PreparedWorkflow(
        workflow_id="safe_sharing",
        plan_digest=str(getattr(plan, "digest")),
        candidates=candidates,
        preparation_status="ready_to_confirm",
    )


def _fresh_privacy_plan(
    source: Path,
    output: Path,
    prepared_scan: PreparedWorkflow,
    review_file: PrivacyReviewFile,
    *,
    source_hash: str,
    contract_hash: str,
    dependencies: DemoPipelineDependencies,
    preserve_preview: bool,
) -> tuple[object, object, object, PreparedWorkflow]:
    output.mkdir(parents=True, exist_ok=True)
    lifecycle_root = (
        Path(tempfile.mkdtemp(prefix=".safe-sharing-preview-", dir=output))
        if preserve_preview
        else output / "safe-sharing"
    )
    pipeline: Any = dependencies.privacy_factory(lifecycle_root)
    scan: object | None = None
    try:
        scan = pipeline.scan(
            source=source, config=SafeSharingConfig(audience="public", sample_fps=5.0)
        )
        visual, audio = _manual_privacy_inputs()
        scan_preparation = SafeSharingScanPreparation(
            scan=scan,
            scan_digest=_scan_digest(scan),
            manual_visual_regions=visual,
            manual_audio_intervals=audio,
        )
        fresh_scan = _privacy_scan_workflow(scan_preparation)
        _same_preparation(prepared_scan, fresh_scan)
        _validate_privacy_review(
            prepared_scan,
            review_file,
            source_hash=source_hash,
            contract_hash=contract_hash,
        )
        reviewed = pipeline.review(
            getattr(scan, "scan_id"),
            _review_decisions(review_file),
            manual_visual_regions=visual,
            manual_audio_intervals=audio,
        )
        preparation = pipeline.prepare(getattr(reviewed, "review_id"))
        preview = Path(pipeline.preview(getattr(preparation, "preparation_id")))
        relative = (_REVIEW_PREVIEW_ROOT / "safe-sharing" / preview.name).as_posix()
        if preserve_preview:
            relative = _copy_review_preview(preview, output, relative)
        workflow = _privacy_plan_workflow(preparation, _relative_path(relative))
        return pipeline, scan, preparation, workflow
    except BaseException:
        if scan is not None:
            pipeline.discard(str(getattr(scan, "scan_id")))
        if preserve_preview:
            shutil.rmtree(lifecycle_root, ignore_errors=True)
        raise


def preview_safe_sharing(
    prepared: PreparedWorkflow,
    review_file: PrivacyReviewFile,
    *,
    source: Path,
    output: Path,
    source_hash: str,
    contract_hash: str,
    dependencies: DemoPipelineDependencies | None = None,
) -> PreparedWorkflow:
    """Re-scan and build a confirmable D plan without confirming it."""
    dependencies = dependencies or default_dependencies()
    pipeline, scan, _preparation, workflow = _fresh_privacy_plan(
        source,
        output,
        prepared,
        review_file,
        source_hash=source_hash,
        contract_hash=contract_hash,
        dependencies=dependencies,
        preserve_preview=True,
    )
    cast(Any, pipeline).discard(str(getattr(scan, "scan_id")))
    lifecycle_root = Path(getattr(pipeline, "_output", output))
    if lifecycle_root.name.startswith(".safe-sharing-preview-"):
        shutil.rmtree(lifecycle_root, ignore_errors=True)
    return workflow


def _same_preparation(expected: PreparedWorkflow, fresh: PreparedWorkflow) -> None:
    if expected.plan_digest != fresh.plan_digest:
        raise DemoConfirmationError("plan digest does not match fresh preparation")
    expected_candidates = [item.model_dump(mode="json") for item in expected.candidates]
    fresh_candidates = [item.model_dump(mode="json") for item in fresh.candidates]
    if _canonical(expected_candidates) != _canonical(fresh_candidates):
        raise DemoConfirmationError(
            "candidate identity or range does not match fresh preparation"
        )


def _check_confirmation(
    prepared: PreparedWorkflow, confirmation: WorkflowConfirmation
) -> None:
    if confirmation.workflow_id != prepared.workflow_id:
        raise DemoConfirmationError(
            "confirmation workflow does not match prepared workflow"
        )
    if confirmation.plan_digest != prepared.plan_digest:
        raise DemoConfirmationError(
            "confirmation digest does not match prepared workflow"
        )
    candidate_ids = {candidate.id for candidate in prepared.candidates}
    if not set(confirmation.accepted_action_ids).issubset(candidate_ids):
        raise DemoConfirmationError(
            "confirmation action does not match prepared candidates"
        )


def _check_exact_action_confirmation(
    prepared: PreparedWorkflow, confirmation: WorkflowConfirmation
) -> None:
    _check_confirmation(prepared, confirmation)
    required = tuple(
        item.id for item in prepared.candidates if item.requires_confirmation
    )
    if confirmation.accepted_action_ids != required:
        raise DemoConfirmationError("confirmation must accept exact action IDs")


def _validate_confirmation_document(
    confirmable: ConfirmablePlan, confirmation: ExecutionConfirmation
) -> None:
    if confirmation.source_sha256 != confirmable.source_sha256 or (
        confirmation.contract_sha256 != confirmable.contract_sha256
    ):
        raise DemoConfirmationError(
            "confirmation binding does not match confirmable plan"
        )
    if set(confirmation.workflows) != set(confirmable.workflows):
        raise DemoConfirmationError("confirmation must echo every confirmable workflow")
    for workflow_id, prepared in confirmable.workflows.items():
        selected = confirmation.workflows[workflow_id]
        _check_confirmation(prepared, selected)
        if workflow_id in {"useful_content", "safe_sharing"}:
            _check_exact_action_confirmation(prepared, selected)


def _relative_existing(path: object, root: Path) -> str | None:
    if not isinstance(path, Path) or not path.is_file():
        return None
    relative = safe_relative_path(path, root)
    if not isinstance(relative, str):
        raise DemoConfirmationError("artifact path could not be normalized")
    return relative


def execute_publish_ready(
    prepared: PreparedWorkflow,
    confirmation: WorkflowConfirmation,
    *,
    source: Path,
    output: Path,
    dependencies: DemoPipelineDependencies | None = None,
) -> WorkflowOutcome:
    _check_confirmation(prepared, confirmation)
    dependencies = dependencies or default_dependencies()
    before = stream_sha256(source)
    pipeline = dependencies.publish_factory(
        PublishReadyConfig(
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            output_directory=output / "publish-ready",
        )
    )
    fresh_preparation: object | None = None
    try:
        fresh_preparation = pipeline.prepare(source)
        fresh = _publish_workflow(
            fresh_preparation,
            preview_relative_path=_publish_preview_relative(fresh_preparation),
        )
        _same_preparation(prepared, fresh)
        fresh_plan = getattr(fresh_preparation, "plan")
        if getattr(fresh_plan, "input_hash") != before:
            raise DemoConfirmationError("fresh publish source hash mismatch")
        result = pipeline.execute(
            fresh_preparation, confirmed_plan_digest=confirmation.plan_digest
        )
        after = stream_sha256(source)
        if after != before:
            raise DemoConfirmationError("source changed during Publish Ready execution")
        raw_status = _status(getattr(result, "status"))
        verification = _status(
            getattr(getattr(result, "technical_report").verification, "status")
        )
        status = raw_status
        if (
            raw_status == "completed"
            and verification != VerificationStatus.PASSED.value
        ):
            status = "failed" if verification == "failed" else "needs_review"
        root = Path(getattr(result, "output_directory", output / "publish-ready"))
        artifacts = {
            name: relative
            for name, path in {
                "video": getattr(result, "video_path", None),
                "cover": getattr(result, "cover_path", None),
                "report": getattr(result, "technical_report_path", None),
            }.items()
            if (relative := _relative_existing(path, root)) is not None
        }
        report = getattr(result, "technical_report")
        return WorkflowOutcome(
            workflow_id="publish_ready",
            status=status,
            source_sha256_before=before,
            source_sha256_after=after,
            actions=tuple(
                _dump(item)
                for item in getattr(getattr(result, "change_log", None), "actions", ())
            ),
            checks=tuple(
                _dump(item) for item in getattr(report.verification, "checks", ())
            ),
            artifacts=artifacts,
            limitations=tuple(
                getattr(report.verification, "manual_review_reasons", ())
            ),
            final_human_review_required=status != "completed",
        )
    finally:
        if fresh_preparation is not None:
            pipeline.discard(fresh_preparation)


def execute_rescue(
    prepared: PreparedWorkflow,
    confirmation: WorkflowConfirmation,
    *,
    source: Path,
    output: Path,
    dependencies: DemoPipelineDependencies | None = None,
) -> WorkflowOutcome:
    _check_confirmation(prepared, confirmation)
    dependencies = dependencies or default_dependencies()
    before = stream_sha256(source)
    pipeline = dependencies.rescue_factory(
        RescueConfig(
            output_directory=output / "video-rescue",
            strategy=RescueStrategy.BALANCED,
            symptoms=(
                RescueSymptom.DARK,
                RescueSymptom.SOFT_DETAIL,
                RescueSymptom.FLICKER,
                RescueSymptom.SHAKE,
                RescueSymptom.AUDIO_NOISE,
            ),
        )
    )
    fresh_preparation: object | None = None
    execution_started = False
    try:
        fresh_preparation = pipeline.prepare(source)
        fresh_previews = _rescue_preview_evidence(
            fresh_preparation, output, preserve=False
        )
        fresh = _rescue_workflow(fresh_preparation, preview_evidence=fresh_previews)
        _same_preparation(prepared, fresh)
        if getattr(fresh_preparation, "source_hash") != before:
            raise DemoConfirmationError("fresh rescue source hash mismatch")
        action_kinds = {candidate.id: candidate.kind for candidate in fresh.candidates}
        publish_improved = any(
            action_kinds[action_id] in _IMPROVEMENT_KINDS
            for action_id in confirmation.accepted_action_ids
        )
        core_confirmation = RescueConfirmation(
            plan_digest=confirmation.plan_digest,
            publish_faithful=True,
            publish_improved=publish_improved,
            accepted_action_ids=confirmation.accepted_action_ids,
            accepted_trim_damage_ids=confirmation.accepted_trim_damage_ids,
        )
        pipeline.confirm(fresh_preparation, core_confirmation)
        execution_started = True
        result = pipeline.execute(fresh_preparation, core_confirmation)
        after = stream_sha256(source)
        if after != before:
            raise DemoConfirmationError("source changed during Video Rescue execution")
        root = Path(
            getattr(result, "public_root", output / "video-rescue")
            or output / "video-rescue"
        )
        artifacts = {
            name: relative
            for name, path in {
                "faithful": getattr(result, "faithful_path", None),
                "improved": getattr(result, "improved_path", None),
                "report": getattr(result, "report_path", None),
            }.items()
            if (relative := _relative_existing(path, root)) is not None
        }
        status = _status(getattr(result, "status"))
        report = getattr(result, "technical_report", None)
        verification = getattr(report, "verification", None)
        faithful_verification = _status(
            getattr(verification, "faithful_status", "failed")
        )
        if "faithful" not in artifacts:
            status = "failed"
        elif status == "completed" and faithful_verification != "passed":
            status = (
                "needs_review" if faithful_verification == "needs_review" else "failed"
            )
        elif publish_improved and "improved" not in artifacts and status == "completed":
            status = "partial"
        return WorkflowOutcome(
            workflow_id="video_rescue",
            status=status,
            source_sha256_before=before,
            source_sha256_after=after,
            checks=tuple(_dump(item) for item in getattr(verification, "checks", ())),
            artifacts=artifacts,
            limitations=tuple(getattr(report, "limitations", ())),
            final_human_review_required=status != "completed",
        )
    finally:
        if fresh_preparation is not None and not execution_started:
            pipeline.abort(fresh_preparation)


def execute_useful_content(
    prepared: PreparedWorkflow,
    confirmation: WorkflowConfirmation,
    *,
    source: Path,
    output: Path,
    dependencies: DemoPipelineDependencies | None = None,
) -> WorkflowOutcome:
    _check_exact_action_confirmation(prepared, confirmation)
    dependencies = dependencies or default_dependencies()
    before = stream_sha256(source)
    pipeline = dependencies.content_factory(_content_pipeline_config(before, output))
    try:
        preparation = pipeline.prepare(source)
        review = pipeline.preview(preparation)
        fresh_previews = _content_preview_evidence(review, output, preserve=False)
        fresh = _content_workflow(review, preview_evidence=fresh_previews)
        _same_preparation(prepared, fresh)
        if getattr(getattr(preparation, "content_map"), "input_hash") != before:
            raise DemoConfirmationError("fresh content source hash mismatch")
        content_confirmation = pipeline.confirm(
            review,
            accepted_action_ids=confirmation.accepted_action_ids,
        )
        result = pipeline.execute(review, content_confirmation)
        after = stream_sha256(source)
        if after != before:
            raise DemoConfirmationError(
                "source changed during Useful Content execution"
            )
        status = _status(getattr(result, "status"))
        verification = getattr(result, "verification", None)
        verification_outcome = _status(getattr(verification, "outcome", "failed"))
        if status == "completed" and verification_outcome != "completed":
            status = verification_outcome
        root = Path(getattr(result, "public_root", None) or output / "useful-content")
        artifacts: dict[str, str] = {}
        for index, artifact in enumerate(getattr(result, "artifacts", ())):
            relative = _relative_path(str(getattr(artifact, "relative_path")))
            path = output / "useful-content" / Path(relative)
            if path.is_file():
                artifacts[f"artifact_{index:03d}"] = safe_relative_path(path, root)
        report = getattr(result, "technical_report", None)
        mappings = tuple(_dump(item) for item in getattr(report, "source_mappings", ()))
        return WorkflowOutcome(
            workflow_id="useful_content",
            status=status,
            source_sha256_before=before,
            source_sha256_after=after,
            actions=mappings,
            checks=tuple(_dump(item) for item in getattr(verification, "checks", ())),
            artifacts=artifacts,
            limitations=tuple(getattr(report, "limitations", ())),
            final_human_review_required=status != "completed",
        )
    finally:
        pipeline.close()


def execute_safe_sharing(
    prepared_scan: PreparedWorkflow,
    confirmable: PreparedWorkflow,
    review_file: PrivacyReviewFile,
    confirmation: WorkflowConfirmation,
    *,
    source: Path,
    output: Path,
    source_hash: str,
    contract_hash: str,
    dependencies: DemoPipelineDependencies | None = None,
) -> WorkflowOutcome:
    _check_exact_action_confirmation(confirmable, confirmation)
    dependencies = dependencies or default_dependencies()
    before = stream_sha256(source)
    if before != source_hash:
        raise DemoConfirmationError("Safe Sharing source hash changed")
    pipeline_value, scan, preparation, fresh = _fresh_privacy_plan(
        source,
        output,
        prepared_scan,
        review_file,
        source_hash=source_hash,
        contract_hash=contract_hash,
        dependencies=dependencies,
        preserve_preview=False,
    )
    consumed = False
    try:
        _same_preparation(confirmable, fresh)
        result = cast(Any, pipeline_value).confirm(
            str(getattr(preparation, "preparation_id")),
            confirmation.plan_digest,
        )
        consumed = True
        after = stream_sha256(source)
        if after != before:
            raise DemoConfirmationError("source changed during Safe Sharing execution")
        status = _status(getattr(result, "status"))
        verification = getattr(result, "verification", None)
        verification_status = _status(getattr(verification, "status", status))
        if (
            status == PrivacyJobOutcome.COMPLETED.value
            and verification_status != "completed"
        ):
            status = verification_status
        root = output / "safe-sharing"
        artifacts = {
            name: relative
            for name, value in {
                "video": getattr(result, "video_relative_path", None),
                "verification": getattr(result, "verification_relative_path", None),
                "report": getattr(result, "technical_report_relative_path", None),
            }.items()
            if isinstance(value, str)
            and (
                relative := _relative_existing(root / Path(_relative_path(value)), root)
            )
            is not None
        }
        return WorkflowOutcome(
            workflow_id="safe_sharing",
            status=status,
            source_sha256_before=before,
            source_sha256_after=after,
            actions=tuple(
                _dump(item)
                for item in getattr(getattr(preparation, "plan"), "actions", ())
            ),
            checks=tuple(_dump(item) for item in getattr(verification, "checks", ())),
            artifacts=artifacts,
            limitations=tuple(getattr(verification, "limitations", ())),
            final_human_review_required=True,
        )
    finally:
        if not consumed:
            cast(Any, pipeline_value).discard(str(getattr(scan, "scan_id")))


def execute_from_confirmation(
    review: PreparedReview,
    confirmation: ExecutionConfirmation,
    *,
    source: Path,
    output: Path,
    dependencies: DemoPipelineDependencies | None = None,
    privacy_review: PrivacyReviewFile | None = None,
    confirmable_plan: ConfirmablePlan | None = None,
) -> dict[str, WorkflowOutcome]:
    """Execute only separately authored confirmations bound to one review file."""
    source_hash = stream_sha256(source)
    if source_hash != review.source_sha256 or source_hash != confirmation.source_sha256:
        raise DemoConfirmationError("source hash does not match prepared review")
    if confirmation.contract_sha256 != review.contract_sha256:
        raise DemoConfirmationError("contract digest does not match prepared review")
    outcomes: dict[str, WorkflowOutcome] = {}
    for workflow_id, prepared in review.workflows.items():
        selected = confirmation.workflows.get(workflow_id)
        if selected is None:
            continue
        if workflow_id == "publish_ready":
            outcomes[workflow_id] = execute_publish_ready(
                prepared,
                selected,
                source=source,
                output=output,
                dependencies=dependencies,
            )
        elif workflow_id == "video_rescue":
            outcomes[workflow_id] = execute_rescue(
                prepared,
                selected,
                source=source,
                output=output,
                dependencies=dependencies,
            )
        elif workflow_id == "useful_content":
            if confirmable_plan is None:
                raise DemoConfirmationError(
                    "Useful Content requires a confirmable plan"
                )
            confirmable = confirmable_plan.workflows.get(workflow_id)
            if confirmable is None:
                raise DemoConfirmationError("confirmable plan omits Useful Content")
            _same_preparation(prepared, confirmable)
            outcomes[workflow_id] = execute_useful_content(
                confirmable,
                selected,
                source=source,
                output=output,
                dependencies=dependencies,
            )
        elif workflow_id == "safe_sharing":
            if privacy_review is None or confirmable_plan is None:
                raise DemoConfirmationError(
                    "Safe Sharing requires privacy review and confirmable plan"
                )
            confirmable = confirmable_plan.workflows.get(workflow_id)
            if confirmable is None:
                raise DemoConfirmationError("confirmable plan omits Safe Sharing")
            outcomes[workflow_id] = execute_safe_sharing(
                prepared,
                confirmable,
                privacy_review,
                selected,
                source=source,
                output=output,
                source_hash=review.source_sha256,
                contract_hash=review.contract_sha256,
                dependencies=dependencies,
            )
    return outcomes


def _read_model(path: Path, model: type[BaseModel]) -> BaseModel:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DemoConfirmationError(f"cannot read {path.name}") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies: DemoPipelineDependencies | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    prepare_parser = phases.add_parser("prepare")
    prepare_parser.add_argument("--source", type=Path, required=True)
    prepare_parser.add_argument("--manifest", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    preview_parser = phases.add_parser("preview")
    preview_parser.add_argument("--prepared", type=Path, required=True)
    preview_parser.add_argument("--privacy-review", type=Path, required=True)
    preview_parser.add_argument("--output", type=Path, required=True)
    execute_parser = phases.add_parser("execute")
    execute_parser.add_argument("--prepared", type=Path, required=True)
    execute_parser.add_argument("--privacy-review", type=Path)
    execute_parser.add_argument("--confirmable-plan", type=Path)
    execute_parser.add_argument("--confirmation", type=Path, required=True)
    execute_parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args(argv)
    if args.phase == "prepare":
        prepare_all(
            args.source,
            args.output,
            manifest_path=args.manifest,
            dependencies=dependencies,
        )
        return 0
    if args.phase == "preview":
        output = args.output
        source = output / _SOURCE_NAME
        manifest = output / _MANIFEST_NAME
        review = _read_model(args.prepared, PreparedReview)
        privacy_review = _read_model(args.privacy_review, PrivacyReviewFile)
        assert isinstance(review, PreparedReview) and isinstance(
            privacy_review, PrivacyReviewFile
        )
        source_hash, contract_hash = _manifest_binding(source, manifest)
        if (
            source_hash != review.source_sha256
            or contract_hash != review.contract_sha256
        ):
            raise DemoConfirmationError("prepared review does not match manifest")
        prepared_privacy = review.workflows.get("safe_sharing")
        if prepared_privacy is None:
            raise DemoConfirmationError("prepared review omits Safe Sharing")
        privacy_plan = preview_safe_sharing(
            prepared_privacy,
            privacy_review,
            source=source,
            output=output,
            source_hash=source_hash,
            contract_hash=contract_hash,
            dependencies=dependencies,
        )
        workflows = dict(review.workflows)
        workflows["safe_sharing"] = privacy_plan
        confirmable = ConfirmablePlan(
            source_sha256=source_hash,
            contract_sha256=contract_hash,
            workflows=workflows,
        )
        _write_json(output / "confirmable-plan.json", confirmable)
        return 0
    output = args.prepared.parent
    source = output / _SOURCE_NAME
    manifest = output / _MANIFEST_NAME
    review = _read_model(args.prepared, PreparedReview)
    confirmation = _read_model(args.confirmation, ExecutionConfirmation)
    assert isinstance(review, PreparedReview) and isinstance(
        confirmation, ExecutionConfirmation
    )
    privacy_review_value = (
        _read_model(args.privacy_review, PrivacyReviewFile)
        if args.privacy_review is not None
        else None
    )
    confirmable_value = (
        _read_model(args.confirmable_plan, ConfirmablePlan)
        if args.confirmable_plan is not None
        else None
    )
    assert privacy_review_value is None or isinstance(
        privacy_review_value, PrivacyReviewFile
    )
    assert confirmable_value is None or isinstance(confirmable_value, ConfirmablePlan)
    parsed_privacy_review: PrivacyReviewFile | None = privacy_review_value
    parsed_confirmable: ConfirmablePlan | None = confirmable_value
    source_hash, contract_hash = _manifest_binding(source, manifest)
    if source_hash not in {review.source_sha256, confirmation.source_sha256} or (
        review.source_sha256 != confirmation.source_sha256
    ):
        raise DemoConfirmationError("source hash does not match manifest binding")
    if (
        contract_hash
        not in {
            review.contract_sha256,
            confirmation.contract_sha256,
        }
        or review.contract_sha256 != confirmation.contract_sha256
    ):
        raise DemoConfirmationError("contract digest does not match manifest binding")
    selected = {item.replace("-", "_") for item in args.only}
    if selected.intersection({"useful_content", "safe_sharing"}):
        if parsed_confirmable is None:
            raise DemoConfirmationError("C/D execution requires confirmable-plan.json")
        if (
            parsed_confirmable.source_sha256 != review.source_sha256
            or parsed_confirmable.contract_sha256 != review.contract_sha256
        ):
            raise DemoConfirmationError(
                "confirmable plan binding does not match review"
            )
        _validate_confirmation_document(parsed_confirmable, confirmation)
    if selected:
        review = review.model_copy(
            update={
                "workflows": {
                    key: value
                    for key, value in review.workflows.items()
                    if key in selected
                }
            }
        )
        confirmation = confirmation.model_copy(
            update={
                "workflows": {
                    key: value
                    for key, value in confirmation.workflows.items()
                    if key in selected
                }
            }
        )
    outcome_path = output / "execution-outcomes.json"
    existing_outcomes = (
        _read_existing_execution_outcomes(
            outcome_path,
            source_hash=source_hash,
            contract_hash=contract_hash,
        )
        if selected
        else {}
    )
    outcomes = execute_from_confirmation(
        review,
        confirmation,
        source=source,
        output=output,
        dependencies=dependencies,
        privacy_review=parsed_privacy_review,
        confirmable_plan=parsed_confirmable,
    )
    outcome_document = (
        _OutcomeDocument(
            source_sha256=source_hash,
            contract_sha256=contract_hash,
            outcomes={**existing_outcomes, **outcomes},
        )
        if selected
        else _OutcomeDocument(
            source_sha256=source_hash,
            contract_sha256=contract_hash,
            outcomes=outcomes,
        )
    )
    _write_json(outcome_path, outcome_document)
    return 0


class _OutcomeDocument(_DemoModel):
    schema_version: Literal["1"] = "1"
    source_sha256: str = Field(pattern=_SHA256)
    contract_sha256: str = Field(pattern=_SHA256)
    outcomes: dict[str, WorkflowOutcome]

    @model_validator(mode="after")
    def validate_outcomes(self) -> _OutcomeDocument:
        if any(key not in _WORKFLOWS for key in self.outcomes):
            raise ValueError("outcome document contains an unknown workflow")
        if any(key != value.workflow_id for key, value in self.outcomes.items()):
            raise ValueError("outcome map keys must match workflow IDs")
        if any(
            value.source_sha256_before != self.source_sha256
            or value.source_sha256_after != self.source_sha256
            for value in self.outcomes.values()
        ):
            raise ValueError("outcome source hash does not match document binding")
        return self


def _merge_execution_outcomes(
    path: Path,
    replacements: Mapping[str, WorkflowOutcome],
    *,
    source_hash: str,
    contract_hash: str,
) -> _OutcomeDocument:
    """Preserve only strictly bound known outcomes during a selected execution."""
    outcomes = _read_existing_execution_outcomes(
        path,
        source_hash=source_hash,
        contract_hash=contract_hash,
    )
    outcomes.update(replacements)
    try:
        return _OutcomeDocument(
            source_sha256=source_hash,
            contract_sha256=contract_hash,
            outcomes=outcomes,
        )
    except ValueError as exc:
        raise DemoConfirmationError(
            "cannot merge unbound outcome workflow data"
        ) from exc


def _read_existing_execution_outcomes(
    path: Path,
    *,
    source_hash: str,
    contract_hash: str,
) -> dict[str, WorkflowOutcome]:
    """Read only a strict outcome document bound to the current demo inputs."""
    if path.exists():
        try:
            existing_value = _OutcomeDocument.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise DemoConfirmationError(
                "cannot merge existing outcome workflow document"
            ) from exc
        if (
            existing_value.source_sha256 != source_hash
            or existing_value.contract_sha256 != contract_hash
        ):
            raise DemoConfirmationError(
                "existing outcome binding does not match current source and contract"
            )
        return dict(existing_value.outcomes)
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
