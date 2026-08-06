# Rescue Trustworthy Timeline Correction Design

## Context

Task 12 introduced bounded, single-decode timeline sampling for long Rescue
inputs. The fifth review confirmed that retained image memory and ordinary
fixed-rate cadence were repaired, but found one remaining correctness gap.
`sample_frames()` still chooses between its audited streaming path and its
legacy fixed-rate path from FFprobe's reported duration. If that duration is
stale-low, the legacy path can stop after `max_samples`, omit a decodable tail,
and return `truncated=false`.

This correction is a separate task because the original Task 12 five-round
review breaker has tripped. It must be completed before release preparation.

## Root Cause

The timeline probe is used for two incompatible purposes:

1. constructing a proposed sampling schedule; and
2. deciding whether the full decoded timeline needs to be audited.

Probe timing is suitable as a schedule proposal, but it is not authoritative
enough to decide that decoding may stop early. The only existing path that can
compare the proposed duration with the actual decodable tail is the streaming
timeline path. Routing an apparently short input around that path therefore
removes the evidence needed to detect a stale-low probe.

## Considered Approaches

### 1. Always audit timeline requests in one streaming decode — selected

Every call that supplies `timeline_duration_seconds` uses the streaming
decoder. Probe timing only selects a target schedule:

- predicted sample count at or below the cap: fixed-rate targets beginning at
  zero, matching the existing `fps=...:start_time=0:round=near` cadence;
- predicted sample count above the cap: bounded, uniformly distributed targets
  that include the timeline endpoints.

The decoder always continues to the actual end and validates that end against
the probe. A stale-low or stale-high duration fails closed. This preserves one
video decode, bounded retained payloads, deterministic ordering, and the
ordinary fixed-rate contract.

### 2. Run a full packet or frame preflight before choosing a path — rejected

A full `ffprobe -show_packets` or `-show_frames` scan could discover the tail
before sampling, but it adds another full-file pass and can become a second
decode-like cost. It weakens the performance contract and duplicates work the
streaming decoder already performs.

### 3. Keep the branch and mark short-path results as uncertain — rejected

Changing only `truncated` or adding a warning would still publish incomplete
frame evidence. Rescue verification relies on the samples themselves, so an
uncertainty label cannot repair the missing tail.

## Selected Architecture

### Scheduling

The public validation and hard cap remain unchanged. A private schedule mode is
passed into the streaming candidate selector:

- `fixed_rate`: targets are `0, 1/rate, 2/rate, ...` strictly before the
  proposed duration, with at most `max_samples` targets. It does not force the
  final decoded frame into the returned samples because that would alter the
  established cadence.
- `uniform_capped`: targets span the proposed duration and retain the existing
  first/last endpoint behavior.

Both schedules are deterministic. Target construction is bounded by the
existing maximum of 1,000 selections.

### Decode and audit

Timeline requests start exactly one FFmpeg video decode. The existing ordered
target advancement retains no more than the previous and current PNG payloads,
writes at most the bounded selected candidates to the private staging
directory, and performs linear target work.

The decoder continues to EOF even after all fixed-rate targets are satisfied.
Its normalized final timestamp is compared with the proposed duration using the
existing frame-duration tolerance. A material mismatch raises
`FrameSamplingError`; no partial frame set is returned as a successful result.
Non-timeline callers keep the legacy fixed-rate extraction path unchanged.

### Error handling and cleanup

Stale-low, stale-high, zero, unknown, non-monotonic, and cardinality-invalid
timelines fail closed with sanitized diagnostics. Published frame files and the
private candidate directory are removed on failure. No absolute input path is
added to user-visible errors.

## Test Strategy

The implementation follows strict red-green-refactor:

1. Add a regression where the timeline probe reports a duration below the cap
   while the single streaming decode exposes a longer tail. It must fail before
   the production change because the legacy branch returns success.
2. Assert the corrected path performs one FFmpeg video decode and fails closed
   without publishing PNG files.
3. Keep a real-FFmpeg regression proving accurate short inputs preserve the
   original fixed-rate timestamps and are not marked truncated.
4. Keep real long, VFR, non-zero-PTS, one-frame, stale-high, missing-duration,
   bounded-memory, linear-work, deterministic-name, and cleanup coverage green.
5. Run the Task 12 real suite and the repository validation script. The known
   NumPy-stub/mypy environment mismatch is reported separately and must not be
   hidden by weakening checks.

## Success Criteria

- No timeline request can bypass decoded-tail auditing because of stale-low
  FFprobe duration.
- Accurate uncapped inputs retain the prior fixed-rate sample cadence.
- Capped inputs retain uniform full-timeline coverage and endpoint behavior.
- Each timeline request uses one FFmpeg video decode.
- Retained in-memory PNG payloads and selected on-disk candidates remain bounded
  by configuration rather than video duration.
- Failures clean partial outputs and do not leak absolute paths.
- A fresh task reviewer reports zero Critical and zero Important findings.

## Non-Goals

- Replacing FFprobe or FFmpeg.
- Changing the public sampling cap or Rescue assessment defaults.
- Adding GPU, AI, network, or model dependencies.
- Refactoring unrelated video sampling or detector behavior.
