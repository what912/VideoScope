"""Public models and local server policy for the optional Web API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field


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
