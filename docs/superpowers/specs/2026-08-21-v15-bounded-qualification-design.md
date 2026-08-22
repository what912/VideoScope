# V15 bounded qualification design

## Status

Approved in chat on 2026-08-21. This document defines the next bounded V15
algorithm-research stage after the r1 faithful-only fallback and its Task8
`needs_review` result.

## Goal

Find a real-source candidate for the omitted clarity/audio actions, while
preserving every existing public verification threshold and fail-closed
publication rule. Transition-STABILIZE may receive a bounded profile/estimator
qualification pass, but its current passing behavior must remain unchanged when
no new profile passes.

## Scope

The work is split into three independent qualification tracks:

1. **SHARPEN / DEBLUR clarity track.** Generate a finite set of real-source
   candidates over the complete retained action range. Each candidate must have
   same-generation baseline, visibility/control, and candidate artifacts. The
   qualification provider must bind exact ranges, PTS inventory, stream
   topology, encode contract, and artifact hashes. Existing clarity, ringing,
   overshoot, noise, and recovered-baseline thresholds remain unchanged. The
   planner selects only the first profile for which every required gate passes;
   otherwise the action is omitted with the canonical limitation.

2. **DENOISE_AUDIO track.** Generate finite encoded AAC candidates from one
   parent generation and measure every complete 50-ms target/non-target window,
   persistent-tone preservation, and boundary-transient gate. Existing target
   attenuation, non-target, persistence, and transient thresholds remain
   unchanged. The first profile satisfying all gates is selected; otherwise no
   audio action is emitted.

3. **Transition-STABILIZE track.** Preserve `transition_anchor_v1`, exact PTS
   binding, range coverage, crop, seam, and consensus checks. Any new estimator
   axis is finite and optional. A candidate is usable only when the unchanged
   P90, seam, crop, coverage, and required residual gates all pass. If no
   candidate passes, the existing GREEN STABILIZE behavior is retained.

## Data flow and contracts

The lifecycle is:

`assessment -> draft plan -> private qualification -> final plan -> preview`
`-> user confirmation -> execution -> final verification`.

Qualification evidence is strict, path-free, canonical JSON. It binds the
input/draft action identity, effective configuration, candidate profile order,
exact source/output ranges, normalized actual PTS, topology, encode contract,
control recipe, metrics, thresholds, and selected profile. The planner embeds
only validated evidence into the final action parameters and recomputes the
action ID and plan digest. Commands, preview, executor, and final verifier each
re-derive and validate the same contract; missing, stale, reordered, duplicated,
or semantically tampered evidence fails closed.

Same-generation controls are private and temporary. They are produced from the
action's immediate parent with the same encode contract, retained only for
verification, and removed in a `finally` block under the private workspace
root. Cleanup failure is an error; external or path-escaped control references
are never unlinked.

## Failure and publication behavior

- A failed or unavailable qualification produces a stable limitation and no
  action; it is never represented as a passing measurement.
- A candidate with any required gate failure is not previewed as an approved
  action and cannot reach confirmation.
- Optional review checks remain visible but cannot publish a result when the
  aggregate outcome is `needs_review`.
- No threshold, tolerance, skip, fixture-name, hash, or path exception may be
  added to make a candidate pass.
- This stage does not run PREPARE, confirmation, execution, Task8, publication,
  network access, model downloads, or Git operations.

## Verification sequence

For each track:

1. Add genuine RED tests for the missing contract or semantic behavior.
2. Implement the smallest GREEN change in the existing qualification,
   planner, executor, and verifier boundaries.
3. Run focused and affected non-native tests, Ruff, format, and mypy.
4. Obtain an independent evidence review.
5. Run at most one fixed-FFmpeg/ffprobe-8.1.2 native qualification node for
   that track in a fresh no-clobber directory; failures are retained without
   retry.

After all selected tracks pass their gates, run one fresh unified validation.
Only then may a separate PREPARE-ONLY candidate be considered.

## Non-goals

- No joint optimizer across SHARPEN, DEBLUR, DENOISE_AUDIO, and STABILIZE.
- No relaxation or reinterpretation of existing public thresholds.
- No automatic fallback from one action family to another.
- No claim that a track is fixed when all real-source candidates are omitted.
