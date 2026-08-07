"""Tests for release archive content rules."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import audit_distribution

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def make_wheel(path: Path, members: dict[str, str]) -> None:
    """Create a minimal ZIP-shaped wheel fixture."""
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_clean_wheel_passes_audit(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "videoscope/__init__.py": '__version__ = "0.1.0"\n',
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
            "videoscope/privacy/pipeline.py": "# Safe Sharing runtime\n",
            "videoscope/privacy/verification.py": "# privacy verification\n",
            "videoscope/rescue/models.py": "# Rescue models\n",
            "videoscope/rescue/pipeline.py": "# Rescue pipeline\n",
            "videoscope/rescue/verification.py": "# Rescue verification\n",
            "videoscope/content/models.py": "# Content models\n",
            "videoscope/content/pipeline.py": "# Content pipeline\n",
            "videoscope/content/verification.py": "# Content verification\n",
            "videoscope/intelligence/models.py": "# Advanced AI models\n",
            "videoscope/intelligence/pipeline.py": "# Advanced AI pipeline\n",
            "videoscope/intelligence/providers/ollama.py": "# Ollama provider\n",
            "videoscope/reporting/templates/rescue_report.html.j2": "<html></html>",
            "videoscope/reporting/templates/content_report.html.j2": "<html></html>",
            "videoscope/web/static/index.html": (
                '<link rel="stylesheet" href="/assets/index-a1.css">'
                '<script type="module" src="/assets/index-b2.js"></script>'
            ),
            "videoscope/web/static/assets/index-a1.css": "body {}",
            "videoscope/web/static/assets/index-b2.js": "export {};",
        },
    )

    assert audit_distribution.audit_archive(wheel) == ()


def test_sdist_requires_safe_sharing_docs_and_examples(tmp_path: Path) -> None:
    """A source archive without the public Safe Sharing contract is incomplete."""
    source = tmp_path / "genvideoscope-0.4.0.dev0.tar.gz"
    payload = b"# VideoScope\n"
    info = tarfile.TarInfo("genvideoscope-0.4.0.dev0/README.md")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))

    violations = audit_distribution.audit_archive(source)

    assert any("docs/safe-sharing.md" in item for item in violations)
    assert any("examples/safe_sharing.ps1" in item for item in violations)
    assert any("examples/privacy-review.example.json" in item for item in violations)


def test_wheel_requires_video_rescue_runtime_and_report_template(
    tmp_path: Path,
) -> None:
    """A wheel without the public Rescue pipeline cannot satisfy the release gate."""
    wheel = tmp_path / "genvideoscope-0.5.0.dev0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "videoscope/privacy/pipeline.py": "# Safe Sharing runtime\n",
            "videoscope/privacy/verification.py": "# privacy verification\n",
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
            "videoscope/web/static/index.html": (
                '<link rel="stylesheet" href="/assets/index-a1.css">'
                '<script type="module" src="/assets/index-b2.js"></script>'
            ),
            "videoscope/web/static/assets/index-a1.css": "body {}",
            "videoscope/web/static/assets/index-b2.js": "export {};",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("videoscope/rescue/pipeline.py" in item for item in violations)
    assert any("videoscope/rescue/verification.py" in item for item in violations)
    assert any("rescue_report.html.j2" in item for item in violations)


def test_sdist_requires_video_rescue_contract_guide_and_examples(
    tmp_path: Path,
) -> None:
    """The source archive must carry the complete public Rescue workflow."""
    source = tmp_path / "genvideoscope-0.5.0.dev0.tar.gz"
    with tarfile.open(source, mode="w:gz") as archive:
        for required in (
            "README.md",
            "docs/privacy-api.md",
            "docs/privacy-schema.md",
            "docs/safe-sharing.md",
            "examples/privacy-review.example.json",
            "examples/safe_sharing.ps1",
            "examples/safe_sharing.sh",
        ):
            payload = b"release asset\n"
            info = tarfile.TarInfo(f"genvideoscope-0.5.0.dev0/{required}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    violations = audit_distribution.audit_archive(source)

    for required in (
        "docs/rescue-schema.md",
        "docs/video-rescue-guide.md",
        "examples/rescue-config.example.json",
        "examples/video_rescue.ps1",
        "examples/video_rescue.sh",
    ):
        assert any(required in item for item in violations)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "private_path",
    [
        r"C:\Users\Alice\private\clip.mp4",
        r"C:\Users\示例用户\private\clip.mp4",
        r"C:\Users\John Doe\private\clip.mp4",
        "/Users/alice/private/clip.mp4",
        "/home/alice/private/clip.mp4",
        "/home/john doe/private/clip.mp4",
        "/root/private/clip.mp4",
    ],
)
def test_sdist_rejects_personal_path_in_public_documentation(
    tmp_path: Path,
    private_path: str,
) -> None:
    """Public source documentation must not retain a developer home path."""
    source = tmp_path / "genvideoscope-0.4.0.dev0.tar.gz"
    with tarfile.open(source, mode="w:gz") as archive:
        for required in sorted(audit_distribution.REQUIRED_SDIST_MEMBERS):
            payload = (
                f"See {private_path}\n".encode()
                if required == "README.md"
                else b"release asset\n"
            )
            info = tarfile.TarInfo(f"genvideoscope-0.4.0.dev0/{required}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    violations = audit_distribution.audit_archive(source)

    assert any("personal absolute path" in item for item in violations)


def test_manifest_packages_safe_sharing_json_example() -> None:
    """The source archive rule must include the review payload example."""
    manifest = (REPOSITORY_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "recursive-include examples *.py *.ps1 *.sh *.yaml *.json" in manifest


def test_generated_video_and_run_output_are_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "tests/fixtures/generated/sample.mp4": "not a real video",
            "runs/demo/report.json": "{}",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("generated synthetic fixture" in item for item in violations)
    assert any("local analysis output" in item for item in violations)


def test_private_and_pending_safe_sharing_artifacts_are_rejected(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "genvideoscope-0.4.0.dev0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "privacy-review-private/evidence/raw.png": "private",
            "share-package/share-safe.mp4": "not a real video",
            "pending-package-deadbeef/changes.json": "{}",
            "staging/privacy-preview.mp4": "not a real video",
            "unredacted-evidence/frame.png": "private",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    for marker in (
        "private Safe Sharing artifact",
        "public Safe Sharing output",
        "pending or staging output",
        "unredacted private evidence",
    ):
        assert any(marker in item for item in violations)


def test_private_and_generated_rescue_artifacts_are_rejected(tmp_path: Path) -> None:
    """Private review, output, workspace, and corruption staging never ship."""
    wheel = tmp_path / "genvideoscope-0.5.0.dev0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "rescue-review-private/previews/source-preview.mp4": "private",
            "rescue-output/faithful-rescue.mp4": "generated",
            "rescue-workspace/staging/candidate.mp4": "workspace",
            "tests/fixtures/generated/.rescue_middle_damaged.tmp.mp4": "partial",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    for marker in (
        "private Video Rescue artifact",
        "public Video Rescue output",
        "Video Rescue workspace",
        "fixture corruption intermediate",
    ):
        assert any(marker in item for item in violations)


def test_missing_report_template_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(wheel, {"videoscope/__init__.py": ""})

    violations = audit_distribution.audit_archive(wheel)

    assert any("required runtime asset is missing" in item for item in violations)


def test_missing_dashboard_asset_referenced_by_index_is_rejected(
    tmp_path: Path,
) -> None:
    """A stale wheel index must not point at an absent hashed bundle."""
    wheel = tmp_path / "videoscope-0.3.0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
            "videoscope/web/static/index.html": (
                '<link rel="stylesheet" href="/assets/index-a1.css">'
                '<script type="module" src="/assets/index-missing.js"></script>'
            ),
            "videoscope/web/static/assets/index-a1.css": "body {}",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("index-missing.js" in item and "missing" in item for item in violations)


def test_ci_static_asset_gate_includes_untracked_generated_files() -> None:
    """A newly hashed bundle must make both build gates fail until committed."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    porcelain_gate = (
        "git status --porcelain --untracked-files=all -- src/videoscope/web/static"
    )

    assert workflow.count(porcelain_gate) == 2
    assert "git diff --exit-code -- src/videoscope/web/static" not in workflow


def test_ci_gates_public_site_and_bounds_windows_ffmpeg_install() -> None:
    """Release CI must verify the public product and fail closed on missing tools."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "  public-site:\n" in workflow
    assert "npm audit --audit-level=high" in workflow
    assert "npm run media:prepare" in workflow
    assert "npm run media:verify" in workflow
    assert "      - public-site\n" in workflow
    assert "foreach ($attempt in 1..3)" in workflow
    assert "Get-Command ffmpeg" in workflow
    assert "Get-Command ffprobe" in workflow


def test_personal_absolute_path_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    personal_path = "C:" + "\\Users\\" + "Example\\private.mp4"
    make_wheel(
        wheel,
        {
            "videoscope/debug.txt": personal_path,
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("personal absolute path" in item for item in violations)


def test_root_home_absolute_path_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    make_wheel(
        wheel,
        {
            "videoscope/debug.txt": "/root/private/candidate.whl",
            "videoscope/reporting/templates/report.html.j2": "<html></html>",
        },
    )

    violations = audit_distribution.audit_archive(wheel)

    assert any("/root/private/candidate.whl" in item for item in violations)


def test_sdist_allows_sanitizer_test_examples(tmp_path: Path) -> None:
    source = tmp_path / "videoscope-0.1.0.tar.gz"
    example = "C:" + "\\Users\\" + "Example\\private.mp4"

    payload = example.encode("utf-8")
    info = tarfile.TarInfo("videoscope-0.1.0/tests/test_distribution_audit.py")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
        for required in sorted(audit_distribution.REQUIRED_SDIST_MEMBERS):
            required_payload = b"release asset\n"
            required_info = tarfile.TarInfo(f"videoscope-0.1.0/{required}")
            required_info.size = len(required_payload)
            archive.addfile(required_info, io.BytesIO(required_payload))

    assert audit_distribution.audit_archive(source) == ()


@pytest.mark.parametrize(
    ("member", "known_source_literals"),
    (
        (
            "tests/rescue/test_artifacts.py",
            (
                '"C:/Users/private/file.mp4"',
                r'r"C:\Users\private\file.mp4"',
                r'r"C:\Users\private\clip.mp4"',
                r'r"Inspect C:\Users\private\clip.mp4"',
                '"C:/Users/private/source.mp4"',
                '"<p>C:/Users/private/source.mp4</p>"',
            ),
        ),
        (
            "tests/rescue/test_models.py",
            (
                '"C:/Users/example/faithful-rescue.mp4"',
                '"C:/Users/Alice/private.mp4"',
            ),
        ),
    ),
)
def test_sdist_allows_only_known_rescue_path_sanitizer_literals(
    tmp_path: Path,
    member: str,
    known_source_literals: tuple[str, ...],
) -> None:
    source = tmp_path / "videoscope-0.5.0.tar.gz"
    payload = "\n".join(known_source_literals).encode()
    info = tarfile.TarInfo(f"videoscope-0.5.0/{member}")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
        for required in sorted(audit_distribution.REQUIRED_SDIST_MEMBERS):
            required_payload = b"release asset\n"
            required_info = tarfile.TarInfo(f"videoscope-0.5.0/{required}")
            required_info.size = len(required_payload)
            archive.addfile(required_info, io.BytesIO(required_payload))

    assert audit_distribution.audit_archive(source) == ()


@pytest.mark.parametrize(
    ("member", "injected_path"),
    (
        (
            "tests/rescue/test_artifacts.py",
            "C:/Users/real-operator/private-recording.mov",
        ),
        (
            "tests/rescue/test_artifacts.py",
            "/home/real-operator/private-recording.mov",
        ),
        (
            "tests/rescue/test_models.py",
            "C:/Users/real-operator/private-recording.mov",
        ),
        (
            "tests/rescue/test_models.py",
            "/home/real-operator/private-recording.mov",
        ),
        (
            "tests/rescue/test_artifacts.py",
            "C:/Users/private/file.mp4/other-recording.mov",
        ),
        (
            "tests/rescue/test_models.py",
            "C:/Users/example/faithful-rescue.mp4/other-recording.mov",
        ),
    ),
)
def test_sdist_rejects_unknown_personal_path_in_rescue_test_module(
    tmp_path: Path,
    member: str,
    injected_path: str,
) -> None:
    source = tmp_path / "videoscope-0.5.0.tar.gz"
    payload = f"before\n{injected_path}\nafter\n".encode()
    info = tarfile.TarInfo(f"videoscope-0.5.0/{member}")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
        for required in sorted(audit_distribution.REQUIRED_SDIST_MEMBERS):
            required_payload = b"release asset\n"
            required_info = tarfile.TarInfo(f"videoscope-0.5.0/{required}")
            required_info.size = len(required_payload)
            archive.addfile(required_info, io.BytesIO(required_payload))

    violations = audit_distribution.audit_archive(source)

    assert any(
        "personal absolute path" in item and injected_path in item
        for item in violations
    )


@pytest.mark.parametrize(
    ("member", "source_text", "injected_path"),
    (
        (
            "tests/rescue/test_artifacts.py",
            'candidate = "C:/Users/private/file.mp4+other-recording.mov"',
            "C:/Users/private/file.mp4+other-recording.mov",
        ),
        (
            "tests/rescue/test_artifacts.py",
            r'candidate = r"C:\Users\private\file.mp4+other-recording.mov"',
            r"C:\Users\private\file.mp4+other-recording.mov",
        ),
        (
            "tests/rescue/test_models.py",
            'candidate = "C:/Users/example/faithful-rescue.mp4追加.mov"',
            "C:/Users/example/faithful-rescue.mp4追加.mov",
        ),
        (
            "tests/rescue/test_artifacts.py",
            r'candidate = r"C:\Users\private\clip.mp4@other-recording.mov"',
            r"C:\Users\private\clip.mp4@other-recording.mov",
        ),
        (
            "tests/rescue/test_artifacts.py",
            r'candidate = br"C:\Users\private\file.mp4"',
            r"C:\Users\private\file.mp4",
        ),
        (
            "tests/rescue/test_models.py",
            'candidate = "C:/Users/Alice/private.mp4"suffix',
            "C:/Users/Alice/private.mp4",
        ),
        (
            "tests/rescue/test_artifacts.py",
            r'candidate = "C:/Users/private/file.mp4\"+suffix"',
            "C:/Users/private/file.mp4",
        ),
    ),
)
def test_sdist_rejects_extended_or_rewritten_known_source_literal(
    tmp_path: Path,
    member: str,
    source_text: str,
    injected_path: str,
) -> None:
    source = tmp_path / "videoscope-0.5.0.tar.gz"
    payload = source_text.encode()
    info = tarfile.TarInfo(f"videoscope-0.5.0/{member}")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
        for required in sorted(audit_distribution.REQUIRED_SDIST_MEMBERS):
            required_payload = b"release asset\n"
            required_info = tarfile.TarInfo(f"videoscope-0.5.0/{required}")
            required_info.size = len(required_payload)
            archive.addfile(required_info, io.BytesIO(required_payload))

    violations = audit_distribution.audit_archive(source)
    rendered_path = repr(injected_path)[1:-1]

    assert any(
        "personal absolute path" in item and rendered_path in item
        for item in violations
    )


@pytest.mark.parametrize(
    ("member", "source_text", "injected_path"),
    (
        (
            "tests/rescue/test_artifacts.py",
            r"""candidate = (
    r"C:\Users\private\file.mp4"
    "+other-recording.mov"
)""",
            r"C:\Users\private\file.mp4+other-recording.mov",
        ),
        (
            "tests/rescue/test_models.py",
            """candidate = (
    "C:/Users/Alice/private.mp4"
    "/other-recording.mov"
)""",
            "C:/Users/Alice/private.mp4/other-recording.mov",
        ),
        (
            "tests/rescue/test_artifacts.py",
            r"""candidate = (
    r"C:\Users\private\file.mp4"
    # Keep the suffix on its own source line.
    "+other-recording.mov"
)""",
            r"C:\Users\private\file.mp4+other-recording.mov",
        ),
        (
            "tests/rescue/test_models.py",
            """candidate = (
    "C:/Users/Alice/private.mp4"
    # Keep the suffix on its own source line.
    "/other-recording.mov"
)""",
            "C:/Users/Alice/private.mp4/other-recording.mov",
        ),
    ),
)
def test_sdist_rejects_multiline_known_literal_concatenation(
    tmp_path: Path,
    member: str,
    source_text: str,
    injected_path: str,
) -> None:
    source = tmp_path / "videoscope-0.5.0.tar.gz"
    payload = source_text.encode()
    info = tarfile.TarInfo(f"videoscope-0.5.0/{member}")
    info.size = len(payload)
    with tarfile.open(source, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
        for required in sorted(audit_distribution.REQUIRED_SDIST_MEMBERS):
            required_payload = b"release asset\n"
            required_info = tarfile.TarInfo(f"videoscope-0.5.0/{required}")
            required_info.size = len(required_payload)
            archive.addfile(required_info, io.BytesIO(required_payload))

    violations = audit_distribution.audit_archive(source)

    assert any("personal absolute path" in item for item in violations)


def test_distribution_path_filters_supported_archives(tmp_path: Path) -> None:
    wheel = tmp_path / "videoscope-0.1.0-py3-none-any.whl"
    source = tmp_path / "videoscope-0.1.0.tar.gz"
    ignored = tmp_path / "notes.txt"
    wheel.touch()
    source.touch()
    ignored.touch()

    assert audit_distribution.distribution_paths(tmp_path) == (wheel, source)
