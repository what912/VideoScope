# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).resolve().parents[1]
entry = root / "src" / "videoscope" / "windows" / "launcher.py"

datas = []
datas += collect_data_files("videoscope.web", includes=["static/**"])
datas += collect_data_files("videoscope.reporting", includes=["templates/**"])
datas += copy_metadata("scenedetect-headless")
datas += copy_metadata("opencv-python-headless")

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
