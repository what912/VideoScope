"""Strict, immutable contracts for Long Video to Useful Content."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

CONTENT_SCHEMA_VERSION: Final = "0.1"
_SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN: Final = r"^[a-z][a-z0-9_-]*$"
_WINDOWS_ABSOLUTE_PATH_PATTERN: Final = re.compile(r"[A-Za-z]:[\\/]")
_UNIX_ABSOLUTE_PATH_PATTERN: Final = re.compile(r"(?<![A-Za-z0-9_.-])/\S+")
_UNC_PATH_PATTERN: Final = re.compile(r"\\\\[^\\/\s]+[\\/]")

CONTENT_REQUIRED_VERIFICATION_CHECK_IDS: Final = (
    "decodable",
    "duration",
    "streams",
    "source_map",
    "locked_ranges",
    "source_order",
    "join_regression",
    "audio_continuity",
    "av_sync",
    "chapters_subtitles",
    "public_artifacts",
    "source_read_only",
)


class _FrozenDict(dict[str, Any]):
    def _reject(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen JSON object does not support mutation")

    __setitem__ = _reject
    __delitem__ = _reject
    clear = _reject
    pop = _reject
    popitem = _reject  # type: ignore[assignment]
    setdefault = _reject
    update = _reject
    __ior__ = _reject  # type: ignore[assignment]


class _FrozenList(list[Any]):
    def _reject(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen JSON list does not support mutation")

    __setitem__ = _reject
    __delitem__ = _reject
    __iadd__ = _reject  # type: ignore[assignment]
    __imul__ = _reject  # type: ignore[assignment]
    append = _reject
    clear = _reject
    extend = _reject
    insert = _reject
    pop = _reject
    remove = _reject
    reverse = _reject
    sort = _reject


class ContentModel(BaseModel):
    """Base model that protects canonical digests from later mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def freeze_nested_containers(self) -> Self:
        for field_name in type(self).model_fields:
            object.__setattr__(
                self, field_name, _deep_freeze(getattr(self, field_name))
            )
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class ContentGoal(StrEnum):
    FAITHFUL_CLEAN = "faithful_clean"
    CHAPTERED_FULL = "chaptered_full"
    SELECTED_CLIPS = "selected_clips"


class ContentOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContentDecision(StrEnum):
    KEEP = "keep"
    REMOVE = "remove"


class ContentDecisionSource(StrEnum):
    PROPOSAL = "proposal"
    USER = "user"
    LOCK = "lock"


class ContentSignalType(StrEnum):
    SCENE = "scene"
    SILENCE = "silence"
    LOW_VISUAL_CHANGE = "low_visual_change"
    NEAR_BLACK = "near_black"
    REPEATED_FRAMES = "repeated_frames"
    TRANSCRIPT = "transcript"
    USER_RANGE = "user_range"


class ContentSelectionEligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    MANUAL_ONLY = "manual_only"


class ContentUserRangeKind(StrEnum):
    KEEP = "keep"
    EXCLUDE = "exclude"
    LOCKED_KEEP = "locked_keep"
    LOCKED_EXCLUDE = "locked_exclude"
    CHAPTER = "chapter"


class ContentProviderStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class ContentActionKind(StrEnum):
    RETAIN = "retain"
    REMOVE = "remove"
    CHAPTER = "chapter"
    EXPORT_CLIP = "export_clip"
    CONCATENATE = "concatenate"
    APPLY_AUDIO_FADE = "apply_audio_fade"
    EXPORT_SUBTITLES = "export_subtitles"
    VERIFY = "verify"


class ContentTransition(StrEnum):
    HARD_JOIN = "hard_join"
    AUDIO_FADE = "audio_fade"


class ContentMappingState(StrEnum):
    UNCHANGED = "unchanged"
    TRANSFORMED = "transformed"


class ContentArtifactRole(StrEnum):
    MEDIA = "media"
    DOCUMENT = "document"
    CLIP = "clip"
    SUBTITLE = "subtitle"


class ContentVerificationStatus(StrEnum):
    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ContentExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ContentTimeRange(ContentModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_positive_duration(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("content range must have positive duration")
        return self

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


class ContentSignal(ContentModel):
    signal_type: ContentSignalType
    provider_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    provider_version: str = Field(min_length=1)
    measurements: dict[str, JsonValue] = Field(default_factory=dict)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()


class ContentProviderExecution(ContentModel):
    provider_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    provider_version: str = Field(min_length=1)
    status: ContentProviderStatus
    elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    warning: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_warning(self) -> Self:
        if self.status is ContentProviderStatus.FAILED and self.warning is None:
            raise ValueError("failed content provider requires a warning")
        return self


class ContentUserRange(ContentModel):
    id: str = Field(pattern=r"^range_[0-9a-f]{64}$")
    kind: ContentUserRangeKind
    source_range: ContentTimeRange
    label: str | None = Field(default=None, min_length=1)


class ContentConfig(ContentModel):
    goal: ContentGoal = ContentGoal.FAITHFUL_CLEAN
    planner_version: str = Field(default="1", min_length=1)
    provider_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    minimum_corrobating_signals: int = Field(default=2, ge=2, le=7)
    minimum_candidate_duration_seconds: float = Field(
        default=2.0, gt=0, allow_inf_nan=False
    )
    context_guard_seconds: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    merge_gap_seconds: float = Field(default=0.25, ge=0, allow_inf_nan=False)
    minimum_chapter_duration_seconds: float = Field(
        default=15.0, gt=0, allow_inf_nan=False
    )
    maximum_chapter_duration_seconds: float = Field(
        default=1800.0, gt=0, allow_inf_nan=False
    )
    target_duration_seconds: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )
    maximum_transcript_cues: int = Field(default=20_000, ge=1, le=100_000)
    maximum_chapters: int = Field(default=500, ge=1, le=2_000)
    maximum_storyboard_items: int = Field(default=2_000, ge=1, le=10_000)
    maximum_previews: int = Field(default=100, ge=1, le=1_000)
    maximum_preview_seconds: float = Field(default=12.0, gt=0, le=30.0)
    audio_fade_seconds: float = Field(default=0.0, ge=0, le=0.5)
    verification_duration_tolerance_seconds: float = Field(
        default=0.25, gt=0, le=2.0, allow_inf_nan=False
    )
    verification_av_sync_tolerance_seconds: float = Field(
        default=0.1, gt=0, le=1.0, allow_inf_nan=False
    )
    preserve_source_order: bool = True
    allow_reorder: bool = False
    export_subtitles: bool = False
    export_clips: bool = False
    generate_html_report: bool = True
    verification_policy: tuple[str, ...] = CONTENT_REQUIRED_VERIFICATION_CHECK_IDS

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        if (
            self.minimum_chapter_duration_seconds
            > self.maximum_chapter_duration_seconds
        ):
            raise ValueError("minimum chapter duration exceeds maximum")
        if not self.preserve_source_order and not self.allow_reorder:
            raise ValueError("source order can change only when reorder is allowed")
        if self.verification_policy != CONTENT_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError("verification policy must match the canonical policy")
        return self


class ContentSegment(ContentModel):
    id: str = Field(pattern=r"^segment_[0-9a-f]{64}$")
    source_range: ContentTimeRange
    source_order_index: int = Field(ge=0)
    signals: tuple[ContentSignal, ...] = ()
    transcript_cue_ids: tuple[str, ...] = ()
    selection_eligibility: ContentSelectionEligibility
    reason: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    private_evidence_paths: tuple[str, ...] = ()
    user_range_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_segment(self) -> Self:
        signal_types = tuple(signal.signal_type for signal in self.signals)
        if len(signal_types) != len(set(signal_types)):
            raise ValueError("duplicate content segment signal type")
        if signal_types != tuple(sorted(signal_types, key=lambda value: value.value)):
            raise ValueError("content segment signals must use stable order")
        _validate_unique(self.transcript_cue_ids, "transcript cue ID")
        _validate_unique(self.user_range_ids, "user range ID")
        _validate_path_collection(self.private_evidence_paths, "private evidence")
        return self


class ContentMap(ContentModel):
    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    transcript_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    effective_config: ContentConfig
    provider_executions: tuple[ContentProviderExecution, ...] = ()
    segments: tuple[ContentSegment, ...] = ()
    user_ranges: tuple[ContentUserRange, ...] = ()
    warnings: tuple[str, ...] = ()
    map_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_map(self) -> Self:
        provider_ids = tuple(item.provider_id for item in self.provider_executions)
        _validate_unique(provider_ids, "content provider ID")
        if provider_ids != tuple(sorted(provider_ids)):
            raise ValueError("content providers must use stable order")
        _validate_unique(tuple(item.id for item in self.segments), "segment ID")
        expected_indices = tuple(range(len(self.segments)))
        indices = tuple(item.source_order_index for item in self.segments)
        if indices != expected_indices:
            raise ValueError("content segments must use contiguous source order")
        if tuple(item.source_range.start_seconds for item in self.segments) != tuple(
            sorted(item.source_range.start_seconds for item in self.segments)
        ):
            raise ValueError("content segments must use source-time order")
        for segment in self.segments:
            expected_id = make_segment_id(
                self.input_hash,
                segment.source_range,
                tuple(signal.signal_type for signal in segment.signals),
            )
            if segment.id != expected_id:
                raise ValueError("segment ID does not match its observable inputs")
            _require_in_duration(segment.source_range, self.duration_seconds, "segment")
        _validate_unique(tuple(item.id for item in self.user_ranges), "user range ID")
        for item in self.user_ranges:
            _require_in_duration(item.source_range, self.duration_seconds, "user range")
        expected_digest = make_content_map_digest(
            self.model_dump(mode="json", exclude={"map_digest"})
        )
        if self.map_digest != expected_digest:
            raise ValueError("map_digest does not match the effective ContentMap")
        return self


class ContentChapter(ContentModel):
    id: str = Field(pattern=r"^chapter_[0-9a-f]{64}$")
    source_range: ContentTimeRange
    output_range: ContentTimeRange | None = None
    title: str = Field(min_length=1, max_length=300)
    title_source: Literal["neutral", "user", "transcript"]
    order_index: int = Field(ge=0)


class StoryboardItem(ContentModel):
    id: str = Field(pattern=r"^story_[0-9a-f]{64}$")
    source_range: ContentTimeRange
    source_order_index: int = Field(ge=0)
    output_order_index: int | None = Field(default=None, ge=0)
    decision: ContentDecision
    decision_source: ContentDecisionSource
    reason: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1, max_length=300)
    segment_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_decision_order(self) -> Self:
        if self.decision is ContentDecision.KEEP and self.output_order_index is None:
            raise ValueError("kept storyboard item requires an output order")
        if (
            self.decision is ContentDecision.REMOVE
            and self.output_order_index is not None
        ):
            raise ValueError("removed storyboard item cannot have an output order")
        _validate_unique(self.segment_ids, "storyboard segment ID")
        return self


class Storyboard(ContentModel):
    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    transcript_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    goal: ContentGoal
    items: tuple[StoryboardItem, ...]
    chapters: tuple[ContentChapter, ...] = ()
    locked_ranges: tuple[ContentUserRange, ...] = ()
    estimated_output_duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    estimated_source_coverage: float = Field(ge=0, le=1, allow_inf_nan=False)
    reorder_acknowledged: bool = False
    storyboard_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_storyboard(self) -> Self:
        _validate_unique(tuple(item.id for item in self.items), "storyboard item ID")
        source_indices = tuple(item.source_order_index for item in self.items)
        if source_indices != tuple(range(len(self.items))):
            raise ValueError("storyboard items must use contiguous source order")
        for item in self.items:
            expected_id = make_storyboard_item_id(
                self.input_hash, item.source_range, item.source_order_index
            )
            if item.id != expected_id:
                raise ValueError("storyboard item ID does not match source inputs")
        kept = sorted(
            (item for item in self.items if item.decision is ContentDecision.KEEP),
            key=lambda item: (
                item.output_order_index if item.output_order_index is not None else -1
            ),
        )
        if tuple(item.output_order_index for item in kept) != tuple(range(len(kept))):
            raise ValueError("kept storyboard items must use contiguous output order")
        kept_source_order = tuple(item.source_order_index for item in kept)
        if not self.reorder_acknowledged and kept_source_order != tuple(
            sorted(kept_source_order)
        ):
            raise ValueError("storyboard reorder acknowledgement is required")
        expected_duration = sum(item.source_range.duration_seconds for item in kept)
        if not math.isclose(
            self.estimated_output_duration_seconds,
            expected_duration,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("estimated output duration does not match kept ranges")
        _validate_unique(tuple(item.id for item in self.chapters), "chapter ID")
        if tuple(item.order_index for item in self.chapters) != tuple(
            range(len(self.chapters))
        ):
            raise ValueError("chapters must use contiguous order")
        for chapter in self.chapters:
            expected_id = make_chapter_id(
                self.input_hash, chapter.source_range, chapter.order_index
            )
            if chapter.id != expected_id:
                raise ValueError("chapter ID does not match source inputs")
        _validate_unique(
            tuple(item.id for item in self.locked_ranges), "locked range ID"
        )
        if any(
            item.kind
            not in {
                ContentUserRangeKind.LOCKED_KEEP,
                ContentUserRangeKind.LOCKED_EXCLUDE,
            }
            for item in self.locked_ranges
        ):
            raise ValueError("storyboard locked ranges must use a locked kind")
        expected_digest = make_storyboard_digest(
            self.model_dump(mode="json", exclude={"storyboard_digest"})
        )
        if self.storyboard_digest != expected_digest:
            raise ValueError("storyboard_digest does not match the Storyboard")
        return self


class ContentAction(ContentModel):
    id: str = Field(pattern=r"^action_[0-9a-f]{64}$")
    version: str = Field(min_length=1)
    kind: ContentActionKind
    description: str = Field(min_length=1)
    source_ranges: tuple[ContentTimeRange, ...] = ()
    expected_output_ranges: tuple[ContentTimeRange, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    changes_content: bool
    requires_confirmation: bool
    depends_on: tuple[str, ...] = ()
    evidence_segment_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.changes_content and not self.requires_confirmation:
            raise ValueError("content-changing action requires confirmation")
        if self.id in self.depends_on:
            raise ValueError("content action cannot depend on itself")
        _validate_unique(self.depends_on, "content action dependency")
        _validate_unique(self.evidence_segment_ids, "action evidence segment ID")
        return self


class ContentPlan(ContentModel):
    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    transcript_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    goal: ContentGoal
    effective_config: ContentConfig
    storyboard: Storyboard
    actions: tuple[ContentAction, ...]
    locked_ranges: tuple[ContentUserRange, ...] = ()
    private_artifacts: tuple[str, ...] = ()
    public_artifacts: tuple[str, ...] = ()
    preview_identities: dict[str, str] = Field(default_factory=dict)
    verification_policy: tuple[str, ...] = CONTENT_REQUIRED_VERIFICATION_CHECK_IDS
    plan_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.effective_config.goal is not self.goal:
            raise ValueError("content config goal does not match plan")
        if (
            self.storyboard.input_hash != self.input_hash
            or self.storyboard.transcript_hash != self.transcript_hash
            or self.storyboard.goal is not self.goal
        ):
            raise ValueError("storyboard identity does not match content plan")
        action_ids = tuple(action.id for action in self.actions)
        _validate_unique(action_ids, "content action ID")
        for action in self.actions:
            if any(dependency not in action_ids for dependency in action.depends_on):
                raise ValueError("content action dependency is not in the plan")
        required_previews = {
            action.id
            for action in self.actions
            if action.changes_content and action.requires_confirmation
        }
        if set(self.preview_identities) != required_previews:
            raise ValueError(
                "preview identities must bind every content-changing action"
            )
        if any(not value for value in self.preview_identities.values()):
            raise ValueError("preview identity must not be empty")
        if self.verification_policy != CONTENT_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError("verification policy must match the canonical policy")
        _validate_path_collection(self.private_artifacts, "private artifact")
        _validate_path_collection(self.public_artifacts, "public artifact")
        if any(
            not path.startswith("content-review-private/")
            for path in self.private_artifacts
        ):
            raise ValueError("private content artifact must remain in private tree")
        if any(
            not path.startswith("content-output/") for path in self.public_artifacts
        ):
            raise ValueError("public content artifact must remain in output tree")
        locked_ids = tuple(item.id for item in self.locked_ranges)
        _validate_unique(locked_ids, "locked range ID")
        if self.locked_ranges != self.storyboard.locked_ranges:
            raise ValueError("plan locks must match storyboard locks")
        expected_digest = make_content_plan_digest(
            self.model_dump(mode="json", exclude={"plan_digest"})
        )
        if self.plan_digest != expected_digest:
            raise ValueError("plan_digest does not match the effective ContentPlan")
        return self

    def validate_confirmation(self, confirmation: ContentConfirmation) -> None:
        if confirmation.input_hash != self.input_hash:
            raise ValueError("confirmation input_hash does not match ContentPlan")
        if confirmation.transcript_hash != self.transcript_hash:
            raise ValueError("confirmation transcript_hash does not match ContentPlan")
        if confirmation.plan_digest != self.plan_digest:
            raise ValueError("confirmation plan_digest does not match ContentPlan")
        if confirmation.storyboard_digest != self.storyboard.storyboard_digest:
            raise ValueError(
                "confirmation storyboard_digest does not match ContentPlan"
            )
        required_actions = {
            action.id
            for action in self.actions
            if action.changes_content and action.requires_confirmation
        }
        if set(confirmation.accepted_action_ids) != required_actions:
            raise ValueError("confirmation must accept the exact action set")
        if dict(confirmation.preview_identities) != dict(self.preview_identities):
            raise ValueError("confirmation preview identities do not match ContentPlan")
        if set(confirmation.locked_range_ids) != {
            item.id for item in self.locked_ranges
        }:
            raise ValueError("confirmation locks do not match ContentPlan")
        if confirmation.verification_policy != self.verification_policy:
            raise ValueError(
                "confirmation verification policy does not match ContentPlan"
            )
        if (
            confirmation.reorder_acknowledged
            is not self.storyboard.reorder_acknowledged
        ):
            raise ValueError("confirmation reorder acknowledgement does not match")


class ContentConfirmation(ContentModel):
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    transcript_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    storyboard_digest: str = Field(pattern=_SHA256_PATTERN)
    accepted_action_ids: tuple[str, ...] = ()
    preview_identities: dict[str, str] = Field(default_factory=dict)
    locked_range_ids: tuple[str, ...] = ()
    verification_policy: tuple[str, ...] = CONTENT_REQUIRED_VERIFICATION_CHECK_IDS
    reorder_acknowledged: bool = False

    @model_validator(mode="after")
    def validate_confirmation_ids(self) -> Self:
        _validate_unique(self.accepted_action_ids, "accepted action ID")
        _validate_unique(self.locked_range_ids, "confirmed locked range ID")
        if self.verification_policy != CONTENT_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError("verification policy must match the canonical policy")
        return self


class ContentSourceMapping(ContentModel):
    id: str = Field(pattern=r"^mapping_[0-9a-f]{64}$")
    output_range: ContentTimeRange
    source_range: ContentTimeRange
    source_order_index: int = Field(ge=0)
    output_order_index: int = Field(ge=0)
    transition: ContentTransition = ContentTransition.HARD_JOIN
    state: ContentMappingState = ContentMappingState.UNCHANGED
    storyboard_item_id: str = Field(pattern=r"^story_[0-9a-f]{64}$")
    action_id: str | None = Field(default=None, pattern=r"^action_[0-9a-f]{64}$")


class ContentActionExecution(ContentModel):
    action_id: str = Field(pattern=r"^action_[0-9a-f]{64}$")
    status: ContentExecutionStatus
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.status is not ContentExecutionStatus.SUCCEEDED and self.reason is None:
            raise ValueError("failed or skipped content action requires a reason")
        return self


class ContentArtifact(ContentModel):
    role: ContentArtifactRole
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        _validate_relative_posix_path(self.relative_path, "artifact path")
        if not self.relative_path.startswith("content-output/"):
            raise ValueError("public content artifact must remain in output tree")
        return self


class ContentChangeLog(ContentModel):
    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    source_modified: Literal[False] = False
    actions: tuple[ContentAction, ...] = ()
    executions: tuple[ContentActionExecution, ...] = ()
    artifacts: tuple[ContentArtifact, ...] = ()

    @model_validator(mode="after")
    def validate_change_log(self) -> Self:
        action_ids = tuple(action.id for action in self.actions)
        _validate_unique(action_ids, "change-log action ID")
        execution_ids = tuple(item.action_id for item in self.executions)
        _validate_unique(execution_ids, "action execution ID")
        if any(item not in action_ids for item in execution_ids):
            raise ValueError("action execution is not present in change log")
        _validate_unique(
            tuple(item.relative_path for item in self.artifacts), "artifact path"
        )
        return self


class ContentVerificationCheck(ContentModel):
    check_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    version: str = Field(min_length=1)
    required: bool = True
    status: ContentVerificationStatus
    message: str = Field(min_length=1)
    measured: dict[str, JsonValue] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()


class ContentVerificationReport(ContentModel):
    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    checks: tuple[ContentVerificationCheck, ...]
    missing_source_ranges: tuple[ContentTimeRange, ...] = ()
    required_check_ids: tuple[str, ...] = CONTENT_REQUIRED_VERIFICATION_CHECK_IDS
    outcome: ContentOutcome

    @model_validator(mode="after")
    def derive_outcome(self) -> Self:
        if self.required_check_ids != CONTENT_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError("required verification checks must use canonical policy")
        check_ids = tuple(item.check_id for item in self.checks)
        _validate_unique(check_ids, "verification check ID")
        required = tuple(item.check_id for item in self.checks if item.required)
        if required != self.required_check_ids:
            raise ValueError("required verification checks must use canonical order")
        required_statuses = {item.status for item in self.checks if item.required}
        if ContentVerificationStatus.FAILED in required_statuses:
            outcome = ContentOutcome.FAILED
        elif ContentVerificationStatus.NEEDS_REVIEW in required_statuses:
            outcome = ContentOutcome.NEEDS_REVIEW
        elif self.missing_source_ranges:
            outcome = ContentOutcome.PARTIAL
        else:
            outcome = ContentOutcome.COMPLETED
        object.__setattr__(self, "outcome", outcome)
        return self


class ContentTechnicalReport(ContentModel):
    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    transcript_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    goal: ContentGoal
    outcome: ContentOutcome
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    artifacts: tuple[ContentArtifact, ...]
    chapters: tuple[ContentChapter, ...]
    source_mappings: tuple[ContentSourceMapping, ...]
    change_log: ContentChangeLog | None
    verification: ContentVerificationReport
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    runtime: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.verification.plan_digest != self.plan_digest:
            raise ValueError("verification plan digest does not match technical report")
        if (
            self.change_log is not None
            and self.change_log.plan_digest != self.plan_digest
        ):
            raise ValueError("change-log plan digest does not match technical report")
        if self.outcome is not self.verification.outcome:
            raise ValueError("technical report outcome does not match verification")
        _validate_unique(
            tuple(item.relative_path for item in self.artifacts), "artifact path"
        )
        _validate_unique(tuple(item.id for item in self.source_mappings), "mapping ID")
        _validate_public_json_value(self.model_dump(mode="json"))
        return self


def make_segment_id(
    input_hash: str,
    source_range: ContentTimeRange,
    signal_types: tuple[ContentSignalType, ...],
) -> str:
    _require_sha256(input_hash, "input_hash")
    return "segment_" + _canonical_digest(
        {
            "input_hash": input_hash,
            "source_range": source_range.model_dump(mode="json"),
            "signal_types": sorted(item.value for item in signal_types),
        }
    )


def make_user_range_id(
    input_hash: str, kind: ContentUserRangeKind, source_range: ContentTimeRange
) -> str:
    _require_sha256(input_hash, "input_hash")
    return "range_" + _canonical_digest(
        {
            "input_hash": input_hash,
            "kind": kind.value,
            "source_range": source_range.model_dump(mode="json"),
        }
    )


def make_storyboard_item_id(
    input_hash: str, source_range: ContentTimeRange, source_order_index: int
) -> str:
    _require_sha256(input_hash, "input_hash")
    if source_order_index < 0:
        raise ValueError("source_order_index must be non-negative")
    return "story_" + _canonical_digest(
        {
            "input_hash": input_hash,
            "source_order_index": source_order_index,
            "source_range": source_range.model_dump(mode="json"),
        }
    )


def make_chapter_id(
    input_hash: str, source_range: ContentTimeRange, order_index: int
) -> str:
    _require_sha256(input_hash, "input_hash")
    if order_index < 0:
        raise ValueError("order_index must be non-negative")
    return "chapter_" + _canonical_digest(
        {
            "input_hash": input_hash,
            "order_index": order_index,
            "source_range": source_range.model_dump(mode="json"),
        }
    )


def make_action_id(
    input_hash: str,
    kind: ContentActionKind,
    source_ranges: tuple[ContentTimeRange, ...],
    order_index: int,
) -> str:
    _require_sha256(input_hash, "input_hash")
    if order_index < 0:
        raise ValueError("order_index must be non-negative")
    return "action_" + _canonical_digest(
        {
            "input_hash": input_hash,
            "kind": kind.value,
            "order_index": order_index,
            "source_ranges": [item.model_dump(mode="json") for item in source_ranges],
        }
    )


def make_mapping_id(
    input_hash: str,
    output_range: ContentTimeRange,
    source_range: ContentTimeRange,
    output_order_index: int,
) -> str:
    _require_sha256(input_hash, "input_hash")
    if output_order_index < 0:
        raise ValueError("output_order_index must be non-negative")
    return "mapping_" + _canonical_digest(
        {
            "input_hash": input_hash,
            "output_order_index": output_order_index,
            "output_range": output_range.model_dump(mode="json"),
            "source_range": source_range.model_dump(mode="json"),
        }
    )


def make_content_map_digest(payload: Mapping[str, JsonValue]) -> str:
    values = dict(payload)
    values.pop("map_digest", None)
    values.setdefault("schema_version", CONTENT_SCHEMA_VERSION)
    values.setdefault("transcript_hash", None)
    return _canonical_digest(values)


def make_storyboard_digest(payload: Mapping[str, JsonValue]) -> str:
    values = dict(payload)
    values.pop("storyboard_digest", None)
    values.setdefault("schema_version", CONTENT_SCHEMA_VERSION)
    values.setdefault("transcript_hash", None)
    return _canonical_digest(values)


def make_content_plan_digest(payload: Mapping[str, JsonValue]) -> str:
    values = dict(payload)
    values.pop("plan_digest", None)
    values.setdefault("schema_version", CONTENT_SCHEMA_VERSION)
    values.setdefault("transcript_hash", None)
    return _canonical_digest(values)


def _validate_relative_posix_path(value: str, field_name: str) -> None:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path == PurePosixPath(".")
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or "\\" in value
        or value != path.as_posix()
    ):
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")


def _validate_path_collection(values: tuple[str, ...], field_name: str) -> None:
    _validate_unique(values, field_name)
    for value in values:
        _validate_relative_posix_path(value, field_name)


def _validate_unique(values: tuple[Any, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {field_name}")


def _require_in_duration(
    time_range: ContentTimeRange, duration_seconds: float, field_name: str
) -> None:
    if time_range.end_seconds > duration_seconds:
        raise ValueError(f"{field_name} exceeds source duration")


def _require_sha256(value: str, field_name: str) -> None:
    if not re.fullmatch(_SHA256_PATTERN, value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _validate_public_json_value(value: object) -> None:
    if isinstance(value, str):
        if _contains_absolute_path(value):
            raise ValueError("public content JSON must not contain an absolute path")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_public_json_value(key)
            _validate_public_json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_public_json_value(item)


def _contains_absolute_path(value: str) -> bool:
    return bool(
        _WINDOWS_ABSOLUTE_PATH_PATTERN.search(value)
        or _UNC_PATH_PATTERN.search(value)
        or _UNIX_ABSOLUTE_PATH_PATTERN.search(value)
    )


def _canonical_digest(payload: object) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(content.encode("utf-8")).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, dict):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value
