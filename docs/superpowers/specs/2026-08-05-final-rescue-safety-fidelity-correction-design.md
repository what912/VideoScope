# Final Rescue Safety and Fidelity Correction Design

## Context

The Video Rescue Balanced CPU MVP has completed its original thirteen tasks,
the trustworthy-timeline correction, and one whole-branch fix wave. The final
scoped review confirmed that the Web confirmation gate, exact action-set
confirmation, locked damage removal, action execution ledger, core source
identity pinning, `needs_review` transport, terminal source comparison,
verification labels, CLI terminal outcomes, and deferred documentation issues
were corrected.

The same review found a smaller set of remaining release-blocking gaps:

- some actions can still be proposed when the bounded preview cannot faithfully
  demonstrate them;
- stabilization, deflicker, rotation, and fixed-offset actions are not all
  safely scoped when locks or structural timeline changes are present;
- Conservative fixed-offset output is not natively checked for residual sync;
- native stabilization evidence does not yet prove the missing-crop path on
  real media;
- retained source descriptors can be closed twice or leaked after abandoned
  preparation;
- schema documentation needs an explicit additive-compatibility decision.

Task 13B is an independently reviewed correction. It must pass before the
branch can be described as merge-ready or release-ready.

## Product Rule

An automatic Rescue action is eligible for confirmation only when all four
conditions are true:

1. the bounded preview actually demonstrates that action;
2. execution can honor every locked and authorized time range;
3. a native verifier can evaluate its result, or the output is explicitly
   review-gated rather than called verified;
4. every source and private artifact needed by the action has an explicit,
   idempotent lifetime.

If any condition is false, VideoScope fails closed: the action is omitted from
the default confirmable set and surfaced as `needs_review` with a concrete
reason. It is never silently executed or described as completed.

## Considered Approaches

### 1. Capability-gated execution with fail-closed fallbacks — selected

Introduce an explicit internal capability decision for each proposed action.
Actions that can be previewed, range-scoped, and verified remain automatic.
Actions that cannot satisfy all conditions are retained as diagnostic advice
or review-gated candidates, not executable defaults.

This approach preserves working Conservative and Balanced improvements while
eliminating false consent and false verification. It is also compatible with
the current CPU-only architecture.

### 2. Fully implement segmented stabilization, segmented rotation, and native
crop reconstruction now — rejected

This would retain more automatic actions, but it requires a substantially new
media-composition architecture and a larger real-media calibration corpus. It
would expand risk immediately before release.

### 3. Disable every disputed action — rejected

This is safe but unnecessarily removes useful fixed-offset, deflicker, and
whole-video behavior that can be supported under clear conditions.

## Architecture

### Action capability decision

The planner computes a private capability record for every proposed content-
changing action. The record contains:

- action ID and kind;
- whether preview rendering supports it;
- whether its authorized source ranges can be mapped and executed exactly;
- whether native result verification is available;
- a finite reason when automatic confirmation is unavailable.

Only capable actions enter the immutable `previewed_action_ids` and default
confirmation set. Unsupported actions remain visible as `needs_review` advice.
The capability record is deterministic and derived entirely from the plan,
locks, mappings, and declared local providers.

### Preview coverage and structural lineage

Preview windows are selected before the confirmation digest is issued. Every
confirmable action must overlap at least one actual preview window. If three
windows cannot cover all proposed actions, the planner deterministically keeps
the highest-priority covered actions and review-gates the remainder.

The faithful preview is rendered through the same structural retained-range
path as the final faithful artifact:

1. intersect each preview source window with the confirmed retained ranges;
2. concatenate only those retained pieces;
3. emit the corresponding source-to-faithful preview mapping;
4. apply faithful global corrections only when they are compatible with every
   lock and represented in the preview;
5. build the improved preview exclusively from the faithful preview and its
   local mapping.

Structural deletions inside a preview window therefore change the faithful and
improved preview timing exactly as they change final execution.

### Range and lock enforcement

Local visual actions continue to map their source ranges through the faithful
mapping and use FFmpeg `enable` expressions on the mapped output intervals.

Actions that are intrinsically global follow stricter rules:

- stabilization is automatically eligible only for a full-timeline unlocked
  action with actual preview support; otherwise it is `needs_review`;
- rotation normalization is eligible only when the requested rotation applies
  to the whole retained video and no lock requires preserving a conflicting
  region;
- fixed audio offset is eligible only when it is a whole-stream correction and
  no lock makes a partial correction necessary;
- deflicker curves are remapped into faithful output time after structural
  deletions; if the curve cannot be mapped exactly, the action is
  `needs_review` rather than applied globally.

Tests compare decoded output outside authorized intervals and inside locked
intervals. Those regions must remain unchanged within deterministic codec
tolerance.

### Native audio residual verification

Conservative fixed-offset execution remains part of the faithful artifact. The
native verifier measures the first usable audio and video packet timestamps
from the resulting artifact using local FFprobe JSON output. It records:

- measured residual offset;
- measurement method and tool version;
- configured tolerance;
- `passed`, `failed`, or `needs_review` when timestamps are unavailable or
  ambiguous.

An unavailable or inconclusive measurement cannot become a successful repair.
A real synthetic fixture with a known A/V offset must demonstrate a reduced
residual after the faithful correction.

Native noise evidence remains conservative. VideoScope does not infer a denoise
action from loudness alone. Missing reliable noise evidence produces no
automatic denoise action and records a limitation or `needs_review` advice.

### Native stabilization evidence

Until VideoScope can measure actual applied crop geometry, stabilization output
cannot receive a passed crop-verification result. A real locally generated
shake fixture must prove one of two outcomes:

- stabilization was not auto-confirmable because preview/range capability was
  unavailable; or
- the produced artifact carries `crop_ratio=null` and a
  `native_crop_measurement_unavailable` `needs_review` check.

Planned crop parameters are never treated as observed evidence.

### Retained source descriptor lifecycle

The core pipeline owns a retained-source registry keyed by analysis/plan ID.
Each entry has one state transition from open to released. Release removes the
entry from the registry before closing the handle, so a reused integer cannot
be closed later.

The following operations release idempotently:

- successful or failed execution;
- explicit abort before confirmation;
- cancellation;
- Web job deletion;
- TTL cleanup;
- service shutdown;
- pipeline close;
- replacing an older prepared plan with a new preparation.

Web job management calls the pipeline abort/release contract rather than
closing descriptors independently. Windows deny-write handles and POSIX
`/proc`/`/dev/fd` paths remain valid until release. Tests cover repeated release,
descriptor-number reuse, cancellation before confirmation, deletion, TTL, and
shutdown.

### Schema compatibility decision

Rescue schema remains `0.2` for this development line. The action-execution
ledger is an additive optional field with a deterministic empty default.

Compatibility semantics are explicit:

- an absent ledger in a legacy `0.2` document means execution state is unknown;
- it must never be interpreted as all actions succeeded;
- new writers emit the ledger;
- strict validation continues to reject unknown fields;
- exact-set confirmation semantics apply to newly issued confirmations, while
  old persisted preview documents that lack the required binding fail closed
  and must be regenerated.

The schema guide and migration tests must encode this decision.

## Error Handling

- Capability failures create finite, sanitized reason codes.
- Preview coverage failure prevents confirmation; it does not silently drop an
  accepted action after digest issuance.
- Native verification unavailable becomes `needs_review`, not `passed`.
- Descriptor release is idempotent and never raises because an entry was
  already released.
- Subprocess failures remain structured, sanitized, and path-safe.
- Single-action failure remains isolated in the execution ledger and cannot be
  rendered as success.

## Test Strategy

Implementation follows strict red-green-refactor. Required regressions include:

1. stabilization-only and more-than-three-window plans cannot issue a
   confirmation that claims uncovered actions were previewed;
2. structural removal inside a preview window produces the same faithful local
   timeline mapping as final execution;
3. locked/clean decoded regions remain unchanged for scoped enhancements;
4. stabilization, rotation, fixed offset, and deflicker fail closed when locks
   or mapping make exact execution impossible;
5. deflicker timestamps are remapped after a deleted source interval;
6. a real shifted-A/V fixture proves Conservative residual offset is measured
   on the faithful artifact and reduced within tolerance;
7. a real shake fixture produces honest native crop review state;
8. prepare-abort, cancel, delete, TTL, shutdown, repeated release, and descriptor
   reuse cannot leak or close an unrelated handle;
9. legacy schema-0.2 reports without a ledger parse as unknown execution state,
   while stale preview/confirmation bindings fail closed;
10. full Rescue, Web API, CLI, frontend, real fixture, packaging, and static
    drift gates remain green.

## Success Criteria

- Every confirmable action is represented by an actual preview window and
  executable under its exact authorized ranges.
- No locked or clean region is modified by an action that cannot be scoped.
- Conservative fixed-offset output has native residual-sync evidence.
- Stabilization never passes crop verification without native evidence.
- No retained descriptor survives cancellation, deletion, TTL, shutdown, or
  execution, and no released integer can later close an unrelated descriptor.
- Schema-0.2 additive ledger compatibility is documented and tested.
- A fresh whole-branch review reports zero Critical and zero Important issues.

## Non-Goals

- Segmented stabilization or segmented rotation composition.
- A new AI model, GPU path, remote service, or network dependency.
- Generative reconstruction of missing media.
- Claiming denoise, crop, or sync accuracy without native evidence.
- Publishing, pushing, tagging, deploying, or creating a release.
