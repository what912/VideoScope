# Safe Sharing privacy schema

Status: Task 6 contract for the Resolve D Safe Sharing CPU MVP.

Schema version: `0.1`

This schema defines the versioned privacy-domain documents used by Safe Sharing.
It is separate from the frozen v0.1 `AnalysisReport` and does not change the
`videoscope analyze` command or the Detector protocol. It does not assert that
all privacy risks can be found or that an output is absolutely safe.

## 1. Boundary and privacy classes

`PrivacyRiskMap` is a private review document. It may include
`PrivacyRisk.private_evidence`, such as unredacted evidence references or raw
recognition material needed only for local review. `PrivacyRiskMap.public_summary()`
returns an `is_private: false` document with every `private_evidence` value
removed. A public risk map rejects non-empty `private_evidence`.

`PrivacyPlan`, `PrivacyChangeLog`, `PrivacyVerificationReport`, and
`PrivacyTechnicalReport` are versioned documents. Only public-safe information
and output-root-relative artifact paths can enter public reports or the share
package. These documents must not contain raw sensitive OCR text, unredacted
evidence, absolute paths, usernames, GPS values, or identity guesses.

## 2. Shared validation rules

- Every model uses Pydantic `extra="forbid"`.
- Every model is immutable after validation. Nested JSON objects and arrays are
  recursively detached and frozen; validated copy updates are revalidated before
  producing a new immutable model. Mutable set values are not valid JSON and are
  rejected at the `JsonValue` boundary.
- Seconds are finite floats greater than or equal to zero; an end cannot precede
  its start.
- `NormalizedBox` has `x_min`, `y_min`, `x_max`, and `y_max` in `[0, 1]`, with
  `x_min < x_max` and `y_min < y_max`.
- Hash fields are lowercase 64-character SHA-256 hexadecimal strings.
- `PrivacyArtifact.relative_path` must be a normalized, forward-slash relative
  path. Absolute, drive-qualified, parent-traversal and backslash paths fail
  validation. The current-directory marker `.` is not an artifact path and also
  fails validation.
- JSON is UTF-8, preserves Unicode (`ensure_ascii=False`), sorts object keys,
  keeps model-defined array order, rejects NaN/Infinity, and ends written files
  with exactly one newline. Writers revalidate values and atomically replace
  destinations through a same-directory temporary file.

## 3. Enums

`PrivacyRiskType` values are `metadata`, `face_region`, `qr_code`, `barcode`,
`suspicious_text`, `manual_visual`, and `manual_audio`.

`PrivacyDecision` values are `unreviewed`, `allow`, and `redact`.

`RedactionStyle` values are `blur`, `pixelate`, `solid_fill`, `crop`, `mute`,
and `remove_metadata`.

`PrivacyActionKind` values are `remove_metadata`, `crop`, `visual_redaction`,
`audio_mute`, `remux`, and `verify`.

`PrivacyJobOutcome` values are `completed`, `needs_review`, `partial`, and
`failed`. They are conservative workflow outcomes, not quality scores.

## 4. Risk and review models

`PrivacyRisk` has exactly these public fields:

```text
id, scanner_id, scanner_version, risk_type, title, public_description,
severity, confidence, start_seconds, end_seconds, box, track_id,
metadata_scope, metadata_key, recommended_style, decision, style,
limitations, evidence, private_evidence
```

`confidence` is scanner-local and must not be aggregated into a global quality
or safety score. Descriptions state observations and limitations, not identities
or guarantees. An `allow` decision forbids a style; a `redact` decision requires
one. Metadata accepts only `remove_metadata`, audio accepts only `mute`, and
visual risk types accept `blur`, `pixelate`, `solid_fill`, or `crop`.

`PrivacyRiskMap` contains `schema_version`, `input_hash`, `profile`,
`duration_seconds`, `risks`, and `is_private`. It recomputes each risk ID and
sorts risks by start seconds, Severity order (`info` through `critical`),
scanner ID, and ID. A crop decision is valid only for a box without a track that
covers the static full-duration interval.

`PrivacyReviewDecision` contains `risk_id`, `decision`, `style`, `edited_box`,
and an aware `reviewed_at` timestamp. The timestamp is an audit field and is
excluded from a deterministic plan digest.

## 5. Plan and artifact models

`PrivacyEffectiveConfig` records `preview_seconds`, `guard_pixels`, the blur
kernel, pixelation block size, BGR solid-fill color, interpolation guard ratio,
track-gap expansion policy, the selected profile version, its QR handling and
default visual style, the normalized private preview identity, the expected
public artifact paths, the immutable `source_read_only: true` commitment, and
the ordered verification policy. Every field is path-safe and
confirmation-relevant;
changing any one of them changes the plan digest. Preview and expected-artifact
identities are normalized forward-slash relative paths and cannot escape their
respective artifact roots.

`PrivacyAction` contains exactly `id`, `version`, `kind`, `start_seconds`,
`end_seconds`, `box`, `parameters`, `changes_semantics`, and
`requires_confirmation`. A `crop` action must match a reviewed static,
full-duration crop risk with the same interval and box. Visual actions may carry
sorted public `keyframes` containing only `timestamp_seconds` and normalized
`box`; these fields are confirmation-relevant and are evaluated against streamed
per-frame presentation timestamps. Private payloads and evidence paths are never
copied into action parameters.

`PrivacyArtifact` contains `relative_path`, `sha256`, and `description`.

`PrivacyPlan` contains `schema_version`, `input_hash`, `profile`, optional
`duration_seconds`, `effective_config`, `risks`, `actions`, `artifacts`, and
`digest`. It rejects `private_evidence`; a crop risk/action additionally
requires an explicit source duration and the same static full-duration interval
and box.
`PrivacyChangeLog` contains `schema_version`, `plan_digest`, the invariant
`source_modified=false`, a path-free `processor` summary, `actions`, and
`artifacts`. The processor summary records the executable basename, locally
observed FFmpeg version status, and deterministic execution order; it never
contains an absolute executable or source path.

`make_privacy_plan_digest(input_hash, profile, effective_config, risks, actions,
artifacts, duration_seconds=...)` creates a SHA-256 digest from canonical UTF-8
JSON. It includes the source duration, complete effective configuration,
reviewed risk values, actions, and artifacts, while deliberately excluding risk
`private_evidence` and review audit time.

## 6. Reviewed planning and preview commands

`build_privacy_plan(risk_map, reviews, profile, config)` applies at most one
review to each known risk, strips private evidence, and rejects unresolved
`high` or `critical` observations. Conflicting full-duration crops are rejected.
Profiles with `qr_handling: redact_by_default` deterministically redact an
otherwise unreviewed QR/barcode proposal with its applicable recommended visual
style, falling back to the profile's default visual style. Profiles with
`qr_handling: review` leave it unreviewed, so a high-risk proposal blocks the
plan until the user reviews it. The effective profile policy and resulting
decision/action are both covered by the digest. Any crop or visual action without
a normalized box fails with a structured planning error.
The deterministic action order is metadata removal, one optional crop, visual
redactions ordered by interval and risk ID, audio mutes, remux, then verification.
Only exactly adjacent actions with the same style and compatible region/track
may merge. Every content-changing action requires confirmation.

The command builders return complete `list[str]` argument arrays. They never
return shell strings and never use `shell=True`. Preview duration is bounded by
the digest-bound effective configuration and source duration. Preview, audio,
and remux commands use explicit video/audio mappings, remove global and
per-stream metadata, remove chapters, preserve Unicode paths as one argument,
and reject an output path that resolves to the source. Reviewed mute intervals
become one time-sorted deterministic FFmpeg `volume` filter chain.

The CPU visual renderer conservatively rejects a video stream with non-zero
rotation or display-matrix metadata before starting decoder or output processes.
This prevents metadata stripping from silently changing the visible orientation.
Users must normalize the rotation into the pixels before running Safe Sharing.

## 7. Verification documents

`PrivacyVerificationCheck` contains `check_id`, `status`, `message`,
`measured`, and `required`. `PrivacyVerificationReport` contains
`schema_version`, `plan_digest`, `status`, and `checks`; it rejects duplicate
check IDs and derives its status conservatively from required checks: `failed`,
then `needs_review`, then `partial`, otherwise `completed`.

`PrivacyTechnicalReport` contains `schema_version`, `plan_digest`,
`verification`, and public `artifacts`.

## 8. Deterministic identity and codecs

`make_privacy_risk_id(input_hash, scanner_id, risk_type, start_seconds,
end_seconds, box)` returns `privacy_risk_` followed by a lowercase SHA-256 digest
of those observable identity inputs. Equal validated inputs always produce the
same ID. Seconds are canonicalized to finite non-negative floats before hashing,
so equivalent integer and float values have one identity and `-0.0` is represented
as `0.0`.

The model-specific codecs are:

```text
privacy_risk_map_to_json / privacy_risk_map_from_json
write_privacy_risk_map_json / read_privacy_risk_map_json
privacy_plan_to_json / privacy_plan_from_json
write_privacy_plan_json / read_privacy_plan_json
privacy_change_log_to_json / privacy_change_log_from_json
write_privacy_change_log_json / read_privacy_change_log_json
privacy_technical_report_to_json / privacy_technical_report_from_json
write_privacy_technical_report_json / read_privacy_technical_report_json
```

All readers validate the entire Pydantic document, including privacy risk IDs and
plan digests. All writers write only local filesystem paths supplied by their
caller; the schema itself imposes no network access, model loading, GPU probing,
or media processing.
