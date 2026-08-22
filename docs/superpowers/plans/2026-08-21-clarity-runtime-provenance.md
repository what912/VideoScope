# Clarity Runtime Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the blocked clarity exact-node AST interpreter with a strict runtime provenance guard that proves the real production qualification, execution, verification, cleanup, source-integrity, and tool-identity chain before any native result can be accepted.

**Architecture:** An exact-node-only pytest fixture installs a code-object observer and owns a private no-clobber audit root below that node's `tmp_path`. The selector seals live production objects into a strict canonical `ClarityRuntimeProvenanceV1`; observer events, tool versions, object identities, domain digests, final metrics, cleanup, and publication absence must all agree or the test fails closed. This is test/audit-only and does not alter production algorithms, thresholds, schemas, or publication behavior.

**Tech Stack:** Python 3.12, pytest 9, Pydantic v2, `sys.setprofile`, `pathlib`, `hashlib`, canonical UTF-8 JSON, existing VideoScope Rescue models and validators.

**Spec:** `docs/superpowers/specs/2026-08-21-clarity-runtime-provenance-design.md`

## Global Constraints

- Work only in `C:\Users\吴少泽\Documents\VideoScope\.worktrees\v0.8-stable-release` and preserve all unrelated dirty/untracked changes.
- Read `AGENTS.md`, `docs/product-spec.md`, `docs/architecture.md`, `docs/roadmap.md`, and `docs/report-schema.md` before edits.
- Do not change the SHARPEN algorithm, qualification profiles, final thresholds, `SharpenQualificationEvidenceV1`, `SharpenVerificationControlRecipeV1`, production pipeline, publisher, or public Rescue/report schemas.
- Do not add a production callback, telemetry API, dependency, network access, model/GPU path, or remote service.
- The provenance implementation stays under `tests/`; no file under `src/videoscope/rescue/` may change unless implementation proves the approved design impossible and stops for a new design ruling.
- The exact native selector remains unrun until implementation plus independent review returns 0 Critical and 0 Important and the user separately authorizes one run.
- Do not run FFmpeg/ffprobe, native/real-media tests, `scripts/validate.py`, PREPARE, confirmation, execute, Task 8, publication, or network access during Tasks 1-4.
- Do not run `git commit`, `git push`, create/merge a PR, publish, reset, clean, checkout-overwrite, or discard existing changes.
- All subprocess calls use argument arrays, `shell=False`, bounded timeouts, checked return codes, and sanitized errors.
- Persisted provenance is strict, frozen, path-free, finite, canonical UTF-8 JSON and contains no absolute path, username, stderr, exception text, Python object ID, or temporary filename.
- Successful native provenance requires FFmpeg and ffprobe semantic version `8.1.2`; no fallback version is accepted.
- The audit directory is always `tmp_path / "clarity-runtime-provenance"`; there is no environment-variable path override.
- A pre-existing audit root is never deleted or modified. Ownership is recorded only after exclusive directory creation succeeds.
- One task implementer writes its report under `.superpowers/sdd/2026-08-21-clarity-runtime-provenance/`; a separate reviewer gates each task. Reports replace commits because repository rules prohibit unrequested Git commits.

---

## File Structure

- Create `tests/rescue/clarity_runtime_provenance.py`: strict private models, canonical serialization, no-clobber writer, tool identity verifier, code-object observer, live-object validator, and guard state machine.
- Modify `tests/conftest.py`: exact-node-only autouse fixture and call-phase outcome capture; unrelated tests retain only the existing network guard.
- Modify `tests/rescue/test_fixture_rescue.py`: bind fixed tool/source inputs and seal live clarity objects after existing cleanup and no-publication assertions.
- Rewrite `tests/rescue/test_v15_clarity_node_contract.py`: remove the Python AST/CFG interpreter and replace it with model, writer, observer, anti-fake, live-binding, tool, and fixture-guard tests.
- Modify `.superpowers/sdd/2026-08-21-v15-bounded-qualification-plan/task-7-clarity-node-report.md`: append the redesign RED/GREEN evidence and remaining native gate.
- Modify `.superpowers/sdd/2026-08-21-v15-bounded-qualification-plan/task-3-report.md`: replace the blocked static-contract status with the reviewed runtime-provenance status.
- Modify `.superpowers/sdd/2026-08-21-v15-bounded-qualification-plan/progress.md`: record task/review outcomes, deferred M1/M2 disposition, and native authorization state.

---

### Task 1: Strict Envelope, Canonical Event Chain, and No-Clobber Writer

**Files:**
- Create: `tests/rescue/clarity_runtime_provenance.py`
- Rewrite incrementally: `tests/rescue/test_v15_clarity_node_contract.py`
- Create: `.superpowers/sdd/2026-08-21-clarity-runtime-provenance/task-1-report.md`

**Interfaces:**
- Consumes: Pydantic v2, `JsonValue` and SHA-256/canonical conventions already used by Rescue tests.
- Produces:
  - `ClarityEventInput(phase, component, outcome, stable_input_digest, stable_output_digest)`
  - `ClarityRuntimeProvenanceV1`
  - `ClarityRuntimeEventV1`
  - `ClarityToolIdentityV1`
  - `ClarityErrorV1`
  - `canonical_provenance_bytes(value: BaseModel | Mapping[str, object]) -> bytes`
  - `provenance_digest(value: BaseModel | Mapping[str, object]) -> str`
  - `build_event_chain(events: Sequence[ClarityEventInput]) -> tuple[ClarityRuntimeEventV1, ...]`
  - `write_clarity_runtime_provenance(root: Path, envelope: ClarityRuntimeProvenanceV1) -> Path`
  - `read_clarity_runtime_provenance(path: Path) -> ClarityRuntimeProvenanceV1`

- [ ] **Step 1: Write the failing strict-model tests**

Remove the current AST evaluator/CFG implementation and its exception-alias matrix. Add tests equivalent to:

```python
def test_clarity_runtime_provenance_round_trip_is_canonical_and_path_free(
    tmp_path: Path,
) -> None:
    envelope = _valid_passed_envelope()
    payload = canonical_provenance_bytes(envelope)
    assert payload.endswith(b"\n")
    assert b"C:\\" not in payload
    assert b"/tmp/" not in payload
    path = write_clarity_runtime_provenance(tmp_path / "audit", envelope)
    assert read_clarity_runtime_provenance(path) == envelope


@pytest.mark.parametrize(
    "mutation",
    [
        _remove_required_event,
        _duplicate_event,
        _reorder_events,
        _break_previous_hash,
        _change_event_payload_without_rehash,
        _change_envelope_without_rehash,
    ],
)
def test_clarity_runtime_provenance_rejects_event_or_digest_tamper(mutation) -> None:
    with pytest.raises(ValueError):
        ClarityRuntimeProvenanceV1.model_validate(mutation(_valid_payload()))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clarity_runtime_provenance_rejects_nonfinite_metrics(value: float) -> None:
    payload = _valid_payload()
    payload["verification"]["minimum_aggregate_gain_ratio"] = value
    with pytest.raises(ValueError):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_clarity_runtime_provenance_preserves_signed_zero_in_digest() -> None:
    assert provenance_digest(_valid_payload(metric_override=0.0)) != provenance_digest(
        _valid_payload(metric_override=-0.0)
    )
```

Parameterize path-bearing strings under arbitrary nested keys with absolute Windows/POSIX, UNC, `../`, `foo/bar`, `foo\\bar`, `file:`, and `https:` values. All must fail before writing.

- [ ] **Step 2: Run the model tests and capture genuine RED**

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest `
  tests/rescue/test_v15_clarity_node_contract.py `
  -k 'provenance and (canonical or tamper or nonfinite or signed_zero or path)' -q
```

Expected: collection/import failures for the missing approved provenance module or symbols. Record only missing-behavior failures as genuine RED.

- [ ] **Step 3: Implement strict frozen models and outcome invariants**

The core shapes must be equivalent to:

```python
class ClarityRuntimeEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: int = Field(ge=0, strict=True)
    phase: Literal[
        "tool_identity_verified",
        "draft_bound",
        "qualification_returned",
        "qualification_cleanup_verified",
        "final_plan_bound",
        "faithful_returned",
        "improved_returned",
        "verification_returned",
        "controls_cleanup_returned",
        "source_integrity_verified",
        "publication_absence_verified",
    ]
    component: str = Field(min_length=1)
    outcome: Literal["returned", "verified"]
    stable_input_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    stable_output_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    previous_event_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    event_sha256: str = Field(pattern=SHA256_PATTERN)


class ClarityRuntimeProvenanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["1"] = "1"
    track: Literal["sharpen_clarity"] = "sharpen_clarity"
    producer_version: Literal["clarity_runtime_provenance_v1"]
    selector_id: Literal["clarity_exact_native_v1"]
    outcome: Literal["passed", "no_profile_passed", "cancelled", "error"]
    component_manifest: tuple[ClarityComponentIdentityV1, ...]
    tools: tuple[ClarityToolIdentityV1, ...]
    source: ClaritySourceProjectionV1 | None
    draft: ClarityPlanProjectionV1 | None
    qualification: ClarityQualificationProjectionV1 | None
    final: ClarityFinalProjectionV1 | None
    runtime_recipe: ClarityRuntimeRecipeProjectionV1 | None
    verification: ClarityVerificationProjectionV1 | None
    cleanup: ClarityCleanupProjectionV1
    events: tuple[ClarityRuntimeEventV1, ...]
    events_digest: str = Field(pattern=SHA256_PATTERN)
    error: ClarityErrorV1 | None
    envelope_digest: str = Field(pattern=SHA256_PATTERN)
```

Define explicit strict projection models for every referenced type. `passed` requires every section and null error; `no_profile_passed` requires source/draft/qualification and forbids final/recipe/verification; cancelled/error require a stable error and reject a null section after its successful event.

The projection field inventory is fixed:

- component: `module`, `qualname`, `source_sha256`;
- tool: `role`, `binary_sha256`, `reported_version_line`,
  `version_stdout_sha256`, `semantic_version`;
- source: `sha256_before`, `sha256_after`, `size_bytes`;
- plan: `input_hash`, `plan_digest`, `action_id`, `config_digest`,
  `encode_contract_digest`, and `source_ranges`;
- qualification: `evidence_digest`, `profile_order`, `selected_profile_id`,
  `selected_identity_digest`, `selected_metrics_digest`;
- final: `plan_digest`, `action_id`, `source_mappings_digest`,
  `output_ranges_digest`, `faithful_sha256`, `improved_sha256`;
- recipe: `recipe_digest`, three generation hashes, normalized PTS digest,
  topology digest, inventory count, and range digests;
- verification: `report_digest`, required check ID/status, two literal-true
  binding flags, four integer metrics, eight finite float metrics, and their
  aggregate digest;
- cleanup: `qualification_root_absent`, `control_count`, `controls_absent`,
  `source_unchanged`, and `public_outputs_absent`;
- error: stable `phase` and `code` only.

- [ ] **Step 4: Implement canonical bytes, event hashes, and envelope digest**

Use one internal serializer with `ensure_ascii=False`, `sort_keys=True`, `allow_nan=False`, separators `(",", ":")`, UTF-8, and one final LF. Validate all content as path-free before hashing. Event hashes exclude `event_sha256`; envelope hash excludes `envelope_digest`. Validators independently recompute both.

- [ ] **Step 5: Implement the owned no-clobber writer**

Require the target root absent; create it with `exist_ok=False`; set `owned=True` only after success; exclusively create `clarity-runtime-provenance.json.partial`; atomically rename to `clarity-runtime-provenance.json`; read back through the strict model and compare canonical bytes. On failure remove only an owned root without following links. Preserve pre-existing directory/file/symlink roots and sentinels byte-for-byte.

- [ ] **Step 6: Add no-clobber and ownership tests**

Cover pre-existing directory, file, symlink-like path, partial-file collision, write/rename/readback failures, path escape, Unicode/spaces, and injected cleanup failure while removing an owned partial root. The cleanup failure must remain visible and must not delete external state.

- [ ] **Step 7: Run Task 1 GREEN and static checks**

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/rescue/test_v15_clarity_node_contract.py -k 'provenance or event or no_clobber or ownership' -q
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m ruff check tests/rescue/clarity_runtime_provenance.py tests/rescue/test_v15_clarity_node_contract.py
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m ruff format --check tests/rescue/clarity_runtime_provenance.py tests/rescue/test_v15_clarity_node_contract.py
```

- [ ] **Step 8: Write the Task 1 report**

Record changed files, exact RED/GREEN commands and output, model/writer invariants, unrun native/unified commands, risks, and no network/Git/publication. Do not commit.

---

### Task 2: Exact Code-Object Observer and Pytest Guard

**Files:**
- Modify: `tests/rescue/clarity_runtime_provenance.py`
- Modify: `tests/conftest.py`
- Modify: `tests/rescue/test_v15_clarity_node_contract.py`
- Create: `.superpowers/sdd/2026-08-21-clarity-runtime-provenance/task-2-report.md`

**Interfaces:**
- Consumes: Task 1 models, event chain, and writer.
- Produces `EXACT_CLARITY_NODE_ID`, `ProductionComponentSpec`, `ClarityObservedReturn`, `ClarityRuntimeObserver.start/stop/require_intact/require_return`, `ClarityRuntimeGuard.finalize_from_pytest_item(item: pytest.Item) -> None`, and exact-node-only fixture `clarity_runtime_provenance_guard`.

- [ ] **Step 1: Write observer and finalizer RED tests**

Add these exact behaviors:

```python
def _observed_component(value: object) -> object:
    return value


def test_observer_accepts_exact_code_object_and_return_identity() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    observer.start()
    try:
        returned = _observed_component(expected)
        observer.require_intact()
    finally:
        observer.stop()
    observer.require_return("observed", returned)


def test_observer_rejects_same_name_fake() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )

    def _same_name(value: object) -> object:
        return value

    expected = object()
    observer.start()
    try:
        _same_name(expected)
    finally:
        observer.stop()
    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", expected)


def test_observer_does_not_record_dead_branch() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    observer.start()
    try:
        if False:
            _observed_component(expected)
    finally:
        observer.stop()
    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", expected)


def test_observer_rejects_disabled_profile_hook() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    observer.start()
    sys.setprofile(None)
    try:
        with pytest.raises(ValueError, match="observer was replaced"):
            observer.require_intact()
    finally:
        observer.stop()
```

Add separate concrete tests for subclass override, early return, previous-hook chaining/restoration, missing/duplicate seal, sanitized partial error, and unrelated-node isolation. Each test must assert the exact failure or restoration state; no test may accept a generic exception.

Use same-name functions with different code objects, a subclass override, and a monkeypatched production method after registry creation.

- [ ] **Step 2: Run observer tests and capture genuine RED**

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/rescue/test_v15_clarity_node_contract.py -k 'observer or code_object or finalizer or profile_hook or non_exact_node' -q
```

- [ ] **Step 3: Implement the exact production registry**

Retain code objects before the selector runs:

```python
DEFAULT_COMPONENTS = (
    component("build_rescue_plan", build_rescue_plan),
    component("qualify", NativeRescueCandidateQualifier.qualify),
    component("execute_faithful", NativeRescueExecutor.execute_faithful),
    component(
        "execute_improved_with_controls",
        NativeRescueExecutor.execute_improved_with_controls,
    ),
    component("verify", RescueVerifier.verify),
    component("cleanup_controls", _cleanup_verification_controls),
)
```

Reject duplicate code objects/names. Persist only module, qualname, and source SHA-256, never source paths.

- [ ] **Step 4: Implement the profile observer**

Capture `sys.getprofile()`, install one dispatcher, chain the previous hook, and record the owning thread. Filter `call`/`return` by exact code object. Keep object IDs in memory only. Require two `build_rescue_plan` returns matching draft/final and one successful return for each other milestone. Duplicate, missing, reordered, cross-thread, fake, or substituted returns fail.

- [ ] **Step 5: Add exact-node-only pytest lifecycle plumbing**

Keep the network guard unchanged. Add a call-report stash and fixture equivalent to:

```python
@pytest.fixture(autouse=True)
def clarity_runtime_provenance_guard(request: pytest.FixtureRequest):
    if request.node.nodeid != EXACT_CLARITY_NODE_ID:
        yield None
        return
    tmp_path = request.getfixturevalue("tmp_path")
    guard = ClarityRuntimeGuard(tmp_path / "clarity-runtime-provenance")
    guard.start()
    try:
        yield guard
    finally:
        guard.finalize_from_pytest_item(request.node)
```

Use `pytest_runtest_makereport` to store only outcome and exception type for sanitized partial evidence. Always restore the prior profile hook.

- [ ] **Step 6: Run Task 2 GREEN and unrelated-test isolation**

Run the observer selection, existing network fixture tests, and one unrelated Rescue unit test. Assert unrelated tests create no audit root and install no profile hook.

- [ ] **Step 7: Run scoped Ruff/format/mypy**

Use full pytest hook/fixture annotations and no blanket ignores.

- [ ] **Step 8: Write the Task 2 report**

Record exact RED/GREEN evidence, hook restoration, isolation, risks, files, and prohibited commands not run. Do not commit.

---

### Task 3: Tool Identity and Live Production Object Validator

**Files:**
- Modify: `tests/rescue/clarity_runtime_provenance.py`
- Modify: `tests/rescue/test_v15_clarity_node_contract.py`
- Create: `.superpowers/sdd/2026-08-21-clarity-runtime-provenance/task-3-report.md`

**Interfaces:**
- Consumes: Task 1 models/writer and Task 2 observer/guard.
- Produces:
  - `CommandRunner = Callable[..., subprocess.CompletedProcess[str]]`
  - `verify_clarity_tool_identity(path: Path, role: Literal["ffmpeg", "ffprobe"], *, runner: CommandRunner = subprocess.run) -> ClarityToolIdentityV1`
  - `ClarityRuntimeGuard.bind_tools(ffmpeg: Path, ffprobe: Path) -> None`
  - `ClarityRuntimeGuard.bind_source_before(source: Path, sha256: str) -> None`
  - `ClarityRuntimeGuard.seal_success` with the exact signature below.
  - `ClarityRuntimeGuard.seal_no_profile` with the exact signature below.

```python
def seal_success(
    self,
    *,
    source: Path,
    source_sha256_after: str,
    draft: RescuePlan,
    evidence: SharpenQualificationEvidenceV1,
    final: RescuePlan,
    faithful: RescueExecutionResult,
    improved: RescueImprovedExecutionResult,
    controls: tuple[SharpenVerificationControlHandle, ...],
    report: RescueVerificationReport,
    qualification_root: Path,
    execution_root: Path,
) -> ClarityRuntimeProvenanceV1:
    """Validate and persist one successful live clarity chain."""


def seal_no_profile(
    self,
    *,
    source: Path,
    source_sha256_after: str,
    draft: RescuePlan,
    evidence: SharpenQualificationEvidenceV1,
    qualification_root: Path,
    execution_root: Path,
) -> ClarityRuntimeProvenanceV1:
    """Persist an honest no-profile outcome without executing final media."""
```

- [ ] **Step 1: Write fake-runner tool identity RED tests**

Cover valid FFmpeg/ffprobe 8.1.2, binary hash, full stdout digest, missing executable, nonzero exit, timeout, wrong role, malformed line, duplicate role, versions 8.1.1/8.2, and stderr/path sanitization. Do not execute a real tool.

- [ ] **Step 2: Write live-object binding RED tests**

Build valid production plans/evidence/execution dataclasses/handle/report with existing fake helpers. One exact positive must seal and read back. Independent negatives change plan/action identity, profile/order/ranges/encode contract, mappings, three media hashes, PTS/topology/count, four integer metrics, eight float metrics, binding flags, source hash/size, cleanup/public output, or substitute a subclass/model copy/prebuilt JSON. Each must fail before retaining a passed envelope.

- [ ] **Step 3: Run Task 3 RED tests**

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest tests/rescue/test_v15_clarity_node_contract.py -k 'tool_identity or live_object or seal or selected_binding or publication_absence' -q
```

- [ ] **Step 4: Implement fixed tool verification**

Call `[str(path), "-version"]` with `shell=False`, bounded timeout, captured bounded text output, and checked return code. Parse the semantic token after the correct role prefix and require `8.1.2`. Persist normalized first line, binary SHA, and complete bounded stdout SHA, but no executable path or stderr.

- [ ] **Step 5: Implement one-shot tool/source bindings**

`bind_tools` must precede observed production milestones. `bind_source_before` requires a regular non-symlink file, validates digest/size, and precedes draft return. Rebinding fails.

- [ ] **Step 6: Implement `seal_success`**

Require observer integrity and live return identity; invoke existing strict validators; rederive plans/actions/mappings; bind recipe, files, selected profile and complete metric vector using existing `rel_tol=0`, `abs_tol=1e-9`; require source unchanged, qualification/control cleanup, and absence of `rescue-output`, report JSON, and HTML; build/write/read back one passed envelope; mark sealed only after readback.

- [ ] **Step 7: Implement `seal_no_profile` and partial finalization**

Require `selected is None`, qualification cleanup, source unchanged, and no final/executor/verifier return. Write `no_profile_passed`, then let the exact test fail with the existing limitation. Missing seal writes sanitized `cancelled` only for `RescueCancelledError`, otherwise `error`; raw exception text is forbidden.

- [ ] **Step 8: Run Task 3 GREEN and existing selected-binding regression**

Run new selections plus the existing one-positive/fourteen-drift executor/verifier tests. Do not run native media.

- [ ] **Step 9: Run Ruff/format/full mypy**

Use a fresh no-clobber mypy cache under `.release-audit`; if it exists, choose a new explicit path rather than deleting it.

- [ ] **Step 10: Write the Task 3 report**

Record fake tool evidence, live identity/drift matrix, selected-binding regression, static outputs, files, unrun commands, and native gate. Do not commit.

---

### Task 4: Exact Selector Integration, AST Removal, and Review Gate

**Files:**
- Modify: `tests/rescue/test_fixture_rescue.py`
- Finalize: `tests/rescue/test_v15_clarity_node_contract.py`
- Modify: `.superpowers/sdd/2026-08-21-v15-bounded-qualification-plan/task-7-clarity-node-report.md`
- Modify: `.superpowers/sdd/2026-08-21-v15-bounded-qualification-plan/task-3-report.md`
- Modify: `.superpowers/sdd/2026-08-21-v15-bounded-qualification-plan/progress.md`
- Create: `.superpowers/sdd/2026-08-21-clarity-runtime-provenance/task-4-report.md`

**Interfaces:**
- Consumes: `ClarityRuntimeGuard` and all Task 1-3 APIs.
- Produces: one guarded but still unrun exact native selector and complete non-native review evidence.

- [ ] **Step 1: Add a static collection RED for fixture binding**

Import `tests.rescue.test_fixture_rescue`, obtain the exact function by name, and assert its signature includes `clarity_runtime_provenance_guard`. Assert `EXACT_CLARITY_NODE_ID` matches module/function identity. Do not interpret the function body.

- [ ] **Step 2: Integrate the guard into the exact selector**

```python
def test_native_fixed_8_1_2_soft_detail_qualification_matches_final_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clarity_runtime_provenance_guard: ClarityRuntimeGuard,
) -> None:
```

After `_activate_fixed_native_tools`, call `bind_tools`; after original source hash, call `bind_source_before`. Preserve all existing production-chain and threshold assertions. On no-profile, call `seal_no_profile` before the existing `pytest.fail`. On success call `seal_success` only after cleanup, source integrity, and no-publication assertions; assert the returned envelope is passed and retained. Never catch guard failure or convert it to skip/xfail.

- [ ] **Step 3: Delete AST/CFG code completely**

Remove `ast` imports, evaluator, binding collectors, reachability/exception interpreter, source-string fixtures, and five-round alias/handler tests. Require this command to return no matches:

```powershell
rg -n "ast\.|_ReachableNodeCollector|_safe_constant_value|_raised_exception_type|TYPE_CHECKING aliases" tests/rescue/test_v15_clarity_node_contract.py
```

- [ ] **Step 4: Run focused non-native/fake tests only**

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m pytest `
  tests/rescue/test_v15_clarity_node_contract.py `
  tests/rescue/test_qualification.py tests/rescue/test_planner.py `
  tests/rescue/test_commands.py tests/rescue/test_preview.py `
  tests/rescue/test_executor.py tests/rescue/test_pipeline.py `
  tests/rescue/test_verification.py `
  tests/rescue/test_v15_qualification_integration.py `
  -k 'not native and not real and not ffmpeg' -q
```

- [ ] **Step 5: Run final static verification**

```powershell
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m ruff check src tests
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m ruff format --check src tests
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m mypy --no-incremental src tests
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' -m py_compile tests/rescue/clarity_runtime_provenance.py tests/rescue/test_v15_clarity_node_contract.py tests/rescue/test_fixture_rescue.py tests/conftest.py
```

- [ ] **Step 6: Write closure reports**

State exact files, RED/GREEN evidence, final counts, AST removal, runtime trust root, unchanged production/thresholds, exact selector still unrun, and no native/FFmpeg/unified/PREPARE/execute/network/Git/publication.

- [ ] **Step 7: Independent review gate**

Dispatch a fresh reviewer with the spec, plan, four task reports, current files, and previous clarity rereview5. Require formal review of code identity, live-object identity, event chain, no-clobber/path safety, 8.1.2 tool contract, failure outcomes, AST removal, unchanged production, and native-unrun state. Only 0 Critical/0 Important permits a user authorization request; findings enter a new maximum-five-round fix loop for this design task.

- [ ] **Step 8: Stop at native authorization**

After a clean review, report the exact selector, fixed tools, fresh no-clobber `--basetemp`, envelope location/schema, and failure-no-retry rule. Do not run it without a new explicit user authorization.

---

## Final Verification and Handoff

After Tasks 1-4 and clean review:

1. Confirm no Python/pytest process remains.
2. Recompute and record the current `src`/`tests`/`pyproject.toml` aggregate with the Task 7 ordinal algorithm.
3. Confirm the exact selector never appeared in implementation pytest output.
4. Present a single-run clarity native authorization request; do not bundle STABILIZE or unified validation.
5. If later native passes, require a separate retained-evidence review before clarity becomes approved.
6. Optional STABILIZE remains a separate blocked track.
