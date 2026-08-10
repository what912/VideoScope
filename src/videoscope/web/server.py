"""Optional Uvicorn launcher kept outside the base import path."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from videoscope.web.app import create_app
from videoscope.web.models import WebServerConfig


@dataclass(slots=True)
class LocalServerController:
    """Own one configured Uvicorn server and expose bounded lifecycle controls."""

    pairing_code: str
    host: str
    port: int
    _server: Any = field(repr=False)

    @property
    def started(self) -> bool:
        """Return whether Uvicorn has completed application startup."""
        return bool(getattr(self._server, "started", False))

    def run(self) -> None:
        """Run until a local shutdown is requested."""
        self._server.run()

    def request_shutdown(self) -> None:
        """Ask Uvicorn to finish active work and stop its event loop."""
        self._server.should_exit = True


def create_server_controller(
    *,
    host: str,
    port: int,
    job_directory: Path | None,
    max_upload_bytes: int,
    cpu_concurrency: int,
    heavy_ai_concurrency: int,
    job_ttl_seconds: float,
    allow_network: bool,
    public_site_origin: str,
    pairing_code: str | None = None,
    access_log: bool = True,
) -> LocalServerController:
    """Build a controllable local server without starting a background process."""
    import uvicorn

    effective_pairing_code = pairing_code or token_urlsafe(9)
    data: dict[str, object] = {
        "max_upload_bytes": max_upload_bytes,
        "cpu_concurrency": cpu_concurrency,
        "heavy_ai_concurrency": heavy_ai_concurrency,
        "job_ttl_seconds": job_ttl_seconds,
        "allow_non_loopback_origin": allow_network,
        "trusted_hosts": ("*",) if allow_network else WebServerConfig().trusted_hosts,
        "allowed_browser_origins": (public_site_origin,),
        "connector_pairing_code": effective_pairing_code,
    }
    if job_directory is not None:
        data["job_root"] = job_directory
    app_config = WebServerConfig.model_validate(data)
    uvicorn_config = uvicorn.Config(
        create_app(app_config),
        host=host,
        port=port,
        log_level="info",
        access_log=access_log,
        log_config=None if not access_log else uvicorn.config.LOGGING_CONFIG,
    )
    return LocalServerController(
        pairing_code=effective_pairing_code,
        host=host,
        port=port,
        _server=uvicorn.Server(uvicorn_config),
    )


def run_server(
    *,
    host: str,
    port: int,
    job_directory: Path | None,
    max_upload_bytes: int,
    cpu_concurrency: int,
    heavy_ai_concurrency: int,
    job_ttl_seconds: float,
    allow_network: bool,
    public_site_origin: str,
    pairing_code: str | None = None,
) -> None:
    """Run one local Uvicorn process with explicit resource policy."""
    controller = create_server_controller(
        host=host,
        port=port,
        job_directory=job_directory,
        max_upload_bytes=max_upload_bytes,
        cpu_concurrency=cpu_concurrency,
        heavy_ai_concurrency=heavy_ai_concurrency,
        job_ttl_seconds=job_ttl_seconds,
        allow_network=allow_network,
        public_site_origin=public_site_origin,
        pairing_code=pairing_code,
    )
    print("VideoScope Local Connector pairing code:")
    print(controller.pairing_code)
    print(f"Allowed public site origin: {public_site_origin}")
    print("API keys entered in the local dashboard remain in memory only.")
    controller.run()
