"""Optional Uvicorn launcher kept outside the base import path."""

from __future__ import annotations

from pathlib import Path
from secrets import token_urlsafe

from videoscope.web.app import create_app
from videoscope.web.models import WebServerConfig


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
) -> None:
    """Run one local Uvicorn process with explicit resource policy."""
    import uvicorn

    data: dict[str, object] = {
        "max_upload_bytes": max_upload_bytes,
        "cpu_concurrency": cpu_concurrency,
        "heavy_ai_concurrency": heavy_ai_concurrency,
        "job_ttl_seconds": job_ttl_seconds,
        "allow_non_loopback_origin": allow_network,
        "trusted_hosts": ("*",) if allow_network else WebServerConfig().trusted_hosts,
        "allowed_browser_origins": (public_site_origin,),
        "connector_pairing_code": token_urlsafe(9),
    }
    if job_directory is not None:
        data["job_root"] = job_directory
    config = WebServerConfig.model_validate(data)
    print("VideoScope Local Connector pairing code:")
    print(config.connector_pairing_code)
    print(f"Allowed public site origin: {public_site_origin}")
    print("API keys entered in the local dashboard remain in memory only.")
    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )
