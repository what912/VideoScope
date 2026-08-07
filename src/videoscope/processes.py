"""Platform-safe helpers for descriptor-pinned local subprocess inputs."""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class PinnedDescriptorError(RuntimeError):
    """A retained input descriptor cannot be exposed safely on this platform."""


_LINUX_DESCRIPTOR_PATH = re.compile(r"^/proc/([0-9]+)/fd/([0-9]+)$")
_DARWIN_DESCRIPTOR_PATH = re.compile(r"^/dev/fd/([0-9]+)$")


def _os_name() -> str:
    return os.name


def _system_platform() -> str:
    return sys.platform


def _fstat_descriptor(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def _stat_descriptor_path(path: Path) -> os.stat_result:
    return os.stat(path)


def _descriptor_is_inheritable(descriptor: int) -> bool:
    return os.get_inheritable(descriptor)


def _validate_descriptor_path(path: Path, descriptor: int) -> None:
    try:
        retained = _fstat_descriptor(descriptor)
        exposed = _stat_descriptor_path(path)
    except OSError as exc:
        raise PinnedDescriptorError("Pinned descriptor path is unavailable") from exc
    if not stat.S_ISREG(retained.st_mode) or not stat.S_ISREG(exposed.st_mode):
        raise PinnedDescriptorError("Pinned descriptor is not a regular file")
    if (retained.st_dev, retained.st_ino) != (exposed.st_dev, exposed.st_ino):
        raise PinnedDescriptorError("Pinned descriptor path identity does not match")
    if _descriptor_is_inheritable(descriptor):
        raise PinnedDescriptorError("Pinned parent descriptor must be non-inheritable")


def pinned_descriptor_path(descriptor: int) -> Path:
    """Return the current-process path for one retained POSIX descriptor."""
    if _os_name() != "posix":
        raise PinnedDescriptorError("Pinned descriptor paths require POSIX")
    path: Path
    if _system_platform().startswith("linux"):
        path = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
    elif _system_platform() == "darwin":
        path = Path(f"/dev/fd/{descriptor}")
    else:
        raise PinnedDescriptorError(
            "Pinned descriptor paths are unavailable on this POSIX platform"
        )
    _validate_descriptor_path(path, descriptor)
    return path


def _descriptor_from_argument(argument: str) -> tuple[Path, int] | None:
    is_dev_fd = argument.startswith("/dev/fd/")
    is_proc_fd = re.match(r"^/proc/[0-9]+/fd/", argument) is not None
    if not is_dev_fd and not is_proc_fd:
        return None
    if _os_name() != "posix":
        raise PinnedDescriptorError("Pinned descriptor arguments require POSIX")
    if _system_platform().startswith("linux"):
        match = _LINUX_DESCRIPTOR_PATH.fullmatch(argument)
        if match is None or int(match.group(1)) != os.getpid():
            raise PinnedDescriptorError("Pinned descriptor path is invalid")
        return Path(argument), int(match.group(2))
    if _system_platform() == "darwin":
        match = _DARWIN_DESCRIPTOR_PATH.fullmatch(argument)
        if match is None:
            raise PinnedDescriptorError("Pinned descriptor path is invalid")
        return Path(argument), int(match.group(1))
    raise PinnedDescriptorError(
        "Pinned descriptor paths are unavailable on this POSIX platform"
    )


def pinned_subprocess_options(arguments: Sequence[str]) -> dict[str, Any]:
    """Return the minimal descriptor inheritance options for one child spawn."""
    descriptors: set[int] = set()
    for argument in arguments:
        pinned = _descriptor_from_argument(argument)
        if pinned is None:
            continue
        path, descriptor = pinned
        _validate_descriptor_path(path, descriptor)
        descriptors.add(descriptor)
        if len(descriptors) > 1:
            raise PinnedDescriptorError(
                "A media child cannot receive multiple pinned descriptors"
            )
    if not descriptors:
        return {}
    return {"close_fds": True, "pass_fds": tuple(sorted(descriptors))}


def secure_read_open(path: Path) -> int:
    """Open and retain one regular input without following a replaceable link."""
    candidate = Path(path)
    existing = _descriptor_from_argument(str(candidate))
    if existing is not None:
        descriptor = os.dup(existing[1])
    elif _os_name() != "nt":
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise PinnedDescriptorError("No-follow file opens are unavailable")
        try:
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | nofollow,
            )
        except OSError as exc:
            raise PinnedDescriptorError("Input could not be opened safely") from exc
    else:
        descriptor = _secure_windows_read_open(candidate)
    try:
        os.set_inheritable(descriptor, False)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PinnedDescriptorError("Pinned input is not a regular file")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def hash_descriptor(descriptor: int) -> str:
    """Hash the exact retained bytes without reopening their original pathname."""
    from hashlib import sha256

    hasher = sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while block := os.read(descriptor, 1024 * 1024):
            hasher.update(block)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise PinnedDescriptorError("Pinned input could not be read") from exc
    return hasher.hexdigest()


def _secure_windows_read_open(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny writes, deletion, and replacement
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise PinnedDescriptorError("Input could not be opened safely")
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        kernel32.CloseHandle(handle)
        raise PinnedDescriptorError("Input could not be inspected safely")
    if information.dwFileAttributes & 0x00000400:
        kernel32.CloseHandle(handle)
        raise PinnedDescriptorError("Input reparse points are not allowed")
    open_osfhandle = getattr(msvcrt, "open_osfhandle")
    return int(open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)))


__all__ = [
    "PinnedDescriptorError",
    "hash_descriptor",
    "pinned_descriptor_path",
    "pinned_subprocess_options",
    "secure_read_open",
]
