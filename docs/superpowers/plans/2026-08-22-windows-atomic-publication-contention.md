# Windows Atomic Publication Contention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Windows atomic no-clobber publication while tolerating the narrowly observed transient `WinError 5` contention window.

**Architecture:** Each of the two existing publication modules gets the same small, private retry primitive because neither module is an appropriate dependency of the other. The helper retries only an intact-source/absent-target `WinError 5`; clarity additionally stops creating and replacing an empty Windows destination placeholder.

**Tech Stack:** Python 3.12, pathlib, pytest, Ruff, mypy

**Spec:** `docs/superpowers/specs/2026-08-22-windows-atomic-publication-contention.md`

## Global Constraints

- Keep atomic no-clobber behavior; never delete, replace, or overwrite a concurrent destination.
- Retry only Windows `WinError 5` while the source exists and destination is absent.
- Use retry delays `(0.01, 0.02, 0.04, 0.08, 0.16)` for at most six attempts and `0.31` seconds total waiting.
- Keep all algorithm and qualification thresholds unchanged.
- Do not add skips, xfails, dependencies, network access, FFmpeg/ffprobe launches, Git operations, release operations, PREPARE, or execute.
- Do not commit; this plan intentionally supersedes the generic skill template's commit step.

---

### Task 1: Stabilize clarity provenance promotion

**Files:**
- Modify: `tests/rescue/test_v15_clarity_node_contract.py`
- Modify: `tests/rescue/clarity_runtime_provenance.py`

**Interfaces:**
- Consumes: fully flushed `partial: Path` and absent `final: Path`
- Produces: `_retry_windows_no_replace_rename(source: Path, target: Path, *, rename: Callable[[Path, Path], None] = os.rename, sleep: Callable[[float], None] = time.sleep) -> None`

- [ ] **Step 1: Add deterministic RED tests**

Add tests that inject rename and sleep callables into the wished-for helper.
Use real temporary source and target paths and assert observable filesystem
results:

```python
def test_windows_no_replace_rename_recovers_from_transient_access_denied(
    tmp_path: Path,
) -> None:
    source = tmp_path / "完整 partial.json"
    target = tmp_path / "final.json"
    source.write_bytes(b"complete")
    attempts = 0
    delays: list[float] = []

    def rename(first: Path, second: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = PermissionError(13, "access denied", str(first))
            error.winerror = 5
            raise error
        os.rename(first, second)

    provenance_module._retry_windows_no_replace_rename(
        source, target, rename=rename, sleep=delays.append
    )

    assert attempts == 2
    assert delays == [0.01]
    assert target.read_bytes() == b"complete"
    assert not source.exists()
```

Add separate cases proving exhaustion is bounded and preserves the final
`PermissionError`, and proving an appearing target, missing source, and a
non-`WinError 5` error are never retried. Add a Windows promotion regression
case that pre-creates `final` and proves its bytes and `partial` bytes are both
preserved.

- [ ] **Step 2: Run the new clarity tests and verify RED**

Run only the newly added node IDs with the repository-root Python environment,
current-worktree `PYTHONPATH`, and a fresh no-clobber pytest/cache root.
Expected result: failures because `_retry_windows_no_replace_rename` does not
exist and/or `_atomic_promote` still performs placeholder replacement.

- [ ] **Step 3: Add the minimal clarity implementation**

Import `time`, define the five named delays, and implement:

```python
def _retry_windows_no_replace_rename(
    source: Path,
    target: Path,
    *,
    rename: Callable[[Path, Path], None] = os.rename,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for delay in (*_WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            rename(source, target)
        except OSError as error:
            if (
                delay is None
                or getattr(error, "winerror", None) != 5
                or not _path_exists_no_follow(source)
                or _path_exists_no_follow(target)
            ):
                raise
            sleep(delay)
        else:
            return
```

On Windows, `_atomic_promote` calls this helper directly without creating a
final placeholder. Keep the current exclusive-placeholder plus `os.replace`
branch unchanged on non-Windows.

- [ ] **Step 4: Run the focused clarity tests and verify GREEN**

Run the new fault-injection tests plus the existing ownership, no-clobber,
Unicode, and incomplete-finalizer tests. Expected result: all selected tests
pass with no warnings.

### Task 2: Stabilize V15 bundle directory publication

**Files:**
- Modify: `tests/scripts/test_verify_b_v15_demo.py`
- Modify: `scripts/verify_b_v15_demo.py`

**Interfaces:**
- Consumes: completed staging `source: Path` and absent output `target: Path`
- Produces: the same private helper signature as Task 1, scoped to the verifier module

- [ ] **Step 1: Add deterministic RED tests**

Add real-filesystem tests for the verifier helper covering one transient
`WinError 5` followed by success, bounded exhaustion, target appearance,
source disappearance, and a non-`WinError 5`. Assert final file/directory
state, attempt count, literal delay sequence, and the surfaced exception.

- [ ] **Step 2: Run the new verifier tests and verify RED**

Run only the new node IDs with a second fresh no-clobber pytest/cache root.
Expected result: failure because the helper is absent and the Windows branch
still calls `os.rename` once.

- [ ] **Step 3: Add the minimal verifier implementation**

Import `time`, implement the same bounded helper using `os.path.lexists`, and
route only the Windows branch of `_rename_directory_no_replace` through it.
Leave Linux, macOS, collision classification, staging cleanup, and public
error messages unchanged.

- [ ] **Step 4: Run the focused verifier tests and verify GREEN**

Run the new tests plus the existing byte-stable publication, race-winner,
real-empty-winner, private-path, and wiring tests. Expected result: all
selected tests pass with no warnings.

### Task 3: Mechanical format repair and bounded verification

**Files:**
- Format: `tests/rescue/test_fixture_rescue.py`
- Format: `tests/rescue/test_verification.py`
- Check: all six files modified by this plan and the retained fix2 work

**Interfaces:**
- Consumes: GREEN Tasks 1 and 2
- Produces: formatter-clean, statically checked bounded change set

- [ ] **Step 1: Format exactly the two retained Ruff failures**

Run Ruff format on only `tests/rescue/test_fixture_rescue.py` and
`tests/rescue/test_verification.py`.

- [ ] **Step 2: Run focused tests and static checks**

Run the combined focused non-native pytest selection, `ruff check`,
`ruff format --check`, `mypy` where supported by the repository controller,
`py_compile`, and `git diff --check`. Do not launch native media tools.

- [ ] **Step 3: Review and stop at the unified-validation gate**

Self-review no-clobber, retry guards, cleanup, and unchanged thresholds. Obtain
an independent review. If and only if review reports zero critical and zero
important findings, request explicit authorization for one new unrestricted
`scripts/validate.py` run using fixed FFmpeg/ffprobe 8.1.2 and a fresh
no-clobber root.
