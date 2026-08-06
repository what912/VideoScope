"""Public models and local server policy for the optional Web API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.ai.models import DevicePreference
from videoscope.content.models import (
    ContentGoal,
    ContentUserRangeKind,
)
from videoscope.intelligence.models import AIReviewDecision
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
)
from videoscope.privacy.models import PrivacyReviewDecision
from videoscope.rescue.models import RescueStrategy, RescueSymptom
from videoscope.resolve import PublishProfileId


def default_job_root() -> Path:
    """Return the platform application-data directory for local Web jobs."""
    return Path(user_data_path("VideoScope", "VideoScope")) / "web-jobs"


class WebModel(BaseModel):
    """Strict base for Web API models."""

    model_config = ConfigDict(extra="forbid")


class JobStatus(StrEnum):
    """Observable lifecycle states for one local analysis job."""

    QUEUED = "queued"
    PROBING = "probing"
    SAMPLING = "sampling"
    DETECTING = "detecting"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Whether no later analysis state is expected."""
        return self in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class PublishJobStatus(StrEnum):
    """Observable lifecycle states for one local Publish Ready job."""

    QUEUED = "queued"
    INSPECTING = "inspecting"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Whether the Publish Ready job has no later lifecycle state."""
        return self in {
            PublishJobStatus.COMPLETED,
            PublishJobStatus.NEEDS_REVIEW,
            PublishJobStatus.FAILED,
            PublishJobStatus.CANCELLED,
        }


class PrivacyJobStatus(StrEnum):
    """Observable lifecycle states for one local Safe Sharing job."""

    QUEUED = "queued"
    INSPECTING = "inspecting"
    SCANNING = "scanning"
    AWAITING_REVIEW = "awaiting_review"
    PLANNING = "planning"
    PREVIEWING = "previewing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            PrivacyJobStatus.COMPLETED,
            PrivacyJobStatus.NEEDS_REVIEW,
            PrivacyJobStatus.PARTIAL,
            PrivacyJobStatus.FAILED,
            PrivacyJobStatus.CANCELLED,
        }


class RescueJobStatus(StrEnum):
    """Observable lifecycle states for one local Video Rescue job."""

    QUEUED = "queued"
    SCANNING = "scanning"
    PLANNING = "planning"
    PREVIEWING = "previewing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            RescueJobStatus.COMPLETED,
            RescueJobStatus.NEEDS_REVIEW,
            RescueJobStatus.PARTIAL,
            RescueJobStatus.FAILED,
            RescueJobStatus.CANCELLED,
        }


class ContentJobStatus(StrEnum):
    """Observable states for one useful-content review lifecycle."""

    QUEUED = "queued"
    PROBING = "probing"
    MAPPING = "mapping"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    PREVIEWING = "previewing"
    READY_TO_CONFIRM = "ready_to_confirm"
    RENDERING = "rendering"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            ContentJobStatus.COMPLETED,
            ContentJobStatus.PARTIAL,
            ContentJobStatus.NEEDS_REVIEW,
            ContentJobStatus.FAILED,
            ContentJobStatus.CANCELLED,
        }


class ContentRangeInput(WebModel):
    """One source-time edit whose deterministic ID is assigned by the server."""

    kind: ContentUserRangeKind
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    label: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_interval(self) -> ContentRangeInput:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class ContentStoryboardRevisionRequest(WebModel):
    """Optimistic-concurrency edit to exact source ranges and chapter labels."""

    expected_revision: int = Field(ge=0)
    ranges: tuple[ContentRangeInput, ...] = ()
    selected_range_order: tuple[str, ...] = ()
    reorder_acknowledged: bool = False
    chapter_titles: dict[str, str] = Field(default_factory=dict)


class ContentConfirmationRequest(WebModel):
    """Confirmation bound to one exact plan and revision."""

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=0)
    accepted_action_ids: tuple[str, ...] = ()


class AdvancedAIPrepareRequest(WebModel):
    """Explicit local provider settings for one useful-content AI review."""

    semantic_model_id: str = Field(min_length=1, max_length=300)
    asr_model_id: str = Field(default="small", min_length=1, max_length=300)
    asr_language: str | None = Field(default=None, min_length=1, max_length=32)
    ollama_endpoint: str = Field(
        default="http://127.0.0.1:11434", min_length=1, max_length=500
    )
    locale: str = Field(default="en", pattern=r"^(en|zh-CN)$")
    device: DevicePreference = DevicePreference.AUTO
    allow_model_download: bool = False
    maximum_suggestions: int = Field(default=24, ge=1, le=200)


class AdvancedAIReviewRequest(WebModel):
    """Exact human decisions over the current private suggestion batch."""

    decisions: tuple[AIReviewDecision, ...]


class AdvancedAIApplyRequest(WebModel):
    """Optimistic-concurrency application of accepted AI ranges to C."""

    expected_revision: int = Field(ge=0)


class PublishConfirmation(WebModel):
    """Confirmation bound to one exact canonical PublishPlan digest."""

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrivacyConfirmation(WebModel):
    """Confirmation bound to one exact canonical privacy-plan digest."""

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RescueConfirmationRequest(WebModel):
    """One digest-bound, single-use confirmation for a Rescue plan."""

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    publish_faithful: bool
    publish_improved: bool
    accepted_action_ids: tuple[str, ...] = ()
    accepted_trim_damage_ids: tuple[str, ...] = ()


class PrivacyReviewRequest(WebModel):
    """Human decisions for the current private risk map."""

    reviews: tuple[PrivacyReviewDecision, ...]
    manual_visual_regions: tuple[ManualVisualRegionInput, ...] = ()
    manual_audio_intervals: tuple[ManualAudioIntervalInput, ...] = ()


class WebServerConfig(WebModel):
    """Resource, retention, and upload limits for one local server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_root: Path = Field(default_factory=default_job_root)
    max_upload_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    upload_chunk_bytes: int = Field(default=1024 * 1024, ge=4096)
    cpu_concurrency: int = Field(default=2, ge=1, le=64)
    heavy_ai_concurrency: int = Field(default=1, ge=1, le=16)
    job_ttl_seconds: float = Field(default=24 * 60 * 60, gt=0)
    cleanup_interval_seconds: float = Field(default=5 * 60, gt=0)
    maximum_prompt_characters: int = Field(default=20_000, ge=1)
    maximum_config_bytes: int = Field(default=64 * 1024, ge=2)
    trusted_hosts: tuple[str, ...] = (
        "127.0.0.1",
        "localhost",
        "[::1]",
        "testserver",
    )
    allow_non_loopback_origin: bool = False


class JobEvent(WebModel):
    """One ordered SSE-compatible job lifecycle event."""

    sequence: int = Field(ge=1)
    status: JobStatus
    message: str
    created_at: datetime


class PublishJobEvent(WebModel):
    """One ordered SSE-compatible Publish Ready lifecycle event."""

    sequence: int = Field(ge=1)
    status: PublishJobStatus
    message: str
    progress_percent: int = Field(ge=0, le=100)
    created_at: datetime


class PrivacyJobEvent(WebModel):
    """One ordered SSE-compatible Safe Sharing lifecycle event."""

    sequence: int = Field(ge=1)
    status: PrivacyJobStatus
    message: str
    progress_percent: int = Field(ge=0, le=100)
    created_at: datetime


class RescueJobEvent(WebModel):
    """One ordered SSE-compatible Video Rescue lifecycle event."""

    sequence: int = Field(ge=1)
    status: RescueJobStatus
    message: str
    progress_percent: int = Field(ge=0, le=100)
    created_at: datetime


class ContentJobEvent(WebModel):
    """One ordered useful-content lifecycle event."""

    sequence: int = Field(ge=1)
    status: ContentJobStatus
    message: str
    progress_percent: int = Field(ge=0, le=100)
    revision: int = Field(ge=0)
    created_at: datetime


class JobResponse(WebModel):
    """Path-free public job state."""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: JobStatus
    message: str
    created_at: datetime
    updated_at: datetime
    upload_size_bytes: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    current_detector: str | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    links: dict[str, str]


class PublishJobResponse(WebModel):
    """Path-free public state for one local Publish Ready job."""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: PublishJobStatus
    message: str
    created_at: datetime
    updated_at: datetime
    upload_size_bytes: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    profile_id: PublishProfileId
    warnings: tuple[str, ...] = ()
    error: str | None = None
    links: dict[str, str]


class PrivacyJobResponse(WebModel):
    """Path-free public state for one local Safe Sharing job."""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: PrivacyJobStatus
    message: str
    created_at: datetime
    updated_at: datetime
    upload_size_bytes: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warnings: tuple[str, ...] = ()
    error: str | None = None
    links: dict[str, str]


class RescueJobResponse(WebModel):
    """Path-free public state for one local Video Rescue job."""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: RescueJobStatus
    message: str
    created_at: datetime
    updated_at: datetime
    upload_size_bytes: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    strategy: RescueStrategy
    symptoms: tuple[RescueSymptom, ...] = ()
    locked_ranges: tuple[tuple[float, float], ...] = ()
    balanced_strength_limit: float = Field(default=1.0, gt=0, le=1)
    private_artifacts: tuple[str, ...] = ()
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warnings: tuple[str, ...] = ()
    error: str | None = None
    links: dict[str, str]


class ContentJobResponse(WebModel):
    """Path-free state for a local useful-content job."""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: ContentJobStatus
    message: str
    created_at: datetime
    updated_at: datetime
    upload_size_bytes: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    goal: ContentGoal
    revision: int = Field(ge=0)
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warnings: tuple[str, ...] = ()
    error: str | None = None
    links: dict[str, str]


class HealthResponse(WebModel):
    """Local service health without exposing filesystem details."""

    status: str = "ok"
    service: str = "VideoScope local API"
    local_only_default: bool = True
    active_jobs: int = Field(ge=0)


class DetectorResponse(WebModel):
    """Detector manifest exposed by the API."""

    id: str
    display_name: str
    version: str
    description: str
    default_enabled: bool
    requires_prompt: bool
    requires_gpu: bool
    requires_network: bool
    optional_packages: tuple[str, ...]
    estimated_cost: str
    category: str
    available: bool
    unavailable_reason: str | None = None
