"""Audit built VideoScope archives for release-only distribution contents."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import re
import tarfile
import tokenize
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ARCHIVE_SUFFIXES = (".whl", ".tar.gz")
VIDEO_SUFFIXES = {".avi", ".mkv", ".mov", ".mp4", ".webm"}
CACHE_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
}
TEXT_SUFFIXES = {
    ".cff",
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".j2",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PERSONAL_PATH_PATTERNS = (
    re.compile(
        r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"']+(?:[\\/][^\r\n\"']*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9._/-])/(?:(?:Users|home)/[^/\r\n\"']+"
        r"(?:/[^\r\n\"']*)?|root(?:/[^\r\n\"']*)?)"
    ),
)
MAX_TEXT_SCAN_BYTES = 2_000_000
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
THIRD_PARTY_NOTICE_NAME = "THIRD_PARTY_NOTICES.txt"
THIRD_PARTY_NOTICE_PATH = REPOSITORY_ROOT / THIRD_PARTY_NOTICE_NAME
WHEEL_DISTRIBUTION_NAME = "genvideoscope"
REQUIRED_WHEEL_MEMBERS = {
    "videoscope/privacy/pipeline.py",
    "videoscope/privacy/verification.py",
    "videoscope/rescue/models.py",
    "videoscope/rescue/pipeline.py",
    "videoscope/rescue/verification.py",
    "videoscope/content/models.py",
    "videoscope/content/pipeline.py",
    "videoscope/content/verification.py",
    "videoscope/intelligence/models.py",
    "videoscope/intelligence/pipeline.py",
    "videoscope/intelligence/providers/ollama.py",
    "videoscope/reporting/templates/report.html.j2",
    "videoscope/reporting/templates/content_report.html.j2",
    "videoscope/reporting/templates/rescue_report.html.j2",
    "videoscope/web/static/index.html",
}
REQUIRED_SDIST_MEMBERS = {
    "README.md",
    "docs/privacy-api.md",
    "docs/privacy-schema.md",
    "docs/rescue-schema.md",
    "docs/safe-sharing.md",
    "docs/video-rescue-guide.md",
    "docs/content-schema.md",
    "docs/long-video-content.md",
    "docs/advanced-ai.md",
    "docs/advanced-ai-evaluation.md",
    "examples/privacy-review.example.json",
    "examples/rescue-config.example.json",
    "examples/safe_sharing.ps1",
    "examples/safe_sharing.sh",
    "examples/video_rescue.ps1",
    "examples/video_rescue.sh",
}
STATIC_INDEX_MEMBER = "videoscope/web/static/index.html"
SDIST_PERSONAL_PATH_EXEMPTIONS = frozenset(
    {
        "tests/privacy/test_artifacts.py",
        "tests/privacy/test_models.py",
        "tests/privacy/test_text.py",
        "tests/reporting/test_html.py",
        "tests/test_distribution_audit.py",
        "tests/test_smoke_test.py",
        "scripts/audit_distribution.py",
        "scripts/smoke_test.py",
    }
)
SDIST_PERSONAL_PATH_SOURCE_LITERAL_EXEMPTIONS = {
    "tests/content/test_features.py": frozenset(
        {
            '"C:/Users/private/source.mp4"',
            '"C:/Users"',
        }
    ),
    "tests/content/test_models.py": frozenset(
        {
            '"C:/Users/example/output.mp4"',
            '"C:/Users/example/private"',
        }
    ),
    "tests/content/test_verification.py": frozenset(
        {
            '"C:\\\\Users\\\\name\\\\file.mp4"',
        }
    ),
    "tests/rescue/test_artifacts.py": frozenset(
        {
            '"C:/Users/private/file.mp4"',
            r'r"C:\Users\private\file.mp4"',
            r'r"C:\Users\private\clip.mp4"',
            r'r"Inspect C:\Users\private\clip.mp4"',
            '"C:/Users/private/source.mp4"',
            '"<p>C:/Users/private/source.mp4</p>"',
        }
    ),
    "tests/rescue/test_models.py": frozenset(
        {
            '"C:/Users/example/faithful-rescue.mp4"',
            '"C:/Users/Alice/private.mp4"',
        }
    ),
    "tests/rescue/test_v15_clarity_node_contract.py": frozenset(
        {
            r'r"C:\Users\person\audit.json"',
            '"C:/Users/private/fixed-ffmpeg.exe"',
        }
    ),
}
STATIC_ASSET_REFERENCE = re.compile(
    r"(?:src|href)=[\"'](?P<path>/?assets/index-[A-Za-z0-9_-]+\.(?:js|css))[\"']"
)


def is_distribution(path: Path) -> bool:
    """Return whether a file is a supported distribution archive."""
    return path.name.endswith(ARCHIVE_SUFFIXES)


def distribution_paths(path: Path) -> tuple[Path, ...]:
    """Resolve one archive or all supported archives in a directory."""
    if path.is_file():
        return (path,) if is_distribution(path) else ()
    if not path.is_dir():
        return ()
    return tuple(
        item
        for item in sorted(path.iterdir(), key=lambda item: item.name)
        if item.is_file() and is_distribution(item)
    )


def zip_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield normalized member names and bytes from a wheel."""
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            yield info.filename, archive.read(info)


def canonical_text_sha256_bytes(data: bytes) -> str:
    """Hash UTF-8 text after cross-platform newline normalization."""
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dashboard_notice_violations(
    notice_members: list[tuple[str, bytes]],
    *,
    expected_member: str | None = None,
) -> tuple[str, ...]:
    if not notice_members:
        return ("dashboard third-party notice is missing",)
    if len(notice_members) != 1:
        return ("dashboard third-party notice is ambiguous",)
    if expected_member is not None and notice_members[0][0] != expected_member:
        return ("dashboard third-party notice is misplaced",)
    try:
        expected_digest = canonical_text_sha256_bytes(
            THIRD_PARTY_NOTICE_PATH.read_bytes()
        )
    except (OSError, UnicodeError) as error:
        return (f"dashboard third-party notice source is unavailable: {error}",)
    name, payload = notice_members[0]
    try:
        actual_digest = canonical_text_sha256_bytes(payload)
    except UnicodeError:
        return (f"{name}: dashboard third-party notice is not UTF-8",)
    if actual_digest != expected_digest:
        return (f"{name}: dashboard third-party notice digest mismatch",)
    return ()


def _expected_wheel_notice_member(path: Path) -> str | None:
    """Return the notice path bound to this exact genvideoscope wheel identity."""
    fields = path.name.removesuffix(".whl").split("-")
    if (
        len(fields) < 5
        or fields[0].replace("_", "-").casefold() != WHEEL_DISTRIBUTION_NAME
        or not fields[1]
    ):
        return None
    dist_info = f"{fields[0]}-{fields[1]}.dist-info"
    return f"{dist_info}/licenses/{THIRD_PARTY_NOTICE_NAME}"


def tar_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield normalized member names and bytes from a source archive."""
    with tarfile.open(path, mode="r:gz") as archive:
        for info in archive.getmembers():
            if not info.isfile():
                continue
            extracted = archive.extractfile(info)
            if extracted is not None:
                yield info.name, extracted.read()


def archive_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield members from a supported distribution archive."""
    if path.suffix == ".whl":
        yield from zip_members(path)
        return
    if path.name.endswith(".tar.gz"):
        yield from tar_members(path)
        return
    raise ValueError(f"Unsupported distribution archive: {path.name}")


def prohibited_member_reason(name: str) -> str | None:
    """Explain why an archive member must not be distributed."""
    normalized = PurePosixPath(name.replace("\\", "/"))
    parts = tuple(part.casefold() for part in normalized.parts)

    if any(part in CACHE_PARTS for part in parts):
        return "local cache or virtual environment"
    if (
        ("tests", "fixtures", "generated") in zip(parts, parts[1:], parts[2:])
        and normalized.name.startswith(".")
        and ".tmp" in normalized.name.casefold()
    ):
        return "fixture corruption intermediate"
    if any(
        part in {"rescue-review-private", "rescue_review_private"} for part in parts
    ):
        return "private Video Rescue artifact"
    if any(part in {"rescue-output", "rescue_output"} for part in parts):
        return "public Video Rescue output"
    if any(
        part
        in {
            ".worktrees",
            "rescue-workspace",
            "rescue_workspace",
            "workspace",
            "workspaces",
        }
        for part in parts
    ):
        return "Video Rescue workspace"
    if any(
        part in {"privacy-review-private", "privacy_review_private"} for part in parts
    ):
        return "private Safe Sharing artifact"
    if any(part in {"share-package", "share_package"} for part in parts):
        return "public Safe Sharing output"
    if any(part == "staging" or part.startswith("pending-package-") for part in parts):
        return "pending or staging output"
    if any(
        part in {"unredacted-evidence", "unredacted_evidence", "private-evidence"}
        for part in parts
    ):
        return "unredacted private evidence"
    if "runs" in parts or "videoscope-output" in parts:
        return "local analysis output"
    if ("tests", "fixtures", "generated") in zip(parts, parts[1:], parts[2:]):
        return "generated synthetic fixture"
    if normalized.suffix.casefold() in VIDEO_SUFFIXES:
        return "video file"
    if normalized.name.casefold().endswith(".log"):
        return "local log"
    return None


def source_string_value_and_prefix(source: str) -> tuple[str, str] | None:
    """Return the decoded value and exact prefix for an ordinary source string."""
    match = re.match(r"(?P<prefix>[A-Za-z]*)(?P<quote>['\"])", source)
    if match is None:
        return None
    quote_end = match.end()
    if quote_end < len(source) and source[quote_end] == match.group("quote"):
        return None
    try:
        value = ast.literal_eval(source)
    except (SyntaxError, ValueError):
        return None
    return (value, match.group("prefix")) if isinstance(value, str) else None


def has_string_concatenation(
    tokens: list[tokenize.TokenInfo],
    index: int,
) -> bool:
    """Return whether a source string participates in direct concatenation."""
    for direction in (-1, 1):
        neighbor = index + direction
        while 0 <= neighbor < len(tokens) and tokens[neighbor].type in {
            tokenize.NL,
            tokenize.COMMENT,
            tokenize.INDENT,
            tokenize.DEDENT,
        }:
            neighbor += direction
        if not 0 <= neighbor < len(tokens):
            continue
        token = tokens[neighbor]
        if token.type == tokenize.STRING or (
            token.type == tokenize.OP and token.string == "+"
        ):
            return True
    return False


def redact_known_test_source_literals(
    text: str,
    known_source_literals: Iterable[str],
) -> str:
    """Redact approved whole source strings from valid Python test source."""
    known = frozenset(
        literal_info
        for literal in known_source_literals
        if (literal_info := source_string_value_and_prefix(literal)) is not None
    )
    if not known:
        return text
    try:
        ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        line_starts = [0]
        line_starts.extend(match.end() for match in re.finditer("\n", text))
        spans = [
            (
                line_starts[token.start[0] - 1] + token.start[1],
                line_starts[token.end[0] - 1] + token.end[1],
            )
            for index, token in enumerate(tokens)
            if token.type == tokenize.STRING
            and not has_string_concatenation(tokens, index)
            and (literal_info := source_string_value_and_prefix(token.string)) in known
        ]
    except (IndentationError, SyntaxError, ValueError, tokenize.TokenError):
        return text
    for start, end in reversed(spans):
        text = text[:start] + '"<known-synthetic-personal-path>"' + text[end:]
    return text


def personal_path_matches(
    name: str,
    data: bytes,
    *,
    known_test_source_literals: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return personal absolute path fragments found in a small text member."""
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix not in TEXT_SUFFIXES or len(data) > MAX_TEXT_SCAN_BYTES:
        return ()
    text = redact_known_test_source_literals(
        data.decode("utf-8", errors="replace"),
        known_test_source_literals,
    )
    matches: list[str] = []
    for pattern in PERSONAL_PATH_PATTERNS:
        matches.extend(match.group(0) for match in pattern.finditer(text))
    return tuple(matches)


def audit_archive(path: Path) -> tuple[str, ...]:
    """Return human-readable violations for one archive."""
    violations: list[str] = []
    is_wheel = path.suffix == ".whl"
    member_names: set[str] = set()
    source_member_names: set[str] = set()
    static_index: bytes | None = None
    third_party_notices: list[tuple[str, bytes]] = []
    expected_notice_member = _expected_wheel_notice_member(path) if is_wheel else None
    if is_wheel and expected_notice_member is None:
        violations.append(
            f"wheel filename does not identify {WHEEL_DISTRIBUTION_NAME} distribution"
        )
    for name, data in archive_members(path):
        normalized_name = name.replace("\\", "/")
        member_names.add(normalized_name)
        normalized_parts = PurePosixPath(normalized_name).parts
        source_member_name = (
            PurePosixPath(*normalized_parts[1:]).as_posix()
            if path.name.endswith(".tar.gz") and len(normalized_parts) > 1
            else normalized_name
        )
        source_member_names.add(source_member_name)
        if (
            is_wheel
            and normalized_name.endswith(
                f".dist-info/licenses/{THIRD_PARTY_NOTICE_NAME}"
            )
        ) or (not is_wheel and source_member_name == THIRD_PARTY_NOTICE_NAME):
            third_party_notices.append((normalized_name, data))
        if normalized_name == STATIC_INDEX_MEMBER:
            static_index = data
        reason = prohibited_member_reason(name)
        if reason is not None:
            violations.append(f"{name}: {reason}")
        if is_wheel or source_member_name not in SDIST_PERSONAL_PATH_EXEMPTIONS:
            known_test_source_literals = (
                ()
                if is_wheel
                else SDIST_PERSONAL_PATH_SOURCE_LITERAL_EXEMPTIONS.get(
                    source_member_name, ()
                )
            )
            for match in personal_path_matches(
                name,
                data,
                known_test_source_literals=known_test_source_literals,
            ):
                violations.append(f"{name}: personal absolute path {match!r}")
    violations.extend(
        _dashboard_notice_violations(
            third_party_notices,
            expected_member=expected_notice_member,
        )
    )
    if is_wheel:
        for required in sorted(REQUIRED_WHEEL_MEMBERS - member_names):
            violations.append(f"{required}: required runtime asset is missing")
        if static_index is not None:
            index_text = static_index.decode("utf-8", errors="replace")
            references = {
                match.group("path").lstrip("/")
                for match in STATIC_ASSET_REFERENCE.finditer(index_text)
            }
            suffixes = {PurePosixPath(reference).suffix for reference in references}
            for suffix in (".css", ".js"):
                if suffix not in suffixes:
                    violations.append(
                        f"{STATIC_INDEX_MEMBER}: referenced hashed "
                        f"{suffix[1:]} asset is missing"
                    )
            static_root = PurePosixPath(STATIC_INDEX_MEMBER).parent
            for reference in sorted(references):
                required = (static_root / reference).as_posix()
                if required not in member_names:
                    violations.append(
                        f"{required}: referenced dashboard asset is missing"
                    )
    else:
        for required in sorted(REQUIRED_SDIST_MEMBERS - source_member_names):
            violations.append(f"{required}: required source release asset is missing")
    return tuple(violations)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Audit VideoScope wheel and sdist contents.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("dist"),
        help="Distribution archive or directory (default: dist).",
    )
    return parser.parse_args()


def main() -> int:
    """Audit requested archives and print a compact result."""
    args = parse_args()
    archives = distribution_paths(args.path)
    if not archives:
        print(f"No wheel or source archive found at {args.path}.")
        return 2

    failed = False
    for archive in archives:
        violations = audit_archive(archive)
        if violations:
            failed = True
            print(f"FAIL {archive.name}")
            for violation in violations:
                print(f"  - {violation}")
        else:
            print(f"PASS {archive.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
