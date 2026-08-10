"""Tkinter launcher for the frozen Windows local connector."""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Protocol

from videoscope import __version__
from videoscope.windows.ffmpeg_locator import (
    FFmpegStatus,
    detect_ffmpeg,
    find_winget,
    install_ffmpeg_with_winget,
    process_environment_with_tools,
)
from videoscope.windows.single_instance import (
    ShutdownSignal,
    SingleInstanceLease,
    request_existing_shutdown,
    wait_for_instance_exit,
)

CONNECTOR_HOST = "127.0.0.1"
CONNECTOR_PORT = 8765
PUBLIC_SITE_ORIGIN = "https://what912.github.io"
PUBLIC_CONNECT_URL = "https://what912.github.io/VideoScope/connect"
FFMPEG_HELP_URL = "https://ffmpeg.org/download.html"


class ServerController(Protocol):
    pairing_code: str

    @property
    def started(self) -> bool: ...

    def run(self) -> None: ...

    def request_shutdown(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LauncherArguments:
    """Validated command-line mode used by installer smoke and uninstall."""

    headless: bool
    shutdown: bool
    port: int
    protocol_url: str | None


@dataclass(frozen=True, slots=True)
class ConnectorServerParameters:
    """Explicit local-only settings passed to the optional Web server."""

    host: str
    port: int
    job_directory: None
    max_upload_bytes: int
    cpu_concurrency: int
    heavy_ai_concurrency: int
    job_ttl_seconds: float
    allow_network: bool
    public_site_origin: str
    pairing_code: str
    access_log: bool


ServerFactory = Callable[[ConnectorServerParameters], ServerController]


def parse_arguments(arguments: Sequence[str] | None = None) -> LauncherArguments:
    parser = argparse.ArgumentParser(description="VideoScope Local Connector")
    parser.add_argument(
        "protocol_url",
        nargs="?",
        choices=("videoscope://start",),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--shutdown", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--port",
        type=int,
        default=CONNECTOR_PORT,
        help=argparse.SUPPRESS,
    )
    parsed = parser.parse_args(arguments)
    if not 1 <= parsed.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return LauncherArguments(
        headless=bool(parsed.headless),
        shutdown=bool(parsed.shutdown),
        port=int(parsed.port),
        protocol_url=parsed.protocol_url,
    )


def should_open_public_site(
    *, server_started: bool, already_opened: bool, closing: bool
) -> bool:
    """Gate browser launch on a confirmed-ready loopback server."""
    return server_started and not already_opened and not closing


def connector_server_parameters(
    *, port: int, pairing_code: str
) -> ConnectorServerParameters:
    """Keep the frozen launcher on the same explicit local-only server policy."""
    return ConnectorServerParameters(
        host=CONNECTOR_HOST,
        port=port,
        job_directory=None,
        max_upload_bytes=1024 * 1024 * 1024,
        cpu_concurrency=2,
        heavy_ai_concurrency=1,
        job_ttl_seconds=24 * 60 * 60,
        allow_network=False,
        public_site_origin=PUBLIC_SITE_ORIGIN,
        pairing_code=pairing_code,
        access_log=False,
    )


def _server_factory(parameters: ConnectorServerParameters) -> ServerController:
    from videoscope.web.server import create_server_controller

    return create_server_controller(
        host=parameters.host,
        port=parameters.port,
        job_directory=parameters.job_directory,
        max_upload_bytes=parameters.max_upload_bytes,
        cpu_concurrency=parameters.cpu_concurrency,
        heavy_ai_concurrency=parameters.heavy_ai_concurrency,
        job_ttl_seconds=parameters.job_ttl_seconds,
        allow_network=parameters.allow_network,
        public_site_origin=parameters.public_site_origin,
        pairing_code=parameters.pairing_code,
        access_log=parameters.access_log,
    )


def _activate_external_tools(status: FFmpegStatus) -> None:
    if status.tools is None:
        return
    updated = process_environment_with_tools(status.tools)
    os.environ["PATH"] = updated["PATH"]


def _start_controller(
    *,
    port: int,
    factory: ServerFactory,
) -> ServerController:
    pairing_code = token_urlsafe(9)
    return factory(connector_server_parameters(port=port, pairing_code=pairing_code))


def run_headless(
    *,
    port: int,
    factory: ServerFactory = _server_factory,
    poll_seconds: float = 0.1,
) -> int:
    """Run without Tk only for deterministic installer smoke testing."""
    status = detect_ffmpeg()
    _activate_external_tools(status)
    lease = SingleInstanceLease()
    if not lease.acquire():
        return 2
    shutdown_signal = ShutdownSignal()
    controller = _start_controller(port=port, factory=factory)
    finished = threading.Event()

    def monitor_shutdown() -> None:
        while not finished.wait(poll_seconds):
            if shutdown_signal.requested:
                controller.request_shutdown()
                return

    monitor = threading.Thread(
        target=monitor_shutdown,
        name="videoscope-shutdown-monitor",
        daemon=True,
    )
    monitor.start()
    try:
        controller.run()
    finally:
        finished.set()
        monitor.join(timeout=1.0)
        shutdown_signal.close()
        lease.close()
    return 0


class ConnectorWindow:
    """Small visible controller; it never persists pairing codes or API keys."""

    def __init__(self, *, port: int, factory: ServerFactory = _server_factory) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self._ttk = ttk
        self._port = port
        self._factory = factory
        self._lease = SingleInstanceLease()
        self._shutdown_signal: ShutdownSignal | None = None
        self._controller: ServerController | None = None
        self._server_thread: threading.Thread | None = None
        self._server_error: Exception | None = None
        self._closing = False
        self._public_site_opened = False
        self._ffmpeg_status = detect_ffmpeg()
        _activate_external_tools(self._ffmpeg_status)

        self.root = tk.Tk()
        self.root.title(f"VideoScope 本地连接器 {__version__}")
        self.root.geometry("680x470")
        self.root.minsize(620, 430)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._connection_text = tk.StringVar(value="正在启动本地连接器…")
        self._ffmpeg_text = tk.StringVar(value=self._ffmpeg_status.message)
        self._pairing_text = tk.StringVar(value="正在生成…")
        self._build()

    def _build(self) -> None:
        ttk = self._ttk
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=28)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text="VideoScope 本地连接器",
            font=("Segoe UI", 20, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="视频留在这台电脑上处理。关闭此窗口会停止连接器。",
        ).grid(row=1, column=0, sticky="w", pady=(6, 22))

        ttk.Label(frame, textvariable=self._connection_text).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Label(frame, text="浏览器配对码", font=("Segoe UI", 10, "bold")).grid(
            row=3, column=0, sticky="w", pady=(24, 4)
        )
        code_row = ttk.Frame(frame)
        code_row.grid(row=4, column=0, sticky="ew")
        code_row.columnconfigure(0, weight=1)
        ttk.Entry(
            code_row,
            textvariable=self._pairing_text,
            state="readonly",
            font=("Consolas", 16, "bold"),
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(code_row, text="复制", command=self.copy_pairing_code).grid(
            row=0, column=1, padx=(10, 0)
        )

        ttk.Separator(frame).grid(row=5, column=0, sticky="ew", pady=22)
        ttk.Label(frame, text="媒体工具", font=("Segoe UI", 10, "bold")).grid(
            row=6, column=0, sticky="w"
        )
        ttk.Label(frame, textvariable=self._ffmpeg_text, wraplength=600).grid(
            row=7, column=0, sticky="w", pady=(4, 10)
        )
        tools_row = ttk.Frame(frame)
        tools_row.grid(row=8, column=0, sticky="w")
        self._winget_button = ttk.Button(
            tools_row,
            text="使用 Winget 安装 FFmpeg",
            command=self.offer_winget_install,
        )
        self._winget_button.grid(row=0, column=0)
        ttk.Button(
            tools_row,
            text="手动安装说明",
            command=lambda: webbrowser.open(FFMPEG_HELP_URL),
        ).grid(row=0, column=1, padx=(10, 0))
        if self._ffmpeg_status.ready or find_winget() is None:
            self._winget_button.state(["disabled"])

        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, sticky="ew", pady=(28, 0))
        ttk.Button(
            actions,
            text="打开 VideoScope 网站",
            command=self.open_public_site,
        ).grid(row=0, column=0)
        ttk.Button(actions, text="停止并退出", command=self.close).grid(
            row=0, column=1, padx=(10, 0)
        )

    def start(self) -> int:
        from tkinter import messagebox

        if not self._lease.acquire():
            messagebox.showinfo("VideoScope", "VideoScope 本地连接器已经在运行。")
            webbrowser.open(PUBLIC_CONNECT_URL)
            self.root.destroy()
            return 0
        self._shutdown_signal = ShutdownSignal()
        self._controller = _start_controller(port=self._port, factory=self._factory)
        self._pairing_text.set(self._controller.pairing_code)

        def run_server() -> None:
            try:
                assert self._controller is not None
                self._controller.run()
            except Exception as exc:
                self._server_error = exc

        self._server_thread = threading.Thread(
            target=run_server,
            name="videoscope-local-server",
            daemon=True,
        )
        self._server_thread.start()
        self.root.after(100, self._poll_server)
        self.root.mainloop()
        return 0

    def _poll_server(self) -> None:
        if self._closing:
            return
        if self._shutdown_signal is not None and self._shutdown_signal.requested:
            self.close()
            return
        controller = self._controller
        thread = self._server_thread
        if controller is not None and controller.started:
            self._connection_text.set(
                f"已在 http://{CONNECTOR_HOST}:{self._port} 安全运行"
            )
            if should_open_public_site(
                server_started=controller.started,
                already_opened=self._public_site_opened,
                closing=self._closing,
            ):
                self.open_public_site()
        elif thread is not None and not thread.is_alive():
            detail = "端口可能已被其他程序占用。"
            if self._server_error is not None:
                detail = "连接器未能启动，请重新打开或检查端口 8765。"
            self._connection_text.set(detail)
        else:
            self._connection_text.set("正在启动本地连接器…")
        self.root.after(200, self._poll_server)

    def copy_pairing_code(self) -> None:
        code = self._pairing_text.get()
        if code and code != "正在生成…":
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.root.update_idletasks()

    def open_public_site(self) -> None:
        if not self._closing:
            self._public_site_opened = True
            webbrowser.open(PUBLIC_CONNECT_URL)

    def offer_winget_install(self) -> None:
        from tkinter import messagebox

        winget = find_winget()
        if winget is None:
            messagebox.showwarning(
                "未找到 Winget",
                "请使用手动安装说明安装 FFmpeg 和 ffprobe。",
            )
            return
        approved = messagebox.askyesno(
            "安装独立媒体工具",
            "将使用 Windows Package Manager 安装 Gyan.FFmpeg。\n\n"
            "它是独立第三方系统依赖，不会被复制进 VideoScope。是否继续？",
        )
        if not approved:
            return
        self._winget_button.state(["disabled"])
        self._ffmpeg_text.set("Winget 正在安装 FFmpeg，请稍候…")

        def install() -> None:
            result = install_ffmpeg_with_winget(winget)
            status = detect_ffmpeg() if result.succeeded else None

            def finish() -> None:
                if self._closing:
                    return
                if status is not None and status.ready:
                    self._ffmpeg_status = status
                    _activate_external_tools(status)
                    self._ffmpeg_text.set(status.message)
                else:
                    self._ffmpeg_text.set(result.message)
                    self._winget_button.state(["!disabled"])

            self.root.after(0, finish)

        threading.Thread(
            target=install,
            name="videoscope-winget-install",
            daemon=True,
        ).start()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._controller is not None:
            self._controller.request_shutdown()
        if self._server_thread is not None:
            self._server_thread.join(timeout=10.0)
        if self._shutdown_signal is not None:
            self._shutdown_signal.close()
        self._lease.close()
        self.root.destroy()


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = parse_arguments(arguments)
    if parsed.shutdown:
        request_existing_shutdown()
        wait_for_instance_exit(timeout_seconds=15.0)
        return 0
    if parsed.headless:
        return run_headless(port=parsed.port)
    return ConnectorWindow(port=parsed.port).start()


if __name__ == "__main__":
    raise SystemExit(main())
