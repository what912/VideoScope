# -*- mode: python ; coding: utf-8 -*-

import re
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).resolve().parents[1]
entry = root / "src" / "videoscope" / "windows" / "launcher.py"
license_root = root / "packaging" / "windows"
runtime_lock = license_root / "requirements-runtime.lock"
lock_entry = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s#]+)"
    r"\s+#\s+SPDX-License-Identifier:\s+(?P<license>.+)$"
)
runtime_distributions = []
for line in runtime_lock.read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
        continue
    match = lock_entry.fullmatch(line)
    if match is None:
        raise ValueError(f"Invalid runtime lock entry: {line}")
    runtime_distributions.append(match.group("name"))

datas = []
datas += collect_data_files("videoscope.web", includes=["static/**"])
datas += collect_data_files("videoscope.reporting", includes=["templates/**"])
datas += [
    (str(root / "LICENSE"), "licenses"),
    (str(root / "NOTICE"), "licenses"),
    (str(root / "THIRD_PARTY_NOTICES.txt"), "licenses"),
    (str(root / "docs" / "third-party-licenses.md"), "licenses"),
    (str(runtime_lock), "licenses"),
    (str(license_root / "license-policy.json"), "licenses"),
]
for distribution in runtime_distributions:
    datas += copy_metadata(distribution)

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("multipart")

analysis = Analysis(
    [str(entry)],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "faster_whisper",
        "build",
        "mypy",
        "open_clip",
        "paddle",
        "paddleocr",
        "PyInstaller",
        "pytest",
        "_pytest",
        "ruff",
        "torch",
        "torchvision",
        "transformers",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="VideoScopeConnector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VideoScopeConnector",
)
