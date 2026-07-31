"""Optional local Web API models without eager FastAPI imports."""

from videoscope.web.jobs import JobManager
from videoscope.web.models import (
    DetectorResponse,
    HealthResponse,
    JobEvent,
    JobResponse,
    JobStatus,
    WebServerConfig,
    default_job_root,
)

__all__ = [
    "DetectorResponse",
    "HealthResponse",
    "JobEvent",
    "JobManager",
    "JobResponse",
    "JobStatus",
    "WebServerConfig",
    "default_job_root",
]
