"""FastAPI application factory for the local VideoScope API."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import ipaddress
import json
import math
import mimetypes
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import token_hex
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from videoscope import __version__
from videoscope.analysis import AnalysisConfig
from videoscope.content import (
    ContentConfig,
    ContentError,
    ContentGoal,
    ContentJoinPreview,
    ContentMap,
    ContentPlan,
)
from videoscope.content.pipeline import ContentPipelineConfig
from videoscope.detectors import create_optional_detector_registry
from videoscope.intelligence import (
    AdvancedAICancelledError,
    AdvancedAIConfig,
    AdvancedAIContentPipeline,
    AdvancedAIDependencies,
    AdvancedAIPreparation,
    AIReviewManifest,
    AISuggestionBatch,
    build_review_manifest,
    reviewed_content_ranges,
    write_intelligence_json,
)
from videoscope.intelligence.providers import (
    OpenAICompatibleContentIntelligenceProvider,
)
from videoscope.privacy.models import PrivacyPlan, PrivacyRiskMap
from videoscope.privacy.profiles import (
    ShareAudienceProfile,
    get_share_audience_profile,
    list_share_audience_profiles,
)
from videoscope.rescue.models import (
    MediaDamageMap,
    RescueConfirmation,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
)
from videoscope.resolve import PublishPlan, PublishProfileId
from videoscope.resolve.profiles import PublishProfile, list_publish_profiles
from videoscope.web.connector import (
    ConnectorPairingRequest,
    ConnectorSession,
    ConnectorSessionStore,
    ConnectorStatus,
    ProviderCapability,
    ProviderCredentialVault,
    ProviderProfileInput,
    ProviderProfileSummary,
    ProviderProtocol,
)
from videoscope.web.content_jobs import (
    ContentArtifactUnavailableError,
    ContentConfirmationMismatchError,
    ContentJobManager,
    ContentJobStateError,
    ContentRevisionConflictError,
)
from videoscope.web.jobs import CpuJobLimiter, JobManager
from videoscope.web.models import (
    AdvancedAIApplyRequest,
    AdvancedAIPrepareRequest,
    AdvancedAIReviewRequest,
    ContentConfirmationRequest,
    ContentJobEvent,
    ContentJobResponse,
    ContentRangeInput,
    ContentStoryboardRevisionRequest,
    DetectorResponse,
    HealthResponse,
    JobEvent,
    JobResponse,
    PrivacyConfirmation,
    PrivacyJobEvent,
    PrivacyJobResponse,
    PrivacyReviewRequest,
    PublishConfirmation,
    PublishJobEvent,
    PublishJobResponse,
    RescueConfirmationRequest,
    RescueJobEvent,
    RescueJobResponse,
    WebServerConfig,
)
from videoscope.web.privacy_jobs import (
    PrivacyArtifactUnavailableError,
    PrivacyConfirmationMismatchError,
    PrivacyJobManager,
    PrivacyJobStateError,
)
from videoscope.web.publish_jobs import (
    PublishArtifactUnavailableError,
    PublishConfirmationMismatchError,
    PublishJobManager,
    PublishJobStateError,
)
from videoscope.web.rescue_jobs import (
    PinnedRescueArtifact,
    RescueArtifactUnavailableError,
    RescueConfirmationMismatchError,
    RescueJobManager,
    RescueJobStateError,
)


def _pinned_rescue_response(
    artifact: PinnedRescueArtifact, request: Request
) -> StreamingResponse:
    """Stream (including one byte range) from the descriptor already validated."""
    start, end, status_code = 0, artifact.size_bytes - 1, status.HTTP_200_OK
    range_header = request.headers.get("range")
    if range_header:
        unit, _, value = range_header.partition("=")
        if unit != "bytes" or "," in value:
            os.close(artifact.descriptor)
            return StreamingResponse(
                iter(()), status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
            )
        first, _, last = value.partition("-")
        try:
            start = int(first) if first else max(0, artifact.size_bytes - int(last))
            end = int(last) if last else artifact.size_bytes - 1
        except ValueError:
            os.close(artifact.descriptor)
            return StreamingResponse(
                iter(()), status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
            )
        if start < 0 or end < start or start >= artifact.size_bytes:
            os.close(artifact.descriptor)
            return StreamingResponse(
                iter(()), status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
            )
        end = min(end, artifact.size_bytes - 1)
        status_code = status.HTTP_206_PARTIAL_CONTENT
    length = end - start + 1

    async def stream() -> AsyncIterator[bytes]:
        remaining = length
        await run_in_threadpool(os.lseek, artifact.descriptor, start, os.SEEK_SET)
        while remaining:
            block = await run_in_threadpool(
                os.read, artifact.descriptor, min(64 * 1024, remaining)
            )
            if not block:
                break
            remaining -= len(block)
            yield block

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status_code == status.HTTP_206_PARTIAL_CONTENT:
        headers["Content-Range"] = f"bytes {start}-{end}/{artifact.size_bytes}"
    return StreamingResponse(
        stream(),
        status_code=status_code,
        media_type=mimetypes.guess_type(artifact.name)[0] or "application/octet-stream",
        headers=headers,
        background=BackgroundTask(os.close, artifact.descriptor),
    )


_COMMON_VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
    ".wmv",
}
_PACKAGE_IMPORT_NAMES = {
    "open-clip-torch": "open_clip",
    "paddlepaddle": "paddle",
}


def _detector_availability(
    optional_packages: tuple[str, ...],
) -> tuple[bool, str | None]:
    missing = tuple(
        package
        for package in optional_packages
        if importlib.util.find_spec(
            _PACKAGE_IMPORT_NAMES.get(package, package.replace("-", "_"))
        )
        is None
    )
    if not missing:
        return True, None
    return (
        False,
        "Install the optional package group providing: " + ", ".join(missing),
    )


def _upload_warnings(filename: str, content_type: str | None) -> tuple[str, ...]:
    warnings: list[str] = []
    if Path(filename).suffix.casefold() not in _COMMON_VIDEO_EXTENSIONS:
        warnings.append(
            "The filename extension is not recognized as common video; "
            "ffprobe will validate the uploaded content."
        )
    if content_type is None or not content_type.casefold().startswith("video/"):
        warnings.append(
            "The upload MIME type is not video/*; ffprobe will validate the "
            "uploaded content."
        )
    return tuple(warnings)


def _parse_rescue_locked_ranges(raw_ranges: str) -> tuple[tuple[float, float], ...]:
    """Parse a small, strict path-free JSON list before reserving a job."""

    if len(raw_ranges.encode("utf-8")) > 4096:
        raise HTTPException(status_code=422, detail="Invalid locked ranges.")
    try:
        payload: object = json.loads(raw_ranges)
        if not isinstance(payload, list) or len(payload) > 64:
            raise ValueError("locked ranges must be a bounded list")
        ranges: list[tuple[float, float]] = []
        for item in payload:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or any(isinstance(value, bool) for value in item)
                or any(not isinstance(value, (int, float)) for value in item)
            ):
                raise ValueError("locked ranges must contain numeric pairs")
            start, end = float(item[0]), float(item[1])
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end < start
            ):
                raise ValueError("locked ranges must be ordered finite seconds")
            ranges.append((start, end))
        return tuple(sorted(set(ranges)))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid locked ranges.") from exc


def _parse_analysis_config(
    raw_config: str | None,
    *,
    maximum_bytes: int,
) -> AnalysisConfig:
    if raw_config is None or not raw_config.strip():
        return AnalysisConfig()
    if len(raw_config.encode("utf-8")) > maximum_bytes:
        raise HTTPException(
            status_code=413,
            detail="Analysis configuration is too large.",
        )
    try:
        payload: object = json.loads(raw_config)
        if not isinstance(payload, dict):
            raise ValueError("configuration root is not an object")
        return AnalysisConfig.model_validate(cast(dict[str, Any], payload))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid analysis configuration.",
        ) from exc


def _event_payload(
    event: (
        JobEvent | PublishJobEvent | PrivacyJobEvent | RescueJobEvent | ContentJobEvent
    ),
) -> str:
    data = event.model_dump_json()
    return f"id: {event.sequence}\nevent: status\ndata: {data}\n\n"


def _is_loopback_origin(origin: str) -> bool:
    """Accept only HTTP(S) browser origins whose host is loopback."""
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_loopback_client(request: Request) -> bool:
    """Keep private Rescue previews on this machine's loopback interface."""
    host = request.client.host if request.client is not None else ""
    if host == "testclient":
        return True
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(
    config: WebServerConfig | None = None,
    *,
    manager: JobManager | None = None,
    publish_manager: PublishJobManager | None = None,
    privacy_manager: PrivacyJobManager | None = None,
    rescue_manager: RescueJobManager | None = None,
    content_manager: ContentJobManager | None = None,
) -> FastAPI:
    """Create an app with no CORS middleware or external service dependency."""
    effective_config = config or WebServerConfig()
    connector_sessions = ConnectorSessionStore(
        effective_config.connector_pairing_code or token_hex(12),
        ttl_seconds=effective_config.connector_session_ttl_seconds,
    )
    provider_vault = ProviderCredentialVault()
    cpu_limiter = CpuJobLimiter(effective_config.cpu_concurrency)
    job_manager: JobManager = (
        manager
        if manager is not None
        else JobManager(effective_config, cpu_limiter=cpu_limiter)
    )
    publish_job_manager: PublishJobManager = (
        publish_manager
        if publish_manager is not None
        else PublishJobManager(effective_config, cpu_limiter=cpu_limiter)
    )
    privacy_job_manager: PrivacyJobManager = (
        privacy_manager
        if privacy_manager is not None
        else PrivacyJobManager(effective_config, cpu_limiter=cpu_limiter)
    )
    rescue_job_manager: RescueJobManager = (
        rescue_manager
        if rescue_manager is not None
        else RescueJobManager(effective_config, cpu_limiter=cpu_limiter)
    )
    content_job_manager: ContentJobManager = (
        content_manager
        if content_manager is not None
        else ContentJobManager(effective_config, cpu_limiter=cpu_limiter)
    )
    job_manager.use_cpu_limiter(cpu_limiter)
    publish_job_manager.use_cpu_limiter(cpu_limiter)
    privacy_job_manager.use_cpu_limiter(cpu_limiter)
    rescue_job_manager.use_cpu_limiter(cpu_limiter)
    content_job_manager.use_cpu_limiter(cpu_limiter)
    heavy_ai_slots = threading.BoundedSemaphore(effective_config.heavy_ai_concurrency)
    advanced_ai_preparations: dict[str, AdvancedAIPreparation] = {}
    advanced_ai_reviews: dict[str, AIReviewManifest] = {}
    advanced_ai_revisions: dict[str, int] = {}
    advanced_ai_cancellations: dict[str, threading.Event] = {}
    advanced_ai_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        del application
        job_manager.start_cleanup()
        publish_job_manager.start_cleanup()
        privacy_job_manager.start_cleanup()
        rescue_job_manager.start_cleanup()
        content_job_manager.start_cleanup()
        try:
            yield
        finally:
            try:
                content_job_manager.shutdown()
            finally:
                try:
                    rescue_job_manager.shutdown()
                finally:
                    try:
                        privacy_job_manager.shutdown()
                    finally:
                        try:
                            publish_job_manager.shutdown()
                        finally:
                            job_manager.shutdown()
                            provider_vault.clear()

    app = FastAPI(
        title="VideoScope Local API",
        version=__version__,
        description=(
            "Local-only analysis job API. Uploaded videos remain under the "
            "configured application-data job directory and are analyzed by "
            "the same AnalysisPipeline used by the CLI."
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(effective_config.trusted_hosts),
    )
    app.state.job_manager = job_manager
    app.state.publish_job_manager = publish_job_manager
    app.state.privacy_job_manager = privacy_job_manager
    app.state.rescue_job_manager = rescue_job_manager
    app.state.content_job_manager = content_job_manager
    app.state.advanced_ai_preparations = advanced_ai_preparations
    app.state.advanced_ai_reviews = advanced_ai_reviews
    app.state.advanced_ai_cancellations = advanced_ai_cancellations
    app.state.connector_sessions = connector_sessions
    app.state.provider_vault = provider_vault

    @app.middleware("http")
    async def reject_cross_site_browser_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        origin = request.headers.get("origin")
        public_origin = origin in effective_config.allowed_browser_origins
        loopback_origin = origin is not None and _is_loopback_origin(origin)
        if request.method == "OPTIONS" and public_origin:
            requested_method = request.headers.get(
                "access-control-request-method", ""
            ).upper()
            requested_headers = {
                item.strip().casefold()
                for item in request.headers.get(
                    "access-control-request-headers", ""
                ).split(",")
                if item.strip()
            }
            allowed_headers = {
                "accept",
                "content-type",
                "last-event-id",
                "x-videoscope-session",
            }
            if requested_method not in {"GET", "POST", "PUT", "DELETE"} or not (
                requested_headers <= allowed_headers
            ):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Connector preflight is not allowed."},
                )
            response = Response(status_code=status.HTTP_204_NO_CONTENT)
            _add_connector_cors_headers(response, origin, request)
            return response
        if (
            origin is not None
            and not public_origin
            and not loopback_origin
            and not effective_config.allow_non_loopback_origin
        ):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Cross-site browser origin is not allowed."},
            )
        if public_origin and request.url.path not in {
            "/api/connector/status",
            "/api/connector/sessions",
        }:
            token = request.headers.get("x-videoscope-session")
            if not connector_sessions.valid(token):
                response = JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Connector pairing is required."},
                )
                _add_connector_cors_headers(response, origin, request)
                return response
        response = await call_next(request)
        if public_origin:
            _add_connector_cors_headers(response, origin, request)
        return response

    def _add_connector_cors_headers(
        response: Response, origin: str, request: Request
    ) -> None:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE"
        response.headers["Access-Control-Allow-Headers"] = (
            "Accept, Content-Type, Last-Event-ID, X-VideoScope-Session"
        )
        response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        response.headers["Vary"] = "Origin"
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"

    def _require_loopback_settings(request: Request) -> None:
        origin = request.headers.get("origin")
        if not _is_loopback_client(request) or (
            origin is not None and not _is_loopback_origin(origin)
        ):
            raise HTTPException(
                status_code=403,
                detail="Provider secrets can be changed only in the loopback UI.",
            )

    @app.get(
        "/api/connector/status",
        response_model=ConnectorStatus,
        summary="Discover the loopback connector without exposing local data",
    )
    async def connector_status() -> ConnectorStatus:
        return ConnectorStatus(version=__version__)

    @app.post(
        "/api/connector/sessions",
        response_model=ConnectorSession,
        summary="Pair an allowlisted public site with this connector",
    )
    async def create_connector_session(
        pairing: ConnectorPairingRequest,
    ) -> ConnectorSession:
        try:
            return connector_sessions.pair(pairing.pairing_code)
        except PermissionError as exc:
            raise HTTPException(
                status_code=401, detail="Pairing code is invalid."
            ) from exc

    @app.delete(
        "/api/connector/sessions/current",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def revoke_connector_session(
        x_videoscope_session: Annotated[str | None, Header()] = None,
    ) -> Response:
        connector_sessions.revoke(x_videoscope_session)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/connector/providers",
        response_model=list[ProviderProfileSummary],
        summary="List configured in-memory BYOK profiles without secrets",
    )
    async def connector_provider_profiles() -> list[ProviderProfileSummary]:
        return list(provider_vault.list())

    @app.put(
        "/api/connector/providers/{profile_id}",
        response_model=ProviderProfileSummary,
        include_in_schema=False,
    )
    async def put_connector_provider_profile(
        profile_id: str,
        profile: ProviderProfileInput,
        request: Request,
    ) -> ProviderProfileSummary:
        _require_loopback_settings(request)
        if profile.profile_id != profile_id:
            raise HTTPException(status_code=422, detail="Provider profile ID mismatch.")
        return provider_vault.put(profile)

    @app.delete(
        "/api/connector/providers/{profile_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        include_in_schema=False,
    )
    async def delete_connector_provider_profile(
        profile_id: str, request: Request
    ) -> Response:
        _require_loopback_settings(request)
        provider_vault.delete(profile_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/docs",
        response_class=HTMLResponse,
        include_in_schema=False,
        summary="Read offline API documentation",
    )
    async def offline_docs() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VideoScope Local API</title>
  <style>
    body { max-width: 920px; margin: 40px auto; padding: 0 20px;
      font: 16px/1.55 system-ui, sans-serif; color: #172033; }
    code { background: #eef2f7; padding: 2px 5px; border-radius: 4px; }
    li { margin: 7px 0; }
    a { color: #1559b7; }
  </style>
</head>
<body>
  <h1>VideoScope Local API</h1>
  <p>This documentation is self-contained and loads no remote resources.</p>
  <p><a href="/openapi.json">OpenAPI JSON</a></p>
  <h2>Endpoints</h2>
  <ul>
    <li><code>GET /api/health</code></li>
    <li><code>GET /api/detectors</code></li>
    <li><code>POST /api/jobs</code></li>
    <li><code>GET /api/jobs/{job_id}</code></li>
    <li><code>GET /api/jobs/{job_id}/events</code></li>
    <li><code>GET /api/jobs/{job_id}/report</code></li>
    <li><code>GET /api/jobs/{job_id}/artifacts/{path}</code></li>
    <li><code>DELETE /api/jobs/{job_id}</code></li>
    <li><code>GET /api/privacy/profiles</code></li>
    <li><code>POST /api/privacy/jobs</code></li>
    <li><code>GET /api/privacy/jobs/{job_id}/risk-map</code></li>
    <li><code>PUT /api/privacy/jobs/{job_id}/review</code></li>
    <li><code>POST /api/privacy/jobs/{job_id}/prepare</code></li>
    <li><code>POST /api/privacy/jobs/{job_id}/confirm</code></li>
    <li><code>POST /api/content/jobs</code></li>
    <li><code>PUT /api/content/jobs/{job_id}/storyboard</code></li>
    <li><code>POST /api/content/jobs/{job_id}/previews</code></li>
    <li><code>POST /api/content/jobs/{job_id}/confirm</code></li>
  </ul>
  <p>See <code>docs/web-api.md</code> in the source distribution for request
  examples, lifecycle details and security boundaries.</p>
</body>
</html>
"""
        )

    @app.get(
        "/api/health",
        response_model=HealthResponse,
        summary="Check local API health",
    )
    async def health() -> HealthResponse:
        return HealthResponse(
            active_jobs=(
                job_manager.active_job_count()
                + publish_job_manager.active_job_count()
                + privacy_job_manager.active_job_count()
                + rescue_job_manager.active_job_count()
                + content_job_manager.active_job_count()
            )
        )

    @app.get(
        "/api/detectors",
        response_model=list[DetectorResponse],
        summary="List CPU and optional local detectors",
    )
    async def detectors() -> list[DetectorResponse]:
        registry = create_optional_detector_registry(
            enable_ai=True,
            enable_ocr=True,
        )
        return [
            DetectorResponse(
                id=detector.id,
                display_name=detector.display_name,
                version=detector.version,
                description=detector.description,
                default_enabled=detector.default_enabled,
                requires_prompt=detector.requirements.requires_prompt,
                requires_gpu=detector.requirements.requires_gpu,
                requires_network=detector.requirements.requires_network,
                optional_packages=detector.requirements.optional_packages,
                estimated_cost=detector.requirements.estimated_cost.value,
                category=(
                    "ocr"
                    if detector.id == "text_stability"
                    else "ai"
                    if detector.requirements.optional_packages
                    else "cpu"
                ),
                available=_detector_availability(
                    detector.requirements.optional_packages
                )[0],
                unavailable_reason=_detector_availability(
                    detector.requirements.optional_packages
                )[1],
            )
            for detector in registry.list_available()
        ]

    @app.get(
        "/api/privacy/profiles",
        response_model=list[ShareAudienceProfile],
        summary="List versioned local Safe Sharing profiles",
    )
    async def privacy_profiles() -> list[ShareAudienceProfile]:
        return list(list_share_audience_profiles())

    @app.post(
        "/api/privacy/jobs",
        response_model=PrivacyJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Upload a local video and scan for privacy risks",
    )
    async def create_privacy_job(
        video: Annotated[
            UploadFile,
            File(
                description=(
                    "Local video upload. Extension and MIME are hints only; "
                    "the Safe Sharing pipeline performs ffprobe validation."
                )
            ),
        ],
        profile_id: Annotated[str, Form()],
        enable_ocr: Annotated[bool, Form()] = False,
    ) -> PrivacyJobResponse:
        filename = (video.filename or "").strip()
        if not filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded video must have a filename.",
            )
        try:
            get_share_audience_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unknown Safe Sharing profile.",
            ) from exc
        record = privacy_job_manager.reserve_job(
            original_filename=filename,
            profile_id=profile_id,
            warnings=_upload_warnings(filename, video.content_type),
            enable_ocr=enable_ocr,
        )
        temporary_path = record.input_path.with_suffix(
            f"{record.input_path.suffix}.upload"
        )
        size = 0
        try:
            with temporary_path.open("wb") as stream:
                while chunk := await video.read(effective_config.upload_chunk_bytes):
                    size += len(chunk)
                    if size > effective_config.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail="Uploaded video exceeds the configured size limit.",
                        )
                    stream.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded video is empty.",
                )
            temporary_path.replace(record.input_path)
            with record.lock:
                record.upload_size_bytes = size
            privacy_job_manager.persist(record.job_id)
            return privacy_job_manager.submit_scan(record.job_id)
        except BaseException:
            privacy_job_manager.discard_reserved(record.job_id)
            raise
        finally:
            await video.close()

    @app.get(
        "/api/privacy/jobs/{job_id}",
        response_model=PrivacyJobResponse,
        summary="Read one Safe Sharing job state",
    )
    async def get_privacy_job(job_id: str) -> PrivacyJobResponse:
        try:
            return privacy_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Privacy job not found.",
            ) from exc

    @app.get(
        "/api/privacy/jobs/{job_id}/events",
        response_class=StreamingResponse,
        summary="Stream ordered Safe Sharing progress using server-sent events",
    )
    async def privacy_job_events(
        request: Request,
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            initial = privacy_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Privacy job not found.",
            ) from exc
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be an integer.",
                ) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor, initial
            while True:
                events = privacy_job_manager.events_after(job_id, cursor)
                for event in events:
                    cursor = event.sequence
                    yield _event_payload(event)
                initial = privacy_job_manager.snapshot(job_id)
                if initial.status.terminal and not privacy_job_manager.events_after(
                    job_id, cursor
                ):
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/privacy/jobs/{job_id}/risk-map",
        response_model=PrivacyRiskMap,
        summary="Read the private review risk map",
    )
    async def privacy_risk_map(job_id: str, response: Response) -> PrivacyRiskMap:
        try:
            risk_map = privacy_job_manager.risk_map(job_id)
            response.headers["Cache-Control"] = "no-store"
            return risk_map
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except PrivacyJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put(
        "/api/privacy/jobs/{job_id}/review",
        response_model=PrivacyJobResponse,
        summary="Store human decisions for the current private risk map",
    )
    async def review_privacy_job(
        job_id: str, review: PrivacyReviewRequest
    ) -> PrivacyJobResponse:
        try:
            return privacy_job_manager.review(
                job_id,
                review.reviews,
                manual_visual_regions=review.manual_visual_regions,
                manual_audio_intervals=review.manual_audio_intervals,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except PrivacyJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/privacy/jobs/{job_id}/prepare",
        response_model=PrivacyJobResponse,
        summary="Build a digest-bound plan and private preview",
    )
    async def prepare_privacy_job(job_id: str) -> PrivacyJobResponse:
        try:
            return cast(
                PrivacyJobResponse,
                await run_in_threadpool(privacy_job_manager.prepare, job_id),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except PrivacyJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/privacy/jobs/{job_id}/plan",
        response_model=PrivacyPlan,
        summary="Read the exact confirmable privacy plan",
    )
    async def privacy_plan(job_id: str) -> PrivacyPlan:
        try:
            return privacy_job_manager.plan(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except PrivacyJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/privacy/jobs/{job_id}/confirm",
        response_model=PrivacyJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Confirm and execute one exact privacy plan",
    )
    async def confirm_privacy_job(
        job_id: str, confirmation: PrivacyConfirmation
    ) -> PrivacyJobResponse:
        try:
            return privacy_job_manager.confirm(job_id, confirmation.plan_digest)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except (PrivacyConfirmationMismatchError, PrivacyJobStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/privacy/jobs/{job_id}/artifacts/{artifact_path:path}",
        response_class=FileResponse,
        summary="Read one safely-contained public Safe Sharing artifact",
    )
    async def privacy_artifact(job_id: str, artifact_path: str) -> FileResponse:
        try:
            path = privacy_job_manager.resolve_public_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except PrivacyArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=409, detail="Safe Sharing artifacts are not available."
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found.") from exc
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.get(
        "/api/privacy/jobs/{job_id}/private-artifacts/{artifact_path:path}",
        response_class=FileResponse,
        summary="Read allowlisted private review evidence",
    )
    async def privacy_private_artifact(job_id: str, artifact_path: str) -> FileResponse:
        try:
            path = privacy_job_manager.resolve_private_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        except PrivacyArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=409,
                detail="Private review artifacts are not available.",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="Private review artifact not found."
            ) from exc
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(
            path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.delete(
        "/api/privacy/jobs/{job_id}",
        response_model=PrivacyJobResponse | None,
        summary="Cancel an active privacy job or delete a terminal job",
    )
    async def delete_privacy_job(job_id: str) -> PrivacyJobResponse | Response:
        try:
            result = privacy_job_manager.delete_or_cancel(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Privacy job not found."
            ) from exc
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return result

    @app.post(
        "/api/rescue/jobs",
        response_model=RescueJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Upload a local video and prepare a Video Rescue plan",
    )
    async def create_rescue_job(
        video: Annotated[UploadFile, File(description="Local video upload.")],
        strategy: Annotated[RescueStrategy, Form()] = RescueStrategy.CONSERVATIVE,
        symptoms: Annotated[tuple[RescueSymptom, ...], Form()] = (),
        locked_ranges: Annotated[str, Form()] = "[]",
        balanced_strength_limit: Annotated[float, Form(gt=0, le=1)] = 1.0,
    ) -> RescueJobResponse:
        filename = (video.filename or "").strip()
        if not filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded video must have a filename.",
            )
        parsed_locked_ranges = _parse_rescue_locked_ranges(locked_ranges)
        record = rescue_job_manager.reserve_job(
            original_filename=filename,
            strategy=strategy,
            symptoms=symptoms,
            locked_ranges=parsed_locked_ranges,
            balanced_strength_limit=balanced_strength_limit,
            warnings=_upload_warnings(filename, video.content_type),
        )
        temporary_path = (
            record.directory / f".upload-{token_hex(16)}{record.input_path.suffix}"
        )
        size = 0
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temporary_path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                digest = hashlib.sha256()
                while chunk := await video.read(effective_config.upload_chunk_bytes):
                    size += len(chunk)
                    if size > effective_config.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail="Uploaded video exceeds the configured size limit.",
                        )
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded video is empty.",
                )
            # The manager re-opens no-follow, hashes, and pins this path
            # before exposing it to the path-based Task 9 pipeline.
            rescue_job_manager.commit_input_snapshot(record.job_id, temporary_path)
            if (
                rescue_job_manager.require(record.job_id).input_sha256
                != digest.hexdigest()
            ):
                raise HTTPException(status_code=400, detail="Uploaded video changed.")
            return rescue_job_manager.submit_prepare(record.job_id)
        except BaseException:
            rescue_job_manager.discard_reserved(record.job_id)
            raise
        finally:
            await video.close()

    @app.get(
        "/api/rescue/jobs/{job_id}",
        response_model=RescueJobResponse,
        summary="Read one local Video Rescue job state",
    )
    async def get_rescue_job(job_id: str) -> RescueJobResponse:
        try:
            return rescue_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc

    @app.get(
        "/api/rescue/jobs/{job_id}/events",
        response_class=StreamingResponse,
        summary="Stream ordered Video Rescue progress using server-sent events",
    )
    async def rescue_job_events(
        request: Request,
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            current = rescue_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Last-Event-ID must be an integer."
                ) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor, current
            while True:
                for event in rescue_job_manager.events_after(job_id, cursor):
                    cursor = event.sequence
                    yield _event_payload(event)
                current = rescue_job_manager.snapshot(job_id)
                if current.status.terminal and not rescue_job_manager.events_after(
                    job_id, cursor
                ):
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/rescue/jobs/{job_id}/damage-map",
        response_model=MediaDamageMap,
        summary="Read the current local Rescue damage map",
    )
    async def rescue_damage_map(job_id: str, response: Response) -> MediaDamageMap:
        try:
            damage_map = rescue_job_manager.damage_map(job_id)
            response.headers["Cache-Control"] = "no-store"
            return damage_map
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        except RescueJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/rescue/jobs/{job_id}/plan",
        response_model=RescuePlan,
        summary="Read the exact confirmable Rescue plan",
    )
    async def rescue_plan(job_id: str) -> RescuePlan:
        try:
            return rescue_job_manager.plan(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        except RescueJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/rescue/jobs/{job_id}/confirm",
        response_model=RescueJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Confirm and execute one exact Video Rescue plan",
    )
    async def confirm_rescue_job(
        job_id: str, confirmation: RescueConfirmationRequest
    ) -> RescueJobResponse:
        try:
            core_confirmation = RescueConfirmation.model_validate(
                confirmation.model_dump(mode="json")
            )
            return rescue_job_manager.confirm(job_id, core_confirmation)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        except (RescueConfirmationMismatchError, RescueJobStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid Rescue confirmation."
            ) from exc

    @app.get(
        "/api/rescue/jobs/{job_id}/artifacts/{artifact_path:path}",
        response_class=StreamingResponse,
        summary="Read one manifest-listed public Video Rescue artifact",
    )
    async def rescue_artifact(
        request: Request, job_id: str, artifact_path: str
    ) -> StreamingResponse:
        try:
            artifact = rescue_job_manager.open_public_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        except RescueArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=409, detail="Video Rescue artifacts are not available."
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found.") from exc
        return _pinned_rescue_response(artifact, request)

    @app.get(
        "/api/rescue/jobs/{job_id}/private-artifacts/{artifact_path:path}",
        response_class=StreamingResponse,
        summary="Read one loopback-only private Rescue preview",
    )
    async def rescue_private_artifact(
        request: Request, job_id: str, artifact_path: str
    ) -> StreamingResponse:
        if not _is_loopback_client(request):
            raise HTTPException(
                status_code=403, detail="Private preview is local-only."
            )
        try:
            artifact = rescue_job_manager.open_private_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        except RescueArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=409, detail="Private Rescue preview is not available."
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="Private preview not found."
            ) from exc
        response = _pinned_rescue_response(artifact, request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.delete(
        "/api/rescue/jobs/{job_id}",
        response_model=RescueJobResponse | None,
        summary="Cancel an active Rescue job or delete a terminal job",
    )
    async def delete_rescue_job(job_id: str) -> RescueJobResponse | Response:
        try:
            result = rescue_job_manager.delete_or_cancel(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Video Rescue job not found."
            ) from exc
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return result

    @app.post(
        "/api/content/jobs",
        response_model=ContentJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Upload a local video and build a useful-content map",
    )
    async def create_content_job(
        video: Annotated[UploadFile, File()],
        goal: Annotated[ContentGoal, Form()] = ContentGoal.FAITHFUL_CLEAN,
        config_json: Annotated[str | None, Form()] = None,
        transcript: Annotated[UploadFile | None, File()] = None,
    ) -> ContentJobResponse:
        filename = (video.filename or "").strip()
        if not filename:
            raise HTTPException(status_code=400, detail="Video filename is required.")
        try:
            raw_config: object = (
                json.loads(config_json) if config_json is not None else {}
            )
            if not isinstance(raw_config, dict):
                raise ValueError("content config must be an object")
            if (
                len((config_json or "").encode("utf-8"))
                > effective_config.maximum_config_bytes
            ):
                raise ValueError("content config exceeds the size limit")
            requested_goal = raw_config.get("goal")
            if requested_goal is not None and requested_goal != goal.value:
                raise ValueError("form goal and config goal do not match")
            raw_config["goal"] = goal.value
            content_config = ContentConfig.model_validate(raw_config)
            pipeline_config = ContentPipelineConfig(
                content=content_config,
                keep_workspace=True,
            )
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Useful-content configuration is not valid.",
            ) from exc
        transcript_name = (
            (transcript.filename or "").strip() if transcript is not None else None
        )
        if transcript is not None and not transcript_name:
            raise HTTPException(
                status_code=400, detail="Transcript filename is required."
            )
        record = content_job_manager.reserve_job(
            original_filename=filename,
            config=pipeline_config,
            transcript_filename=transcript_name,
            warnings=_upload_warnings(filename, video.content_type),
        )
        temporary_video = record.input_path.with_suffix(
            f"{record.input_path.suffix}.upload"
        )
        temporary_transcript = (
            record.transcript_path.with_suffix(
                f"{record.transcript_path.suffix}.upload"
            )
            if record.transcript_path is not None
            else None
        )
        size = 0
        try:
            with temporary_video.open("wb") as stream:
                while chunk := await video.read(effective_config.upload_chunk_bytes):
                    size += len(chunk)
                    if size > effective_config.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail="Uploaded video exceeds the configured size limit.",
                        )
                    stream.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Uploaded video is empty.")
            temporary_video.replace(record.input_path)
            if transcript is not None and temporary_transcript is not None:
                transcript_size = 0
                with temporary_transcript.open("wb") as stream:
                    while chunk := await transcript.read(
                        effective_config.upload_chunk_bytes
                    ):
                        transcript_size += len(chunk)
                        if transcript_size > effective_config.maximum_config_bytes:
                            raise HTTPException(
                                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                detail="Transcript exceeds the configured size limit.",
                            )
                        stream.write(chunk)
                if transcript_size == 0:
                    raise HTTPException(status_code=400, detail="Transcript is empty.")
                transcript_target = record.transcript_path
                if transcript_target is None:
                    raise RuntimeError("transcript target is unavailable")
                temporary_transcript.replace(transcript_target)
            content_job_manager.commit_upload(record.job_id)
            return content_job_manager.submit_prepare(record.job_id)
        except BaseException:
            content_job_manager.discard_reserved(record.job_id)
            raise
        finally:
            temporary_video.unlink(missing_ok=True)
            if temporary_transcript is not None:
                temporary_transcript.unlink(missing_ok=True)
            await video.close()
            if transcript is not None:
                await transcript.close()

    @app.get(
        "/api/content/jobs/{job_id}",
        response_model=ContentJobResponse,
    )
    async def get_content_job(job_id: str) -> ContentJobResponse:
        try:
            return content_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc

    @app.get(
        "/api/content/jobs/{job_id}/events",
        response_class=StreamingResponse,
    )
    async def content_job_events(
        request: Request,
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            current = content_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be an integer.",
                ) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor, current
            while True:
                for event in content_job_manager.events_after(job_id, cursor):
                    cursor = event.sequence
                    yield _event_payload(event)
                current = content_job_manager.snapshot(job_id)
                if current.status.terminal and not content_job_manager.events_after(
                    job_id, cursor
                ):
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/content/jobs/{job_id}/map",
        response_model=ContentMap,
    )
    async def content_job_map(job_id: str) -> ContentMap:
        try:
            return content_job_manager.content_map(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/content/jobs/{job_id}/ai/prepare",
        response_model=AISuggestionBatch,
        summary="Prepare private grounded suggestions with explicit local AI",
    )
    async def prepare_content_ai(
        job_id: str,
        settings: AdvancedAIPrepareRequest,
        request: Request,
    ) -> AISuggestionBatch:
        if not _is_loopback_client(request):
            raise HTTPException(
                status_code=403,
                detail="Private local AI review is available only from loopback.",
            )
        try:
            (
                source_path,
                transcript_path,
                output_directory,
                input_hash,
                revision,
                _existing_ranges,
            ) = content_job_manager.advanced_ai_context(job_id)
            cancellation = threading.Event()
            with advanced_ai_lock:
                previous = advanced_ai_cancellations.get(job_id)
                if previous is not None:
                    previous.set()
                advanced_ai_cancellations[job_id] = cancellation

            dependencies: AdvancedAIDependencies | None = None
            if settings.provider_profile_id is not None:
                if not settings.remote_data_consent:
                    raise ValueError(
                        "remote provider use requires explicit data-transfer consent"
                    )
                profile = provider_vault.get(settings.provider_profile_id)
                if profile.summary.protocol is not ProviderProtocol.OPENAI_COMPATIBLE:
                    raise ValueError("selected provider protocol is not supported here")
                if (
                    ProviderCapability.STRUCTURED_TEXT
                    not in profile.summary.capabilities
                ):
                    raise ValueError(
                        "selected provider lacks structured text capability"
                    )
                content_provider = OpenAICompatibleContentIntelligenceProvider(
                    provider_id=profile.summary.provider_id,
                    model_id=profile.summary.model_id,
                    api_base_url=profile.summary.api_base_url,
                    api_key=profile.api_key,
                    request_json_object=profile.summary.request_json_object,
                )
                dependencies = AdvancedAIDependencies(content_provider=content_provider)

            def run() -> AdvancedAIPreparation:
                with heavy_ai_slots:
                    config = AdvancedAIConfig(
                        output_directory=output_directory / "advanced-ai",
                        transcript_path=transcript_path,
                        asr_model_id=settings.asr_model_id,
                        asr_language=settings.asr_language,
                        semantic_model_id=settings.semantic_model_id,
                        ollama_endpoint=settings.ollama_endpoint,
                        locale=cast(Any, settings.locale),
                        device=settings.device,
                        allow_model_download=settings.allow_model_download,
                        maximum_suggestions=settings.maximum_suggestions,
                        keep_workspace=True,
                        cancellation_callback=cancellation.is_set,
                    )
                    pipeline = (
                        AdvancedAIContentPipeline(config)
                        if dependencies is None
                        else AdvancedAIContentPipeline(
                            config, dependencies=dependencies
                        )
                    )
                    return pipeline.prepare(source_path)

            preparation = cast(
                AdvancedAIPreparation,
                await run_in_threadpool(run),
            )
            if preparation.suggestions.input_hash != input_hash:
                raise ContentJobStateError("AI suggestion source identity changed")
            (
                _current_source,
                _current_transcript,
                _current_output,
                current_hash,
                current_revision,
                _current_ranges,
            ) = content_job_manager.advanced_ai_context(job_id)
            if current_hash != input_hash or current_revision != revision:
                raise ContentJobStateError(
                    "AI suggestion context changed during preparation"
                )
            if cancellation.is_set():
                raise AdvancedAICancelledError("Advanced AI preparation was cancelled")
            with advanced_ai_lock:
                advanced_ai_preparations[job_id] = preparation
                advanced_ai_reviews.pop(job_id, None)
                advanced_ai_revisions[job_id] = revision
            return preparation.suggestions
        except AdvancedAICancelledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=422,
                detail="Advanced AI configuration or output is invalid.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "AI preparation failed. Check the selected local model or "
                    f"BYOK provider: {type(exc).__name__}."
                ),
            ) from exc
        finally:
            with advanced_ai_lock:
                current = advanced_ai_cancellations.get(job_id)
                if "cancellation" in locals() and current is cancellation:
                    advanced_ai_cancellations.pop(job_id, None)

    @app.get(
        "/api/content/jobs/{job_id}/ai/suggestions",
        response_model=AISuggestionBatch,
    )
    async def content_ai_suggestions(
        job_id: str, request: Request
    ) -> AISuggestionBatch:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Loopback access is required.")
        with advanced_ai_lock:
            preparation = advanced_ai_preparations.get(job_id)
        if preparation is None:
            raise HTTPException(status_code=404, detail="AI suggestions not found.")
        return preparation.suggestions

    @app.put(
        "/api/content/jobs/{job_id}/ai/review",
        response_model=AIReviewManifest,
    )
    async def review_content_ai(
        job_id: str,
        review: AdvancedAIReviewRequest,
        request: Request,
    ) -> AIReviewManifest:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Loopback access is required.")
        try:
            _source, _transcript, _output, _hash, revision, _ranges = (
                content_job_manager.advanced_ai_context(job_id)
            )
            with advanced_ai_lock:
                preparation = advanced_ai_preparations.get(job_id)
                prepared_revision = advanced_ai_revisions.get(job_id)
            if preparation is None or prepared_revision != revision:
                raise ContentJobStateError("AI suggestions are missing or stale")
            manifest = build_review_manifest(preparation.suggestions, review.decisions)
            write_intelligence_json(
                manifest,
                preparation.private_root / "review-manifest.json",
            )
            with advanced_ai_lock:
                advanced_ai_reviews[job_id] = manifest
            return manifest
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=422, detail="AI review does not match suggestions."
            ) from exc

    @app.post(
        "/api/content/jobs/{job_id}/ai/apply",
        response_model=ContentJobResponse,
        summary="Apply accepted AI ranges through the ordinary C revision gate",
    )
    async def apply_content_ai(
        job_id: str,
        apply_request: AdvancedAIApplyRequest,
        request: Request,
    ) -> ContentJobResponse:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Loopback access is required.")
        try:
            _source, _transcript, _output, _hash, revision, existing_ranges = (
                content_job_manager.advanced_ai_context(job_id)
            )
            if revision != apply_request.expected_revision:
                raise ContentRevisionConflictError("storyboard revision is stale")
            with advanced_ai_lock:
                preparation = advanced_ai_preparations.get(job_id)
                manifest = advanced_ai_reviews.get(job_id)
                prepared_revision = advanced_ai_revisions.get(job_id)
            if preparation is None or manifest is None or prepared_revision != revision:
                raise ContentJobStateError("reviewed AI suggestions are unavailable")
            accepted = reviewed_content_ranges(
                preparation.suggestions,
                manifest,
            )
            combined = (*existing_ranges, *accepted)
            unique: dict[tuple[str, float, float], ContentRangeInput] = {}
            for item in combined:
                if item.label is not None and len(item.label) > 300:
                    raise ValueError("accepted AI label exceeds the Web limit")
                key = (
                    item.kind.value,
                    item.source_range.start_seconds,
                    item.source_range.end_seconds,
                )
                unique[key] = ContentRangeInput(
                    kind=item.kind,
                    start_seconds=item.source_range.start_seconds,
                    end_seconds=item.source_range.end_seconds,
                    label=item.label,
                )
            revision_request = ContentStoryboardRevisionRequest(
                expected_revision=revision,
                ranges=tuple(unique[key] for key in sorted(unique)),
            )
            result = cast(
                ContentJobResponse,
                await run_in_threadpool(
                    content_job_manager.revise,
                    job_id,
                    revision_request,
                ),
            )
            with advanced_ai_lock:
                advanced_ai_preparations.pop(job_id, None)
                advanced_ai_reviews.pop(job_id, None)
                advanced_ai_revisions.pop(job_id, None)
            return result
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentRevisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=422, detail="Accepted AI ranges are invalid."
            ) from exc

    @app.put(
        "/api/content/jobs/{job_id}/storyboard",
        response_model=ContentJobResponse,
    )
    async def revise_content_storyboard(
        job_id: str,
        revision: ContentStoryboardRevisionRequest,
    ) -> ContentJobResponse:
        try:
            return cast(
                ContentJobResponse,
                await run_in_threadpool(
                    content_job_manager.revise,
                    job_id,
                    revision,
                ),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentRevisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ContentJobStateError, ContentError, ValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Storyboard revision is not valid.",
            ) from exc

    @app.post(
        "/api/content/jobs/{job_id}/previews",
        response_model=ContentJobResponse,
    )
    async def create_content_previews(job_id: str) -> ContentJobResponse:
        try:
            return cast(
                ContentJobResponse,
                await run_in_threadpool(content_job_manager.preview, job_id),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/content/jobs/{job_id}/previews",
        response_model=list[ContentJoinPreview],
    )
    async def list_content_previews(job_id: str) -> list[ContentJoinPreview]:
        try:
            return list(content_job_manager.previews(job_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/content/jobs/{job_id}/plan",
        response_model=ContentPlan,
    )
    async def content_job_plan(job_id: str) -> ContentPlan:
        try:
            return content_job_manager.plan(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/content/jobs/{job_id}/confirm",
        response_model=ContentJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def confirm_content_job(
        job_id: str,
        confirmation: ContentConfirmationRequest,
    ) -> ContentJobResponse:
        try:
            return content_job_manager.confirm(job_id, confirmation)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except (ContentConfirmationMismatchError, ContentJobStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/content/jobs/{job_id}/previews/{artifact_path:path}",
        response_class=FileResponse,
    )
    async def content_preview(
        request: Request,
        job_id: str,
        artifact_path: str,
    ) -> FileResponse:
        if not _is_loopback_client(request):
            raise HTTPException(
                status_code=403, detail="Private previews are local only."
            )
        try:
            path = content_job_manager.resolve_preview(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=409, detail="Preview is unavailable."
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Preview not found.") from exc
        media_type, _encoding = mimetypes.guess_type(path.name)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.get(
        "/api/content/jobs/{job_id}/artifacts/{artifact_path:path}",
        response_class=FileResponse,
    )
    async def content_artifact(job_id: str, artifact_path: str) -> FileResponse:
        try:
            path = content_job_manager.resolve_public_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=409, detail="Artifacts are unavailable."
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found.") from exc
        media_type, _encoding = mimetypes.guess_type(path.name)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.delete(
        "/api/content/jobs/{job_id}",
        response_model=ContentJobResponse | None,
    )
    async def delete_content_job(job_id: str) -> ContentJobResponse | Response:
        try:
            result = content_job_manager.delete_or_cancel(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        with advanced_ai_lock:
            cancellation = advanced_ai_cancellations.pop(job_id, None)
            if cancellation is not None:
                cancellation.set()
            advanced_ai_preparations.pop(job_id, None)
            advanced_ai_reviews.pop(job_id, None)
            advanced_ai_revisions.pop(job_id, None)
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return result

    @app.delete(
        "/api/content/jobs/{job_id}/ai",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Cancel active private AI preparation and discard its review state",
    )
    async def cancel_content_ai(job_id: str, request: Request) -> Response:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Loopback access is required.")
        try:
            content_job_manager.advanced_ai_context(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Content job not found."
            ) from exc
        except ContentJobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        with advanced_ai_lock:
            cancellation = advanced_ai_cancellations.get(job_id)
            if cancellation is not None:
                cancellation.set()
            advanced_ai_preparations.pop(job_id, None)
            advanced_ai_reviews.pop(job_id, None)
            advanced_ai_revisions.pop(job_id, None)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/publish/profiles",
        response_model=list[PublishProfile],
        summary="List local Publish Ready profiles",
    )
    async def publish_profiles() -> list[PublishProfile]:
        return list(list_publish_profiles())

    @app.post(
        "/api/publish/jobs",
        response_model=PublishJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Upload a local video and prepare a Publish Ready plan",
    )
    async def create_publish_job(
        video: Annotated[
            UploadFile,
            File(
                description=(
                    "Local video upload. Extension and MIME are hints only; "
                    "the core pipeline performs authoritative ffprobe validation."
                )
            ),
        ],
        profile_id: Annotated[PublishProfileId, Form()],
    ) -> PublishJobResponse:
        filename = (video.filename or "").strip()
        if not filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded video must have a filename.",
            )
        record = publish_job_manager.reserve_job(
            original_filename=filename,
            profile_id=profile_id,
            warnings=_upload_warnings(filename, video.content_type),
        )
        temporary_path = record.input_path.with_suffix(
            f"{record.input_path.suffix}.upload"
        )
        size = 0
        try:
            with temporary_path.open("wb") as stream:
                while chunk := await video.read(effective_config.upload_chunk_bytes):
                    size += len(chunk)
                    if size > effective_config.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "Uploaded video exceeds the configured size limit."
                            ),
                        )
                    stream.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded video is empty.",
                )
            temporary_path.replace(record.input_path)
            record.update_upload_size(size)
            return publish_job_manager.submit_prepare(record.job_id)
        except BaseException:
            publish_job_manager.discard_reserved(record.job_id)
            raise
        finally:
            await video.close()

    @app.get(
        "/api/publish/jobs/{job_id}",
        response_model=PublishJobResponse,
        summary="Read one Publish Ready job state",
    )
    async def get_publish_job(job_id: str) -> PublishJobResponse:
        try:
            return publish_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish job not found.",
            ) from exc

    @app.get(
        "/api/publish/jobs/{job_id}/events",
        response_class=StreamingResponse,
        summary="Stream ordered Publish Ready progress using server-sent events",
    )
    async def publish_job_events(
        request: Request,
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            initial = publish_job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish job not found.",
            ) from exc
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be an integer.",
                ) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor, initial
            while True:
                events = publish_job_manager.events_after(job_id, cursor)
                for event in events:
                    cursor = event.sequence
                    yield _event_payload(event)
                initial = publish_job_manager.snapshot(job_id)
                if initial.status.terminal and not publish_job_manager.events_after(
                    job_id,
                    cursor,
                ):
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/api/publish/jobs/{job_id}/plan",
        response_model=PublishPlan,
        summary="Read the exact confirmable Publish Ready plan",
    )
    async def publish_plan(job_id: str) -> PublishPlan:
        try:
            return publish_job_manager.plan(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish job not found.",
            ) from exc
        except PublishJobStateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.post(
        "/api/publish/jobs/{job_id}/confirm",
        response_model=PublishJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Confirm and execute one exact Publish Ready plan",
    )
    async def confirm_publish_job(
        job_id: str,
        confirmation: PublishConfirmation,
    ) -> PublishJobResponse:
        try:
            return publish_job_manager.confirm(job_id, confirmation.plan_digest)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish job not found.",
            ) from exc
        except (PublishConfirmationMismatchError, PublishJobStateError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.get(
        "/api/publish/jobs/{job_id}/artifacts/{artifact_path:path}",
        response_class=FileResponse,
        summary="Read a safely-contained Publish Ready artifact",
    )
    async def publish_artifact(job_id: str, artifact_path: str) -> FileResponse:
        try:
            path = publish_job_manager.resolve_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish job not found.",
            ) from exc
        except PublishArtifactUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Publish artifacts are not available.",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish artifact not found.",
            ) from exc
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(
            path,
            media_type=media_type or "application/octet-stream",
        )

    @app.delete(
        "/api/publish/jobs/{job_id}",
        response_model=PublishJobResponse | None,
        summary="Cancel an active Publish job or delete a terminal job",
    )
    async def delete_publish_job(job_id: str) -> PublishJobResponse | Response:
        try:
            result = publish_job_manager.delete_or_cancel(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Publish job not found.",
            ) from exc
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return result

    @app.post(
        "/api/jobs",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Upload a local video and queue analysis",
    )
    async def create_job(
        video: Annotated[
            UploadFile,
            File(
                description=(
                    "Local video upload. Extension and MIME are hints only; "
                    "ffprobe performs authoritative validation."
                )
            ),
        ],
        prompt: Annotated[str | None, Form()] = None,
        configuration_json: Annotated[
            str | None,
            Form(
                alias="config",
                description="UTF-8 JSON matching AnalysisConfig.",
            ),
        ] = None,
    ) -> JobResponse:
        filename = (video.filename or "").strip()
        if not filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded video must have a filename.",
            )
        if (
            prompt is not None
            and len(prompt) > effective_config.maximum_prompt_characters
        ):
            raise HTTPException(
                status_code=413,
                detail="Prompt is too large.",
            )
        analysis_config = _parse_analysis_config(
            configuration_json,
            maximum_bytes=effective_config.maximum_config_bytes,
        )
        record = job_manager.reserve_job(
            original_filename=filename,
            prompt=prompt,
            analysis_config=analysis_config,
            warnings=_upload_warnings(filename, video.content_type),
        )
        temporary_path = record.input_path.with_suffix(
            f"{record.input_path.suffix}.upload"
        )
        size = 0
        try:
            with temporary_path.open("wb") as stream:
                while chunk := await video.read(effective_config.upload_chunk_bytes):
                    size += len(chunk)
                    if size > effective_config.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="Uploaded video exceeds the configured size limit.",
                        )
                    stream.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded video is empty.",
                )
            temporary_path.replace(record.input_path)
            record.update_upload_size(size)
            return job_manager.submit(record.job_id)
        except BaseException:
            job_manager.discard_reserved(record.job_id)
            raise
        finally:
            await video.close()

    @app.get(
        "/api/jobs/{job_id}/video",
        response_class=FileResponse,
        summary="Stream the retained local source video",
    )
    async def source_video(job_id: str) -> FileResponse:
        try:
            record = job_manager.require(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            ) from exc
        if not record.input_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job video is not available.",
            )
        media_type, _ = mimetypes.guess_type(record.input_path.name)
        return FileResponse(
            record.input_path,
            media_type=media_type or "application/octet-stream",
            filename=None,
        )

    @app.get(
        "/api/jobs/{job_id}",
        response_model=JobResponse,
        summary="Read one job state",
    )
    async def get_job(job_id: str) -> JobResponse:
        try:
            return job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            ) from exc

    @app.get(
        "/api/jobs/{job_id}/events",
        response_class=StreamingResponse,
        summary="Stream ordered job progress using server-sent events",
    )
    async def job_events(
        request: Request,
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            initial = job_manager.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            ) from exc
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Last-Event-ID must be an integer.",
                ) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor, initial
            while True:
                events = job_manager.events_after(job_id, cursor)
                for event in events:
                    cursor = event.sequence
                    yield _event_payload(event)
                initial = job_manager.snapshot(job_id)
                if initial.status.terminal and not job_manager.events_after(
                    job_id,
                    cursor,
                ):
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/api/jobs/{job_id}/report",
        response_class=FileResponse,
        summary="Download the completed JSON report",
    )
    async def report(job_id: str) -> FileResponse:
        try:
            path = job_manager.report_path(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Job report is not available.",
            ) from exc
        return FileResponse(path, media_type="application/json")

    @app.get(
        "/api/jobs/{job_id}/artifacts/{artifact_path:path}",
        response_class=FileResponse,
        summary="Read a completed report artifact",
    )
    async def artifact(job_id: str, artifact_path: str) -> FileResponse:
        try:
            path = job_manager.resolve_artifact(job_id, artifact_path)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Artifact not found.",
            ) from exc
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(
            path,
            media_type=media_type or "application/octet-stream",
        )

    @app.delete(
        "/api/jobs/{job_id}",
        response_model=JobResponse | None,
        summary="Cancel an active job or delete a terminal job",
    )
    async def delete_job(job_id: str) -> JobResponse | Response:
        try:
            result = job_manager.delete_or_cancel(job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            ) from exc
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return result

    static_directory = Path(__file__).with_name("static")
    if (static_directory / "index.html").is_file():
        app.mount(
            "/",
            StaticFiles(directory=static_directory, html=True),
            name="dashboard",
        )

    return app
