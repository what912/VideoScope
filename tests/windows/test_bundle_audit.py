from __future__ import annotations

from pathlib import Path

from scripts.audit_windows_bundle import REQUIRED_PATHS, audit_bundle

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _valid_bundle(root: Path) -> None:
    for relative in REQUIRED_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime\n", encoding="utf-8")


def test_bundle_audit_accepts_required_runtime_without_external_media(
    tmp_path: Path,
) -> None:
    _valid_bundle(tmp_path)

    assert audit_bundle(tmp_path) == ()


def test_bundle_audit_rejects_ffmpeg_models_and_secrets(tmp_path: Path) -> None:
    _valid_bundle(tmp_path)
    (tmp_path / "ffmpeg.exe").write_bytes(b"binary")
    (tmp_path / "model.onnx").write_bytes(b"weight")
    (tmp_path / "settings.txt").write_text(
        "sk-this-is-not-a-real-key-but-must-never-ship",
        encoding="utf-8",
    )

    violations = audit_bundle(tmp_path)

    assert any("ffmpeg.exe" in item for item in violations)
    assert any("model.onnx" in item for item in violations)
    assert any("embedded secret" in item for item in violations)


def test_bundle_audit_rejects_development_tooling(tmp_path: Path) -> None:
    _valid_bundle(tmp_path)
    development_module = tmp_path / "_internal" / "mypy" / "__init__.py"
    development_module.parent.mkdir(parents=True)
    development_module.write_text("", encoding="utf-8")

    violations = audit_bundle(tmp_path)

    assert any("development-only package" in item for item in violations)


def test_installer_registers_user_scoped_start_protocol() -> None:
    installer = (
        REPOSITORY_ROOT / "packaging" / "windows" / "VideoScope.iss"
    ).read_text(encoding="utf-8")

    assert "[Registry]" in installer
    assert 'Root: HKCU; Subkey: "Software\\Classes\\videoscope"' in installer
    assert 'ValueName: "URL Protocol"' in installer
    assert '"%1"' in installer
    assert 'RunOnceId: "StopConnector"' in installer


def test_installer_uses_self_contained_bilingual_messages() -> None:
    installer = (
        REPOSITORY_ROOT / "packaging" / "windows" / "VideoScope.iss"
    ).read_text(encoding="utf-8")

    assert "ChineseSimplified.isl" not in installer
    assert "[Messages]" in installer
    assert "欢迎安装" in installer
    assert "Welcome" in installer


def test_build_script_discovers_user_scoped_inno_setup() -> None:
    build_script = (
        REPOSITORY_ROOT / "scripts" / "build_windows_installer.ps1"
    ).read_text(encoding="utf-8")

    assert "LOCALAPPDATA" in build_script
    assert "Programs\\Inno Setup 6\\ISCC.exe" in build_script
