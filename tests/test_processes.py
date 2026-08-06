"""Descriptor inheritance rules for immutable Rescue source snapshots."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from videoscope import processes


def _regular_identity() -> SimpleNamespace:
    return SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_dev=7, st_ino=11)


def test_darwin_pinned_argument_passes_only_exact_snapshot_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        processes,
        "_fstat_descriptor",
        lambda descriptor: _regular_identity(),
        raising=False,
    )
    monkeypatch.setattr(
        processes,
        "_stat_descriptor_path",
        lambda path: _regular_identity(),
        raising=False,
    )
    monkeypatch.setattr(
        processes, "_descriptor_is_inheritable", lambda descriptor: False, raising=False
    )

    options: dict[str, object] = processes.pinned_subprocess_options(
        ["ffmpeg", "-i", "/dev/fd/41", "output.mp4"]
    )

    assert options == {"close_fds": True, "pass_fds": (41,)}


def test_pinned_argument_rejects_descriptor_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        processes,
        "_fstat_descriptor",
        lambda descriptor: _regular_identity(),
        raising=False,
    )
    monkeypatch.setattr(
        processes,
        "_stat_descriptor_path",
        lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_dev=7, st_ino=99),
        raising=False,
    )
    monkeypatch.setattr(
        processes, "_descriptor_is_inheritable", lambda descriptor: False, raising=False
    )

    with pytest.raises(processes.PinnedDescriptorError, match="identity"):
        processes.pinned_subprocess_options(["ffprobe", "/dev/fd/41"])


def test_pinned_argument_rejects_inheritable_parent_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        processes,
        "_fstat_descriptor",
        lambda descriptor: _regular_identity(),
        raising=False,
    )
    monkeypatch.setattr(
        processes,
        "_stat_descriptor_path",
        lambda path: _regular_identity(),
        raising=False,
    )
    monkeypatch.setattr(
        processes, "_descriptor_is_inheritable", lambda descriptor: True, raising=False
    )

    with pytest.raises(processes.PinnedDescriptorError, match="non-inheritable"):
        processes.pinned_subprocess_options(["ffprobe", "/dev/fd/41"])


def test_unsupported_posix_pinned_argument_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "freebsd14")

    with pytest.raises(processes.PinnedDescriptorError, match="unavailable"):
        processes.pinned_subprocess_options(["ffprobe", "/dev/fd/41"])


def test_ordinary_arguments_do_not_inherit_descriptors() -> None:
    options: dict[str, object] = processes.pinned_subprocess_options(
        ["ffprobe", str(Path("ordinary.mp4"))]
    )
    assert options == {}


def test_ordinary_proc_path_is_not_treated_as_a_pinned_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    options = processes.pinned_subprocess_options(["tool", "/proc/cpuinfo"])
    assert options == {}


def test_linux_child_accepts_only_current_process_descriptor_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getpid", lambda: 700)
    monkeypatch.setattr(
        processes, "_fstat_descriptor", lambda descriptor: _regular_identity()
    )
    monkeypatch.setattr(
        processes, "_stat_descriptor_path", lambda path: _regular_identity()
    )
    monkeypatch.setattr(
        processes, "_descriptor_is_inheritable", lambda descriptor: False
    )

    assert processes.pinned_subprocess_options(["ffprobe", "/proc/700/fd/41"]) == {
        "close_fds": True,
        "pass_fds": (41,),
    }
    with pytest.raises(processes.PinnedDescriptorError, match="invalid"):
        processes.pinned_subprocess_options(["ffprobe", "/proc/701/fd/41"])


def test_child_rejects_multiple_distinct_pinned_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        processes,
        "_fstat_descriptor",
        lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o400, st_dev=7, st_ino=descriptor
        ),
    )
    monkeypatch.setattr(
        processes,
        "_stat_descriptor_path",
        lambda path: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o400,
            st_dev=7,
            st_ino=int(Path(path).name),
        ),
    )
    monkeypatch.setattr(
        processes, "_descriptor_is_inheritable", lambda descriptor: False
    )

    with pytest.raises(processes.PinnedDescriptorError, match="multiple"):
        processes.pinned_subprocess_options(
            ["ffmpeg", "-i", "/dev/fd/41", "-i", "/dev/fd/42"]
        )
