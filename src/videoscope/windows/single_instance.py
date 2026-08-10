"""Per-user Windows mutex and shutdown event for the connector launcher."""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from typing import Protocol

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0

DEFAULT_MUTEX_NAME = r"Local\VideoScopeConnector-v1"
DEFAULT_SHUTDOWN_EVENT_NAME = r"Local\VideoScopeConnectorShutdown-v1"


class KernelApi(Protocol):
    """Small injectable Windows kernel boundary."""

    def create_mutex(self, name: str) -> tuple[int, bool]: ...

    def create_event(self, name: str) -> int: ...

    def open_event(self, name: str) -> int | None: ...

    def event_is_set(self, handle: int) -> bool: ...

    def set_event(self, handle: int) -> bool: ...

    def close(self, handle: int) -> None: ...


class WindowsKernel:
    """ctypes wrapper loaded only by the Windows launcher."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("VideoScope Windows launcher requires Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel

    def create_mutex(self, name: str) -> tuple[int, bool]:
        ctypes.set_last_error(0)
        handle = self._kernel.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle), ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def create_event(self, name: str) -> int:
        handle = self._kernel.CreateEventW(None, True, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def open_event(self, name: str) -> int | None:
        handle = self._kernel.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False, name)
        return int(handle) if handle else None

    def event_is_set(self, handle: int) -> bool:
        return int(self._kernel.WaitForSingleObject(handle, 0)) == WAIT_OBJECT_0

    def set_event(self, handle: int) -> bool:
        return bool(self._kernel.SetEvent(handle))

    def close(self, handle: int) -> None:
        self._kernel.CloseHandle(handle)


class SingleInstanceLease:
    """Keep the named mutex handle alive for one launcher lifetime."""

    def __init__(
        self,
        *,
        name: str = DEFAULT_MUTEX_NAME,
        kernel: KernelApi | None = None,
    ) -> None:
        self._name = name
        self._kernel = kernel or WindowsKernel()
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle, already_exists = self._kernel.create_mutex(self._name)
        if already_exists:
            self._kernel.close(handle)
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.close(self._handle)
            self._handle = None

    def __enter__(self) -> SingleInstanceLease:
        if not self.acquire():
            raise RuntimeError("VideoScope connector is already running")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ShutdownSignal:
    """Primary-process event that supports a controlled uninstall shutdown."""

    def __init__(
        self,
        *,
        name: str = DEFAULT_SHUTDOWN_EVENT_NAME,
        kernel: KernelApi | None = None,
    ) -> None:
        self._kernel = kernel or WindowsKernel()
        self._handle = self._kernel.create_event(name)

    @property
    def requested(self) -> bool:
        return self._kernel.event_is_set(self._handle)

    def close(self) -> None:
        if self._handle:
            self._kernel.close(self._handle)
            self._handle = 0


def request_existing_shutdown(
    *,
    event_name: str = DEFAULT_SHUTDOWN_EVENT_NAME,
    kernel: KernelApi | None = None,
) -> bool:
    """Signal the primary process without opening a network control endpoint."""
    effective_kernel = kernel or WindowsKernel()
    handle = effective_kernel.open_event(event_name)
    if handle is None:
        return False
    try:
        return effective_kernel.set_event(handle)
    finally:
        effective_kernel.close(handle)


def wait_for_instance_exit(
    *,
    timeout_seconds: float,
    mutex_name: str = DEFAULT_MUTEX_NAME,
    kernel: KernelApi | None = None,
) -> bool:
    """Wait until the primary mutex is released, using bounded polling."""
    effective_kernel = kernel or WindowsKernel()
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        handle, already_exists = effective_kernel.create_mutex(mutex_name)
        effective_kernel.close(handle)
        if not already_exists:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


__all__ = [
    "DEFAULT_MUTEX_NAME",
    "DEFAULT_SHUTDOWN_EVENT_NAME",
    "KernelApi",
    "ShutdownSignal",
    "SingleInstanceLease",
    "WindowsKernel",
    "request_existing_shutdown",
    "wait_for_instance_exit",
]
