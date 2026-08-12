"""Prepare and execute the full-local Publish Ready and Rescue demo safely."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from scripts.full_local_demo_contract import safe_relative_path, stream_sha256
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
_WORKFLOWS = ("publish_ready", "video_rescue")
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


def default_dependencies() -> DemoPipelineDependencies:
    return DemoPipelineDependencies(
        publish_factory=PublishReadyPipeline,
        rescue_factory=VideoRescuePipeline,
        content_factory=_unavailable_factory,
        privacy_factory=_unavailable_factory,
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
            return dumped
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": str(value)}


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

    review = PreparedReview(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        workflows={"publish_ready": publish_workflow, "video_rescue": rescue_workflow},
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


def execute_from_confirmation(
    review: PreparedReview,
    confirmation: ExecutionConfirmation,
    *,
    source: Path,
    output: Path,
    dependencies: DemoPipelineDependencies | None = None,
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
    execute_parser = phases.add_parser("execute")
    execute_parser.add_argument("--prepared", type=Path, required=True)
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
    output = args.prepared.parent
    source = output / _SOURCE_NAME
    manifest = output / _MANIFEST_NAME
    review = _read_model(args.prepared, PreparedReview)
    confirmation = _read_model(args.confirmation, ExecutionConfirmation)
    assert isinstance(review, PreparedReview) and isinstance(
        confirmation, ExecutionConfirmation
    )
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
    outcomes = execute_from_confirmation(
        review,
        confirmation,
        source=source,
        output=output,
        dependencies=dependencies,
    )
    _write_json(output / "execution-outcomes.json", _OutcomeDocument(outcomes=outcomes))
    return 0


class _OutcomeDocument(_DemoModel):
    outcomes: dict[str, WorkflowOutcome]


if __name__ == "__main__":
    raise SystemExit(main())
