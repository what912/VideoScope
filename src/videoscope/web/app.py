"""FastAPI application factory for the local VideoScope API."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from videoscope import __version__
from videoscope.analysis import AnalysisConfig
from videoscope.detectors import create_optional_detector_registry
from videoscope.web.jobs import JobManager
from videoscope.web.models import (
    DetectorResponse,
    HealthResponse,
    JobEvent,
    JobResponse,
    WebServerConfig,
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


def _event_payload(event: JobEvent) -> str:
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


def create_app(
    config: WebServerConfig | None = None,
    *,
    manager: JobManager | None = None,
) -> FastAPI:
    """Create an app with no CORS middleware or external service dependency."""
    effective_config = config or WebServerConfig()
    job_manager = manager or JobManager(effective_config)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        del application
        job_manager.start_cleanup()
        try:
            yield
        finally:
            job_manager.shutdown()

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

    @app.middleware("http")
    async def reject_cross_site_browser_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        origin = request.headers.get("origin")
        if (
            origin is not None
            and not effective_config.allow_non_loopback_origin
            and not _is_loopback_origin(origin)
        ):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Cross-site browser origin is not allowed."},
            )
        return await call_next(request)

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
        return HealthResponse(active_jobs=job_manager.active_job_count())

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
