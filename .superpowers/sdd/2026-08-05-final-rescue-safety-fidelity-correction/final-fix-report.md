# Final Rescue safety and fidelity correction report

Date: 2026-08-05
Worktree: repository-local `video-rescue-balanced-cpu-mvp` worktree
Starting commit: `5d6ddfc155154e2d0ad4a61595bdfc12c5440409`
Implementation commit: `fix(rescue): close final safety and fidelity gaps` (this report is included in that local commit)

## Outcome

All four Important final-review findings were corrected in one wave:

1. Rescue FFmpeg enable/select expressions and Python-side deflicker evaluation now use half-open ranges. Exact boundary tests prove that a seam belongs to the later adjacent interval and the final end is neutral.
2. Deflicker preview/remapping and FFmpeg rendering now share interval-aware gain semantics. When a faithful deletion compacts two source intervals onto one timestamp, the later interval wins deterministically.
3. Every content-changing mapped action is admitted only when its mappings exactly match the plan's retained source ranges, in retained order. The independent verifier enforces the same plan binding as well as artifact self-consistency. One shared 0.25-second tolerance is applied per segment and to total duration.
4. Atomic publication is the core pipeline's irrevocable completion cutoff. In Web, the cutoff is reached immediately after `pipeline.execute()` returns, so a later cancellation cannot replace a verified terminal result.

The local-first, CPU-first, source-read-only behavior remains intact. No networking, model download, GPU use, publication, push, PR, release, or deployment occurred.

## Root causes

- `_enable_expression` and the deflicker FFmpeg generator used inclusive `between(t,start,end)`, while Python deflicker evaluation also treated an interval end as included.
- Flattened deflicker gain lookup lost interval ownership at adjacent/deletion seams; duplicate timestamps kept the earlier interval.
- Mapping admission covered only a subset of action kinds, and verification checked only whether mappings were internally self-consistent with the rendered artifact.
- The core checked cancellation after atomic publication, while Web allowed a cancellation flag to override the result after `execute()` returned.

## Production changes

- `src/videoscope/rescue/commands.py`
  - Emit `gte(t,start)*lt(t,end)` for Rescue enable/select windows.
  - Ignore empty windows and use `0` for an empty expression.
- `src/videoscope/rescue/visual.py`
  - Resolve adjacent interval seams with half-open membership and reverse interval lookup, so the later interval wins.
  - Preserve interval-specific curves during deletion remapping and use the shared evaluator for FFmpeg segment gains.
  - Emit half-open FFmpeg predicates for deflicker fragments.
- `src/videoscope/rescue/timeline.py`
  - Add `mappings_match_retained_ranges`, binding source boundaries and retained order exactly while applying the shared `DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS` (0.25 seconds) to each segment and the total.
- `src/videoscope/rescue/capabilities.py`
  - Require exact plan-bound mappings for every `changes_content` action.
  - Derive only the canonical full-source mapping when no structural removal exists and mappings were omitted.
- `src/videoscope/rescue/verification.py`
  - Independently require plan-bound retained mappings before artifact mapping self-consistency can pass.
- `src/videoscope/rescue/pipeline.py`
  - Do not honor cancellation after a non-empty atomic publication result has been returned.
- `src/videoscope/web/rescue_jobs.py`
  - Record an irrevocable completion cutoff immediately after core execution returns.
  - Prevent cancellation and generic-exception fallback paths from overwriting a post-cutoff terminal result.

## Test changes

- `tests/rescue/test_commands.py`
- `tests/rescue/test_executor.py`
- `tests/rescue/test_stabilization.py`
  - Assert exact half-open FFmpeg expressions, adjacent seam ownership, final-end neutrality, and preview/execution parity after a middle deletion with deliberately different gains.
- `tests/rescue/test_verification.py`
  - Cover omitted and reordered retained mappings for an ordinary luma action at admission and in the independent verifier.
  - Bind existing declared-deletion verifier scenarios to plans that actually declare those retained ranges.
- `tests/rescue/test_pipeline.py`
  - Deterministically cancel after the real atomic publisher returns and assert the verified result remains completed.
- `tests/web/test_rescue_api.py`
  - Pause immediately after core `execute()` returns, cancel, then prove Web emits exactly one completed terminal event.

## TDD evidence

### RED: half-open ranges and deflicker seam parity

Command:

```powershell
$env:PYTHONPATH='src'; & '<repo-root>\.venv\Scripts\python.exe' -m pytest tests/rescue/test_stabilization.py::test_deflicker_curve_is_remapped_after_middle_deletion tests/rescue/test_stabilization.py::test_deflicker_adjacent_seam_is_half_open_and_later_interval_wins tests/rescue/test_commands.py::test_improvement_filter_uses_mapped_authorized_ranges_only tests/rescue/test_commands.py::test_locked_deflicker_gap_is_not_present_in_filter tests/rescue/test_commands.py::test_faithful_preview_represents_structural_removal_and_rotation tests/rescue/test_executor.py::test_native_executor_renders_bound_balanced_improvement_from_faithful -q
```

Result: `6 failed in 1.19s`. The deletion seam returned `(1.08, 0.93)` instead of `(0.93, 1.0)`, the adjacent seam retained the earlier gain, and FFmpeg strings still used `between(...)`.

### GREEN: half-open ranges and deflicker seam parity

Same command after implementation: `6 passed in 0.68s`.

### RED: exact plan-bound mappings

Command:

```powershell
$env:PYTHONPATH='src'; & '<repo-root>\.venv\Scripts\python.exe' -m pytest tests/rescue/test_verification.py::test_local_improvement_rejects_omitted_or_reordered_retained_ranges tests/rescue/test_verification.py::test_verifier_rejects_omitted_or_reordered_plan_retained_ranges -q
```

Result: `3 failed, 1 passed in 2.64s`. Ordinary luma admission accepted omitted and reordered plan ranges; the verifier accepted an omitted but self-consistent mapping.

### GREEN: exact plan-bound mappings and declared-deletion regressions

Command selected the two new parametrized tests plus the retained-reference/deletion regressions. Result: `7 passed in 0.64s`.

### RED/GREEN: core publication cutoff

Command:

```powershell
$env:PYTHONPATH='src'; & '<repo-root>\.venv\Scripts\python.exe' -m pytest tests/rescue/test_pipeline.py::test_cancellation_after_atomic_publication_returns_verified_result -q
```

RED: `1 failed in 3.84s`, raising `RescueCancelledError` after the publisher returned.
GREEN: `1 passed in 0.62s`.

### RED/GREEN: Web execute-return cutoff

Command:

```powershell
$env:PYTHONPATH='src'; & '<repo-root>\.venv\Scripts\python.exe' -m pytest tests/web/test_rescue_api.py::test_cancel_after_core_execute_return_terminalizes_published_result -q
```

RED: `1 failed in 3.25s`; the post-execute barrier was never reached because no explicit acceptance boundary existed.
GREEN: `1 passed in 0.94s`.

## Additional verification

- Selected Rescue/Web suite: `269 passed, 12 skipped in 12.68s`.
- Complete Rescue plus Web Rescue/API suite: `453 passed, 53 skipped in 19.23s`.
- Ruff check: `All checks passed!`.
- Ruff format after formatting: `13 files already formatted`.
- Scoped mypy: non-zero only at `numpy\__init__.pyi:737` because a Python 3.12 `type` statement is parsed under the repository's Python 3.11 mypy target.

### Native FFmpeg gates

The repository's preferred FFmpeg path was absent. The locally installed FFmpeg/FFprobe pair under the Windows package directory was used with explicit PATH injection; no installation or download occurred.

- First forced fixture generation: 25 fixture videos generated and validated.
- Native selected Rescue gate: `123 passed in 210.22s (0:03:30)`.
- Second forced fixture generation: 25 fixture videos generated and validated.
- Fixture-only gate: `21 passed in 187.72s (0:03:07)`.
- An earlier 180-second selected-suite invocation timed out with exit 124 before emitting a test result and was not counted as evidence.
- Decoder diagnostics such as `header damaged` came from deliberately corrupted fixtures and accompanied passing tests.

## Required unified validation

Exactly one repository-wide command was launched after the final code change:

```powershell
$env:PYTHONPATH='src'; $env:PATH='<local-ffmpeg-bin>;' + $env:PATH; & '<repo-root>\.venv\Scripts\python.exe' scripts/validate.py
```

Results:

- `ruff check`: passed — `All checks passed!`
- `ruff format --check`: passed — `285 files already formatted`
- `mypy`: failed only with the accepted external mismatch:
  - `<repo-root>\.venv\Lib\site-packages\numpy\__init__.pyi:737: error: Type statement is only supported in Python 3.12 and greater [syntax]`
- `pytest`: passed — `1258 passed, 16 skipped in 409.98s (0:06:49)`
- Overall process: exit 1 solely because of that mypy/NumPy version mismatch.

## Files changed

Production:

- `src/videoscope/rescue/capabilities.py`
- `src/videoscope/rescue/commands.py`
- `src/videoscope/rescue/pipeline.py`
- `src/videoscope/rescue/timeline.py`
- `src/videoscope/rescue/verification.py`
- `src/videoscope/rescue/visual.py`
- `src/videoscope/web/rescue_jobs.py`

Tests:

- `tests/rescue/test_commands.py`
- `tests/rescue/test_executor.py`
- `tests/rescue/test_pipeline.py`
- `tests/rescue/test_stabilization.py`
- `tests/rescue/test_verification.py`
- `tests/web/test_rescue_api.py`

Report:

- `.superpowers/sdd/2026-08-05-final-rescue-safety-fidelity-correction/final-fix-report.md`

## Remaining concern and external actions

- The only remaining validation issue is the pre-existing external NumPy stub/Python-target mismatch described above; all 1,258 repository tests passed.
- No external access occurred.
- No models or runtime assets were downloaded.
- One authorized local implementation commit was created. No push, PR, tag, release, publication, or deployment occurred.
