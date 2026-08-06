# Video Rescue schema

`videoscope.rescue` is a post-v0.1, opt-in Resolve domain. It never changes
the frozen `AnalysisReport`, the detector protocol, or `videoscope analyze`.
It is a local, CPU-first contract for recording observable media intervals,
reviewable rescue plans, separate output artifacts, and independent technical
verification. It does not calculate a total quality or recovery score.

## Version and JSON rules

All canonical top-level Rescue documents intended for persistence carry
`schema_version: "0.2"` and reject unknown fields. The ephemeral
`RescueConfirmation` request is intentionally unversioned: it is validated
against the exact versioned `RescuePlan` digest and is never persisted as a
canonical Rescue document. Seconds are finite, non-negative floats; an interval
ends at or after its start. SHA-256 values are 64-character lowercase
hexadecimal strings.
Any published artifact path is a normalized, output-root-relative POSIX path:
it cannot be absolute, use a Windows drive or backslash, be `.`, or contain
`..`.

JSON writers validate before writing, use UTF-8 with unescaped Unicode,
alphabetically sorted object keys, stable model ordering, `allow_nan=False`, a
single trailing newline, and same-directory atomic replacement. Public JSON
never contains source or workspace absolute paths.

The action-execution ledger is an additive schema 0.2 field. A missing
`action_executions` field in a legacy 0.2 change log or technical report means
the execution state is unknown; an explicitly emitted empty list means that
the writer recorded no executable action. Neither state means that all actions
succeeded. New canonical writers always emit `action_executions`, including an
empty list, while readers continue to reject unknown fields.

## Damage map

`MediaDamageMap` records the source hash, source duration, scanner version,
scan coverage, and deterministically ordered `DamageInterval` values. Each
interval contains a deterministic `damage_<sha256>` identity, stream ID,
`DamageKind`, time interval, neutral observable description, and optional
path-free measurements. The ID is the canonical SHA-256 digest of input hash,
stream ID, kind, start seconds, and end seconds.

`DamageKind` is one of `decodable`, `undecodable`,
`timestamp_discontinuity`, `missing_stream`, `fixed_av_offset`, `dark`,
`video_noise`, `soft_detail`, `flicker`, `shake`, `low_loudness`,
`audio_noise`, `audio_clipping`, `uncertain`, or `missing_information`.
These are observable classifications or explicit uncertainty, not a claim about
the media's cause or a statement that information was recovered.

## Plan and confirmation

`RescuePlan` binds one `RescueStrategy` (`conservative` or `balanced`) to its
path-free effective configuration, ordered actions, shared preview ranges,
expected private/public artifact paths, optional observed damage intervals, and
`plan_digest`. `plan_digest` is the SHA-256 digest of the complete plan payload
without that field; changing any effective value invalidates it.

Each `RescueAction` has an ID, version, action kind, neutral description,
source ranges, JSON parameters, whether it changes content, whether it requires
confirmation, dependencies, and an optional fallback. Stable action order is:
`remux`, `rebuild_timestamps`, `select_tracks`, `normalize_rotation`,
`salvage_segments`, `trim_damaged_edges`, `correct_fixed_av_offset`,
`adjust_luma`, `denoise_video`, `sharpen`, `deflicker`, `stabilize`,
`normalize_audio`, `denoise_audio`, `verify`. Content-changing actions require
confirmation. Conservative plans never include subjective enhancement; Balanced
actions are explicit and must be preview-confirmed by later workflow stages.

`RescueConfirmation` always requests `publish_faithful: true`. It optionally
requests an improved copy and lists accepted action and trim-damage IDs. Before
execution, `RescuePlan.validate_confirmation()` requires the matching digest,
rejects IDs absent from the plan, and permits `publish_improved` only with an
accepted Balanced action. Source media remains read-only.

New confirmations bind the exact action set represented by the private
preview. A persisted preview or confirmation that lacks that exact binding is
stale and must be regenerated before confirmation; it is never silently
accepted for execution.

## Execution and verification records

`RescueChangeLog` records the confirmation-bound executed actions and artifacts
with `source_modified: false`. In schema 0.2, every `RescueArtifact` contains a
required `artifact_role` (`faithful`, `improved`, or `document`) in addition to
its safe relative path, SHA-256, and description. Verification reports accept
only the ordered `faithful` and optional `improved` roles, and each media role
is bound to its canonical output path. Consumers must use this typed role; they
must not infer media identity from filenames.

`RESCUE_REQUIRED_VERIFICATION_CHECK_IDS` is the immutable canonical v0.1
policy. Both `RescueEffectiveConfig.verification_policy` and
`RescueVerificationReport.required_check_ids` must equal it exactly, including
order. A report requires those check IDs once for the faithful artifact and,
when present, once more for the improved artifact. Each check identifies its
artifact. Faithful/improved status and `RescueOutcome` are derived from those
checks rather than trusted from the caller: missing checks cannot produce
`passed` or `completed`. A faithful
failure is `failed`; an improved failure after a passed faithful copy is
`partial`; any review-required copy is `needs_review`; otherwise it is
`completed`. Public technical records recursively reject absolute paths in
text, JSON keys, and JSON values, keeping private diagnostics outside the
public schema.
`RescueTechnicalReport` binds the damage map, verification report, artifacts,
limitations, and manual-review reasons to the exact plan digest.

## Migration from schema 0.1

Schema 0.1 Rescue artifacts contained only `relative_path`, `sha256`, and
`description`; they did not carry an artifact role. Schema 0.2 makes
`artifact_role` required because terminal consumers must distinguish verified
faithful and improved results without guessing from a filename.

There is intentionally no implicit 0.1-to-0.2 migration. A role cannot be
reconstructed safely without trusted execution context, so 0.1 public payloads
and persisted jobs fail closed under the 0.2 readers. Users must start a new
local Rescue job to produce a complete 0.2 contract. Readers never relabel a
0.1 payload as 0.2 and never infer a missing role from `relative_path`.
