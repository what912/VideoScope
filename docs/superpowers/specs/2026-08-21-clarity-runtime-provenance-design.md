# Clarity Exact Native Runtime Provenance Design

Status: proposed for implementation after user review

Date: 2026-08-21

Scope: V15 bounded SHARPEN clarity exact-native qualification gate only

## 1. Context

The clarity exact-native selector already uses the real production chain:

```text
draft RescuePlan
  -> NativeRescueCandidateQualifier
  -> strict SharpenQualificationEvidenceV1
  -> final RescuePlan
  -> NativeRescueExecutor faithful execution
  -> NativeRescueExecutor improved execution with same-generation controls
  -> RescueVerifier final measurement
  -> confined cleanup
```

The production executor and verifier already bind the runtime baseline,
visibility control, candidate, normalized PTS inventory, stream topology,
frame counts, retained ranges, and complete clarity metric vector to the
selected qualification profile. The remaining Task 7 blocker is not a media,
algorithm, threshold, or production trust-boundary defect. It is the
test-only static AST validator used to prove that the exact selector reaches
the production chain.

After five bounded fix rounds, that validator still misclassifies reachable
and unreachable Python control flow involving exception aliases, handler
ordering, and bare re-raise semantics. Extending it further would amount to
building an incomplete Python interpreter and would not prove what actually
ran.

## 2. Decision

Replace the static AST/CFG anti-fake validator with a test/audit-only runtime
observer and live-object validator. The observer records calls to exact
production code objects during the exact selector. The validator seals those
observations together with the live domain objects and media/tool identities
into a strict private provenance envelope.

This design does not change:

- the SHARPEN algorithm or any qualification/final threshold;
- `SharpenQualificationEvidenceV1`;
- `SharpenVerificationControlRecipeV1`;
- qualification, planner, executor, verifier, pipeline, or publisher behavior;
- the public Rescue report schema or public artifact inventory;
- the source media or existing cleanup rules;
- the one-run, explicit-user-authorization native gate.

## 3. Alternatives Considered

### 3.1 Continue extending the AST interpreter

Rejected. It has no finite completeness boundary, validates source shape
rather than execution, and does not solve durable tool/version or verifier
evidence retention.

### 3.2 Add provenance callbacks to production APIs

Rejected for this task. A sink or callback threaded through qualification,
executor, verifier, and pipeline would widen production APIs for a single
native audit concern. A same-process callback would still need an independent
trust root and would add product integration risk without changing the
underlying media result.

### 3.3 Runtime observer plus live-object validator

Selected. It observes the real production code objects, requires the real
returned objects, produces a durable machine-readable envelope, and remains
isolated from public product contracts.

## 4. Trust Model and Limitations

The trust root consists of:

1. the conditional pytest guard in `tests/conftest.py`;
2. the runtime observer and validator in
   `tests/rescue/clarity_runtime_provenance.py`;
3. independent review of those files and the exact selector;
4. the fresh no-clobber audit root supplied for an authorized native run.

The observer proves that the current Python process executed the expected
production code objects and returned the live objects later sealed by the
validator. It does not provide cryptographic remote attestation against an
attacker who can modify the test harness, interpreter, or repository before
the run. Repository aggregate hashes and independent code review remain the
authority for that boundary.

A single envelope proves one invocation inside its acquired no-clobber audit
root. It cannot prove that no invocation occurred in a different directory.
The controller and user authorization continue to enforce the global
single-run/no-retry policy.

## 5. Components

### 5.1 Conditional pytest guard

An autouse fixture activates only for the fully qualified exact selector:

```text
tests/rescue/test_fixture_rescue.py::
test_native_fixed_8_1_2_soft_detail_qualification_matches_final_verifier
```

For every other test it performs no filesystem writes, profiling, tool
probing, or environment inspection.

For the exact selector it:

- derives `clarity-runtime-provenance/` below that selector's pytest
  `tmp_path`, whose parent is controlled by the authorized fresh
  `--basetemp`, and acquires the absent subdirectory with no-clobber semantics;
- installs the observer before the test body;
- preserves and chains any pre-existing profile hook;
- records the owning thread and rejects expected component calls from another
  thread;
- verifies at finalization that the observer was not replaced or disabled;
- requires exactly one terminal seal;
- writes a sanitized partial failure envelope if setup succeeded but the test
  exits without a valid terminal seal;
- restores the previous profile hook in all outcomes.

The exact selector remains single-threaded. A future threaded implementation
must introduce an explicit observer design change rather than silently
weakening the current gate.

### 5.2 Runtime observer

The observer filters Python profile events by exact code-object identity, not
function name. It observes the following ordered production milestones:

1. draft `build_rescue_plan` returned;
2. `NativeRescueCandidateQualifier.qualify` returned;
3. final `build_rescue_plan` returned;
4. `NativeRescueExecutor.execute_faithful` returned;
5. `NativeRescueExecutor.execute_improved_with_controls` returned;
6. `RescueVerifier.verify` returned;
7. `_cleanup_verification_controls` returned.

The observer keeps Python object identities in memory only. It records stable
domain or content digests in the persisted event ledger. A dead branch, early
return, empty selector, local same-name helper, subclass, monkeypatched fake,
or prebuilt JSON cannot synthesize events for the exact production code
objects.

Repeated or reordered terminal milestones fail closed. Internal qualifier
calls that legitimately repeat candidate rendering are not terminal
milestones and do not affect the required sequence.

### 5.3 Live-object validator and seal

The exact selector receives the guard fixture and calls `seal_success` only
after final verification, cleanup, source-integrity, and publication-absence
assertions succeed. It passes live references to:

- source path and before/after source hashes;
- fixed FFmpeg and ffprobe paths;
- draft plan and draft SHARPEN action;
- qualification evidence;
- final plan and final SHARPEN action;
- faithful execution result;
- improved execution result and runtime controls;
- final verification report and required SHARPEN check;
- qualification and execution roots.

The validator requires exact concrete production types where a concrete type
exists. It rejects subclasses and structurally similar fakes at the audit
boundary. It matches each sealed live object by identity to the corresponding
observer return event.

It independently re-runs existing strict, non-media contracts and verifies:

- plan digests and action IDs;
- selected profile, profile order, ranges, and encode contract;
- selected qualification identity and metric digest;
- source mappings and exact output-range mapping;
- runtime recipe plan/action/range/encode binding;
- baseline, visibility-control, candidate, PTS, topology, and frame-count
  identity;
- final check status and complete metric-vector equality using the existing
  `rel_tol=0`, `abs_tol=1e-9` semantics;
- source hash and size unchanged;
- qualification root and runtime control cleanup completed;
- no public Rescue output, HTML, or report was created.

`seal_no_profile` is a distinct terminal API. It accepts the live draft and
qualification evidence, requires `selected is None`, proves qualification
cleanup and source integrity, persists an honest `no_profile_passed`
envelope, and still causes the exact native gate to fail. It never executes
the final planner, executor, verifier, or publisher stages.

### 5.4 Tool identity verifier

The guard invokes fixed FFmpeg and ffprobe with argument arrays,
`shell=False`, a bounded timeout, and sanitized output handling. For each tool
it records:

- role (`ffmpeg` or `ffprobe`);
- binary SHA-256;
- the normalized first version line;
- SHA-256 of the complete bounded version stdout;
- the parsed version, which must equal `8.1.2`.

Missing tools, nonzero exits, timeouts, malformed output, version drift, or
duplicate roles fail before media qualification. Absolute tool paths are
never persisted.

### 5.5 No-clobber canonical writer

The audit root is private test evidence and never enters the public Rescue
tree. The writer:

- requires the target root to be absent;
- creates it once and records ownership only after successful creation;
- writes UTF-8 canonical JSON with sorted keys, unescaped Unicode, LF ending,
  and `allow_nan=False`;
- uses an exclusive temporary file and atomic rename inside the owned root;
- refuses symlinks, path escape, an existing final file, duplicate writes, and
  unsupported filesystem object types;
- reads the final file back through the strict model and recomputes its digest;
- never follows links while cleaning an owned partial root;
- never deletes a root it did not successfully acquire.

There is no environment-variable path override. Standalone native execution
selects retention through pytest's fresh `--basetemp`; ordinary suite
execution receives an isolated pytest temporary root. This keeps the writer's
filesystem authority explicit and prevents an environment value from
redirecting cleanup.

The successful envelope remains for independent review. Qualification media,
baseline, and visibility controls retain their current confined cleanup
behavior and are not copied into this audit root.

## 6. Provenance Envelope

`ClarityRuntimeProvenanceV1` is a test-private Pydantic model with
`extra="forbid"`, `frozen=True`, strict scalar validation, finite floats, and
path-free canonical JSON.

Required top-level fields:

```text
schema_version: "1"
track: "sharpen_clarity"
producer_version: "clarity_runtime_provenance_v1"
selector_id: "clarity_exact_native_v1"
outcome: passed | no_profile_passed | cancelled | error
component_manifest[]
tools[]
source
draft
qualification
final | null
runtime_recipe | null
verification | null
cleanup
events[]
events_digest
error | null
envelope_digest
```

The persisted model never contains an absolute path, username, stderr, Python
object ID, temporary filename, or exception string.

Outcome-dependent presence is strict:

- `passed` requires every section and `error` must be null;
- `no_profile_passed` requires tools, source, draft, and qualification;
  `final`, `runtime_recipe`, and `verification` must be null;
- `cancelled` and `error` require every section completed before the failure,
  permit later sections to be null, and require a sanitized `error` projection;
- a section may never be null when its corresponding successful event exists.

Finite signed zero is permitted and preserved exactly by canonical JSON:
`0.0` and `-0.0` serialize to different tokens and therefore produce different
digests. Validators compare metric values using the existing numerical
contract, but never normalize either token while serializing evidence.

### 6.1 Component manifest

Each required production component entry contains:

```text
module
qualname
source_sha256
```

The source path used to calculate the hash is not persisted. Duplicate or
missing components fail validation.

### 6.2 Domain projections

The draft, qualification, final, runtime recipe, and verification sections
store only canonical path-free projections and SHA-256 digests of the strict
objects. The qualification projection includes the configured profile order,
selected profile, ranges, encode contract digest, selected generation
identity, and complete selected metric digest. The verification projection
includes the required check ID/status, both binding flags, full measured
metric digest, and report digest.

### 6.3 Event hash chain

Every event contains:

```text
sequence
phase
component
outcome
stable_input_digest | null
stable_output_digest | null
previous_event_sha256 | null
event_sha256
```

The event hash is calculated from the canonical event payload excluding
`event_sha256`. Sequence starts at zero, is contiguous, and the previous hash
must match the prior event. `events_digest` binds the complete ordered list.

For a successful exact selector the semantic order is:

```text
tool_identity_verified
draft_bound
qualification_returned
qualification_cleanup_verified
final_plan_bound
faithful_returned
improved_returned
verification_returned
controls_cleanup_returned
source_integrity_verified
publication_absence_verified
```

The observer supplies production call/return facts; the seal supplies the
post-call integrity facts. Both are required.

### 6.4 Error projection

Failure evidence uses only stable codes and phases:

```text
phase
code
```

No raw exception message, command line, stderr, or path is persisted. Cleanup
failure overrides a prior successful or no-profile outcome and becomes
`error`.

## 7. Failure Semantics

The guard fails closed for:

- absent, duplicate, or reordered required runtime events;
- events from non-production code objects;
- return-object identity mismatch;
- missing, duplicate, or late terminal seal;
- observer replacement or disablement;
- tool version or binary identity drift;
- plan, action, evidence, range, mapping, encode, recipe, PTS, topology,
  inventory, metric, source, or report mismatch;
- prebuilt JSON passed instead of live objects;
- nonfinite, extra, path-bearing, or noncanonical fields;
- no-clobber acquisition, persistence, readback, or digest failure;
- path escape, symlink, unsupported filesystem object, or cleanup failure;
- unexpected public output.

Cancellation is re-raised after a sanitized partial envelope is attempted.
Persistence failure never converts a failed test into a pass. If partial
evidence cannot be written safely, the original failure remains and the
persistence error is reported through a stable secondary code.

## 8. Test Strategy

### 8.1 Pure model and serialization tests

Cover:

- strict valid round trip and deterministic canonical bytes/digest;
- extra fields, missing fields, invalid enums, booleans-as-integers,
  NaN/Infinity, signed zero policy, and invalid hashes;
- path-bearing keys and values, absolute/relative/UNC/URI escapes;
- event sequence, previous hash, missing/duplicate/reordered event, and digest
  tampering;
- no-clobber root/file behavior, symlink refusal, ownership, write failure,
  readback, and cleanup-failure precedence.

### 8.2 Observer and anti-fake tests

Use pure/fake collaborators and pytest fixture tests to prove:

- exact production code-object calls are recorded in order;
- local same-name functions, subclasses, monkeypatch replacements, dead
  branches, empty selectors, and early returns cannot satisfy the guard;
- a prebuilt envelope cannot replace a live seal;
- a missing seal fails in the fixture finalizer;
- temporarily disabling or replacing the profile hook fails;
- duplicate calls and return-object substitution fail;
- a pre-existing profile hook is chained and restored;
- unrelated tests do not install the observer or touch the filesystem.

The former Python CFG/exception AST matrix is removed. It is not retained as
a second source of truth.

### 8.3 Live-object binding tests

Retain the existing fifteen selected-versus-runtime/final cases and extend the
new validator with isolated drift cases for:

- plan/action/evidence identity;
- baseline/control/candidate SHA;
- normalized PTS, topology, and inventory count;
- source/output ranges and mappings;
- all integer and float clarity metrics;
- source hash and size;
- cleanup and publication absence;
- FFmpeg/ffprobe role, binary hash, version, and stdout digest.

One exact positive uses live production domain objects with fake media runners.

### 8.4 Native gate

Implementation and independent review must reach 0 Critical and 0 Important
before requesting native authorization. The later authorized command remains
one exact selector invocation with fixed FFmpeg/ffprobe 8.1.2 and a fresh
no-clobber base/audit root. Failure is not retried.

Native success requires both:

1. pytest reports the exact selector passed; and
2. the retained provenance envelope independently parses, matches the log and
   repository aggregate, records the fixed tools, proves all production
   milestones and bindings, and confirms cleanup/source/publication
   invariants.

No successful provenance envelope authorizes PREPARE, confirmation, execute,
Task 8, publication, network access, or Git operations.

## 9. Files and Ownership

Expected implementation files:

- create `tests/rescue/clarity_runtime_provenance.py` — strict models,
  canonical serialization, event chain, observer, live validator, and
  no-clobber writer;
- modify `tests/conftest.py` — exact-node-only guard installation and forced
  finalization;
- modify `tests/rescue/test_fixture_rescue.py` — accept the guard fixture,
  record tool/source facts, and seal live objects after cleanup checks;
- rewrite `tests/rescue/test_v15_clarity_node_contract.py` — replace the AST
  interpreter with pure runtime provenance, observer, persistence, anti-fake,
  and drift tests;
- update Task 7/Task 3 SDD reports and progress ledger.

Production files under `src/videoscope/rescue/` are intentionally unchanged.
If implementation discovers that the runtime observer cannot prove an
essential fact without changing a production interface, the task stops and
returns to design review rather than widening scope implicitly.

## 10. Acceptance Criteria

The design is implemented only when all of the following hold:

- the static AST/CFG interpreter and its exception-alias matrix are removed;
- the exact selector is guarded by observed production code-object identity;
- live returned objects are identity-bound to observer events and strict
  domain projections;
- a canonical path-free envelope is written exactly once to an owned
  no-clobber private root and passes strict readback;
- actual FFmpeg and ffprobe versions and binary identities are recorded and
  fixed to 8.1.2 for native execution;
- source integrity, cleanup, and public-output absence are machine-readable;
- dead branches, early return, same-name fake, subclass, monkeypatch,
  prebuilt JSON, observer replacement, event drift, object substitution,
  metric drift, tool drift, persistence failure, path escape, and cleanup
  failure all fail closed;
- all existing clarity production-contract and affected non-native tests pass;
- Ruff format/check and full mypy pass;
- an independent review returns 0 Critical and 0 Important;
- no native selector or unified validation runs before separate authorization.
