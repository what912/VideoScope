from __future__ import annotations

import sys

from pytest import MonkeyPatch

from videoscope.web.server import create_server_controller


def test_windowed_headless_controller_does_not_require_console_streams(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    controller = create_server_controller(
        host="127.0.0.1",
        port=49155,
        job_directory=None,
        max_upload_bytes=1024,
        cpu_concurrency=1,
        heavy_ai_concurrency=1,
        job_ttl_seconds=60,
        allow_network=False,
        public_site_origin="https://what912.github.io",
        access_log=False,
    )

    assert controller._server.config.log_config is None
