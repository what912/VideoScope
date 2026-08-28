"""Fail-closed tests for deterministic v0.8.2 release evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import release_assets

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
PRIMARY_NAMES = (
    "genvideoscope-0.8.2-py3-none-any.whl",
    "genvideoscope-0.8.2.tar.gz",
    "VideoScope-Setup-x64.exe",
)
OUTPUT_NAMES = {
    *PRIMARY_NAMES,
    "VideoScope-Setup-x64.exe.sha256",
    "SHA256SUMS.txt",
    "release-evidence.json",
}


class FakeGitRunner:
    """Return deterministic Git results without starting a real process."""

    def __init__(
        self,
        *,
        head: str | tuple[str, ...] = COMMIT,
        status: str | tuple[str, ...] = "",
    ) -> None:
        self.heads = (head,) if isinstance(head, str) else head
        self.statuses = (status,) if isinstance(status, str) else status
        self.head_index = 0
        self.status_index = 0
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def _next_head(self) -> str:
        result = self.heads[min(self.head_index, len(self.heads) - 1)]
        self.head_index += 1
        return result

    def _next_status(self) -> str:
        result = self.statuses[min(self.status_index, len(self.statuses) - 1)]
        self.status_index += 1
        return result

    def __call__(
        self, argv: tuple[str, ...], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, cwd))
        if argv == ("git", "rev-parse", "--verify", "HEAD^{commit}"):
            return subprocess.CompletedProcess(argv, 0, f"{self._next_head()}\n", "")
        if argv == (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ):
            return subprocess.CompletedProcess(argv, 0, self._next_status(), "")
        raise AssertionError(f"unexpected Git command: {argv!r}")


def make_primary_assets(root: Path) -> list[Path]:
    """Create tiny synthetic files with the exact canonical release names."""
    root.mkdir(exist_ok=True)
    input_root = root / "input"
    input_root.mkdir()
    assets: list[Path] = []
    for index, name in enumerate(PRIMARY_NAMES, start=1):
        path = input_root / name
        path.write_bytes((f"asset-{index}\n").encode())
        assets.append(path)
    return assets


def prepare(root: Path) -> tuple[Path, list[Path], FakeGitRunner]:
    """Prepare one synthetic release evidence directory."""
    assets = make_primary_assets(root)
    output = root / "release"
    runner = FakeGitRunner()
    release_assets.prepare_release_assets(
        assets,
        output,
        expected_commit=COMMIT,
        repository_root=root,
        runner=runner,
    )
    return output, assets, runner


def test_default_git_runner_uses_argv_shell_false_and_bounded_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production runner must never delegate command parsing to a shell."""
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = release_assets.run_git_command(
        ("git", "rev-parse", "--verify", "HEAD^{commit}"), tmp_path
    )

    assert result.stdout == "ok\n"
    assert observed["argv"] == [
        "git",
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    ]
    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["timeout"] == release_assets.GIT_TIMEOUT_SECONDS


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("failure", "message"),
    [
        (
            subprocess.TimeoutExpired(["git", "status"], timeout=10),
            "timed out",
        ),
        (FileNotFoundError("git missing"), "could not be started"),
    ],
)
def test_default_git_runner_translates_process_start_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
    message: str,
) -> None:
    def fail_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail_run)
    with pytest.raises(release_assets.ReleaseAssetError, match=message):
        release_assets.run_git_command(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            tmp_path,
        )


def test_prepare_is_no_clobber_canonical_and_deterministic(tmp_path: Path) -> None:
    """Preparation copies only canonical inputs and emits stable evidence."""
    output, assets, runner = prepare(tmp_path / "first")

    assert {path.name for path in output.iterdir()} == OUTPUT_NAMES
    assert all(path.is_file() and not path.is_symlink() for path in output.iterdir())
    for source in assets:
        assert (output / source.name).read_bytes() == source.read_bytes()

    expected_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in assets
    }
    expected_sums = "".join(
        f"{expected_hashes[name]}  {name}\n" for name in sorted(PRIMARY_NAMES)
    )
    assert (output / "SHA256SUMS.txt").read_text(encoding="utf-8") == expected_sums
    installer_hash = expected_hashes["VideoScope-Setup-x64.exe"]
    assert (output / "VideoScope-Setup-x64.exe.sha256").read_text(
        encoding="utf-8"
    ) == f"{installer_hash}  VideoScope-Setup-x64.exe\n"

    evidence_bytes = (output / "release-evidence.json").read_bytes()
    evidence = json.loads(evidence_bytes)
    assert evidence == {
        "assets": [
            {
                "name": name,
                "sha256": expected_hashes[name],
                "size_bytes": (output / name).stat().st_size,
            }
            for name in sorted(PRIMARY_NAMES)
        ],
        "commit": COMMIT,
        "schema_version": "1",
    }
    assert evidence_bytes == (
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    assert runner.calls == [
        (("git", "rev-parse", "--verify", "HEAD^{commit}"), tmp_path / "first"),
        (
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            tmp_path / "first",
        ),
        (("git", "rev-parse", "--verify", "HEAD^{commit}"), tmp_path / "first"),
        (
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            tmp_path / "first",
        ),
    ]

    second_root = tmp_path / "second"
    second_assets = make_primary_assets(second_root)
    second_output = second_root / "release"
    release_assets.prepare_release_assets(
        second_assets,
        second_output,
        expected_commit=COMMIT,
        repository_root=second_root,
        runner=FakeGitRunner(),
    )
    for name in OUTPUT_NAMES:
        assert (second_output / name).read_bytes() == (output / name).read_bytes()

    with pytest.raises(release_assets.ReleaseAssetError, match="already exists"):
        release_assets.prepare_release_assets(
            assets,
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path / "first",
            runner=FakeGitRunner(),
        )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("mutate", "message"),
    [
        (lambda paths, root: paths[:2], "exactly three"),
        (lambda paths, root: [*paths, root / "extra.bin"], "exactly three"),
        (lambda paths, root: [paths[0], paths[0], paths[2]], "duplicate"),
        (
            lambda paths, root: [
                paths[0].with_name("GENVIDEOSCOPE-0.8.2-py3-none-any.whl"),
                paths[1],
                paths[2],
            ],
            "canonical",
        ),
    ],
)
def test_prepare_rejects_noncanonical_asset_sets_before_creating_output(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    assets = make_primary_assets(tmp_path)
    extra = tmp_path / "extra.bin"
    extra.write_bytes(b"extra")
    wrong_case = assets[0].with_name("GENVIDEOSCOPE-0.8.2-py3-none-any.whl")
    wrong_case.write_bytes(b"wrong")
    output = tmp_path / "release"

    changed = mutate(assets, tmp_path)  # type: ignore[operator]
    with pytest.raises(release_assets.ReleaseAssetError, match=message):
        release_assets.prepare_release_assets(
            changed,
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=FakeGitRunner(),
        )
    assert not output.exists()


def test_prepare_rejects_symlinks_before_creating_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets = make_primary_assets(tmp_path)
    output = tmp_path / "release"
    original = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path == assets[0] or original(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(release_assets.ReleaseAssetError, match="symbolic link"):
        release_assets.prepare_release_assets(
            assets,
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=FakeGitRunner(),
        )
    assert not output.exists()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("expected_commit", "runner", "message"),
    [
        ("ABCDEF", FakeGitRunner(), "40 lowercase"),
        (
            COMMIT,
            FakeGitRunner(head="fedcba9876543210fedcba9876543210fedcba98"),
            "does not match",
        ),
        (COMMIT, FakeGitRunner(status=" M README.md\n"), "not clean"),
    ],
)
def test_prepare_rejects_invalid_or_unfrozen_repository_before_output(
    tmp_path: Path,
    expected_commit: str,
    runner: FakeGitRunner,
    message: str,
) -> None:
    assets = make_primary_assets(tmp_path)
    output = tmp_path / "release"
    with pytest.raises(release_assets.ReleaseAssetError, match=message):
        release_assets.prepare_release_assets(
            assets,
            output,
            expected_commit=expected_commit,
            repository_root=tmp_path,
            runner=runner,
        )
    assert not output.exists()


def test_prepare_reports_nonzero_git_with_bounded_stderr(tmp_path: Path) -> None:
    assets = make_primary_assets(tmp_path)
    output = tmp_path / "release"

    def failing_runner(
        argv: tuple[str, ...], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, "", "failure:" + ("x" * 10_000))

    with pytest.raises(release_assets.ReleaseAssetError) as raised:
        release_assets.prepare_release_assets(
            assets,
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=failing_runner,
        )

    assert "failure:" in str(raised.value)
    assert len(str(raised.value)) <= release_assets.MAX_GIT_ERROR_CHARS + 300
    assert not output.exists()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("runner", "message"),
    [
        (FakeGitRunner(head=(COMMIT, OTHER_COMMIT)), "does not match"),
        (FakeGitRunner(status=("", " M README.md\n")), "not clean"),
    ],
)
def test_prepare_rechecks_repository_after_output_creation(
    tmp_path: Path, runner: FakeGitRunner, message: str
) -> None:
    assets = make_primary_assets(tmp_path)
    output = tmp_path / "release"

    with pytest.raises(release_assets.ReleaseAssetError, match=message):
        release_assets.prepare_release_assets(
            assets,
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=runner,
        )

    assert output.is_dir()
    assert {path.name for path in output.iterdir()} == OUTPUT_NAMES


def test_verify_accepts_only_the_exact_canonical_evidence(tmp_path: Path) -> None:
    output, _, _ = prepare(tmp_path)
    release_assets.verify_release_assets(
        output,
        expected_commit=COMMIT,
        repository_root=tmp_path,
        runner=FakeGitRunner(),
    )


def test_verify_snapshots_all_six_files_before_and_after_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, _, _ = prepare(tmp_path)
    snapshots: list[set[str]] = []
    original_snapshot = release_assets._snapshot_release_files

    def observe_snapshot(root: Path) -> dict[str, tuple[str, int]]:
        snapshot = original_snapshot(root)
        snapshots.append(set(snapshot))
        return snapshot

    monkeypatch.setattr(release_assets, "_snapshot_release_files", observe_snapshot)
    release_assets.verify_release_assets(
        output,
        expected_commit=COMMIT,
        repository_root=tmp_path,
        runner=FakeGitRunner(),
    )

    assert snapshots == [OUTPUT_NAMES, OUTPUT_NAMES]


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "filename",
    ["VideoScope-Setup-x64.exe", "release-evidence.json"],
)
def test_verify_rejects_files_changed_between_full_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str
) -> None:
    output, _, _ = prepare(tmp_path)
    original_snapshot = release_assets._snapshot_release_files
    call_count = 0

    def mutate_before_final_snapshot(root: Path) -> dict[str, tuple[str, int]]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            with (root / filename).open("ab") as target:
                target.write(b"changed-between-snapshots")
        return original_snapshot(root)

    monkeypatch.setattr(
        release_assets,
        "_snapshot_release_files",
        mutate_before_final_snapshot,
    )
    with pytest.raises(release_assets.ReleaseAssetError, match="changed during"):
        release_assets.verify_release_assets(
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=FakeGitRunner(),
        )
    assert call_count == 2


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("runner", "message"),
    [
        (FakeGitRunner(head=(COMMIT, OTHER_COMMIT)), "does not match"),
        (FakeGitRunner(status=("", " M README.md\n")), "not clean"),
    ],
)
def test_verify_rechecks_repository_after_final_snapshot(
    tmp_path: Path, runner: FakeGitRunner, message: str
) -> None:
    output, _, _ = prepare(tmp_path)

    with pytest.raises(release_assets.ReleaseAssetError, match=message):
        release_assets.verify_release_assets(
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=runner,
        )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("mutation", "message"),
    [
        ("missing", "exact release file set"),
        ("extra", "exact release file set"),
        ("primary", "checksum"),
        ("sidecar", "canonical"),
        ("sums", "canonical"),
        ("evidence", "canonical"),
        ("evidence_commit", "commit"),
    ],
)
def test_verify_rejects_missing_extra_mutated_or_noncanonical_evidence(
    tmp_path: Path, mutation: str, message: str
) -> None:
    output, _, _ = prepare(tmp_path)
    if mutation == "missing":
        (output / "genvideoscope-0.8.2.tar.gz").unlink()
    elif mutation == "extra":
        (output / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    elif mutation == "primary":
        (output / "VideoScope-Setup-x64.exe").write_bytes(b"mutated")
    elif mutation == "sidecar":
        sidecar = output / "VideoScope-Setup-x64.exe.sha256"
        sidecar.write_bytes(sidecar.read_bytes().replace(b"  ", b" ", 1))
    elif mutation == "sums":
        sums = output / "SHA256SUMS.txt"
        sums.write_bytes(sums.read_bytes().replace(b"  ", b" ", 1))
    elif mutation == "evidence":
        evidence = output / "release-evidence.json"
        evidence.write_bytes(evidence.read_bytes().replace(b"  ", b"    ", 1))
    else:
        evidence = output / "release-evidence.json"
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        payload["commit"] = "fedcba9876543210fedcba9876543210fedcba98"
        evidence.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    with pytest.raises(release_assets.ReleaseAssetError, match=message):
        release_assets.verify_release_assets(
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=FakeGitRunner(),
        )


def test_verify_rejects_commit_mismatch(tmp_path: Path) -> None:
    output, _, _ = prepare(tmp_path)
    with pytest.raises(release_assets.ReleaseAssetError, match="does not match"):
        release_assets.verify_release_assets(
            output,
            expected_commit=COMMIT,
            repository_root=tmp_path,
            runner=FakeGitRunner(head="fedcba9876543210fedcba9876543210fedcba98"),
        )


def test_cli_wires_prepare_and_verify_without_starting_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets = [tmp_path / name for name in PRIMARY_NAMES]
    output = tmp_path / "release"
    observed: list[tuple[str, object]] = []

    def fake_prepare(
        primary_assets: list[Path],
        output_root: Path,
        *,
        expected_commit: str,
        repository_root: Path,
        runner: object = None,
    ) -> None:
        observed.append(
            (
                "prepare",
                (primary_assets, output_root, expected_commit, repository_root, runner),
            )
        )

    def fake_verify(
        output_root: Path,
        *,
        expected_commit: str,
        repository_root: Path,
        runner: object = None,
    ) -> None:
        observed.append(
            ("verify", (output_root, expected_commit, repository_root, runner))
        )

    monkeypatch.setattr(release_assets, "prepare_release_assets", fake_prepare)
    monkeypatch.setattr(release_assets, "verify_release_assets", fake_verify)

    assert (
        release_assets.main(
            [
                "prepare",
                "--commit",
                COMMIT,
                "--repo-root",
                str(tmp_path),
                "--output-root",
                str(output),
                *(str(path) for path in assets),
            ]
        )
        == 0
    )
    assert (
        release_assets.main(
            [
                "verify",
                "--commit",
                COMMIT,
                "--repo-root",
                str(tmp_path),
                "--output-root",
                str(output),
            ]
        )
        == 0
    )
    assert observed == [
        ("prepare", (assets, output, COMMIT, tmp_path, None)),
        ("verify", (output, COMMIT, tmp_path, None)),
    ]
