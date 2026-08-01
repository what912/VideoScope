# CPU detector algorithm notes

This document describes the observable signals used by the VideoScope CPU
profile and explicitly enabled v0.2 optional detectors. It is an implementation
guide, not an accuracy claim. A Finding indicates evidence worth reviewing, not
a proven defect or an inference about creative intent.

## Shared analysis context

The pipeline probes metadata with ffprobe, computes a streaming SHA-256 hash,
samples frames at a configured fixed rate, and obtains scene boundaries through
the scene-detector adapter. Detector thresholds live in Pydantic configuration
models. The same input bytes, tool version and configuration produce the same
Finding IDs, ordering and interval calculations; `analysis_id`, creation time
and runtime durations are intentionally run-specific.

Frame sampling trades temporal precision for CPU cost. Events shorter than the
sampling interval can be missed or have boundaries displaced by roughly one
sample. Scene boundaries are context, not quality Findings.

For state-like observations such as near-black frames, relative sharpness drops,
and sustained flicker sequences, the reported interval is half-open: a final
anomalous sample extends to the following sample boundary, capped by the video
or scene end. Evidence timestamps remain the actual sampled instants.

## `near_black`

For each sampled frame the detector measures mean luminance, median luminance
and the fraction of pixels below a dark-pixel threshold. A frame is considered
a candidate only when the configured luminance conditions agree. Consecutive
candidates become a Finding after the minimum duration and nearby intervals may
be merged.

The score reflects the strength and duration of the observed near-black signal.
Night scenes, fades, title cards and intentional black frames can look the same
to this heuristic, so Findings use neutral wording and record that limitation.

Key configuration:

- `mean_luma_threshold`
- `dark_pixel_threshold`
- `dark_pixel_ratio`
- `min_duration_seconds`
- `merge_gap_seconds`

## `possible_freeze`

Adjacent samples are compared with both grayscale mean absolute pixel
difference and a low-resolution structural hash distance. A run is a candidate
only when both differences remain below their configured maxima. Accumulation
restarts at scene boundaries, preventing a normal cut from joining unrelated
static intervals.

The interval is described as “Possible frozen or repeated frames.” A deliberate
still shot, locked camera, sparse animation or repeated loop can be
indistinguishable from a freeze at this level of analysis.

Key configuration:

- `max_pixel_difference`
- `max_hash_distance`
- `min_duration_seconds`
- `merge_gap_seconds`

## `scene_relative_blur`

The detector calculates grayscale Laplacian variance as a sharpness proxy.
Within each scene, values are compared with that scene's median so that a
temporary relative drop can be found without assuming one universal sharpness
threshold. A configurable absolute floor also supplies evidence when an entire
scene is uniformly very soft.

Findings report raw sharpness values and the scene baseline. Low texture,
depth-of-field, motion blur and intentional soft focus can reduce Laplacian
variance without representing a quality failure.

Key configuration:

- `relative_ratio_threshold`
- `absolute_floor`
- `min_duration_seconds`
- `merge_gap_seconds`

## `global_flicker`

Each sample contributes a robust global luminance statistic. A local trend is
removed and the detector evaluates alternating high-frequency residuals.
Candidate cycles must exceed the configured residual strength and duration.
Samples near scene boundaries are excluded, while trend removal reduces
sensitivity to gradual fades.

For the sampled sequence, `minimum_cycles` counts sustained oscillations after
the first opposite-sign residual pair. Thus the default value of two requires
at least four consecutive, threshold-crossing residual peaks with alternating
signs.

The Finding is “Potential global luminance flicker.” Rapid intentional exposure
changes, strobe effects or large moving bright objects can produce the same
global signal, and spatially local flicker may be missed.

Key configuration:

- `residual_threshold`
- `minimum_cycles`
- `min_duration_seconds`
- `scene_boundary_guard_seconds`

## Intervals, evidence and severity

Shared time-series utilities convert candidate samples to ranges and merge small
gaps. Evidence selection is handled after detector execution: the common
EvidenceManager copies nearby beginning, middle and ending samples using
generated names and relative paths. Detectors never write HTML or copy files.

Severity, score and confidence are detector-specific summaries of signal
strength; they are not calibrated probabilities and are never combined into an
unsupported global quality score. Review the interval, evidence, parameters and
limitations together.

## Optional `visual_semantic_drift`

This explicitly enabled detector compares normalized visual embeddings only
inside each scene. Samples near scene boundaries are excluded. Both adjacent
and longer-gap frame pairs contribute cosine-distance observations, and each
scene supplies its own robust median/MAD baseline. Consecutive above-baseline
comparisons are merged without crossing a scene boundary.

The Finding title is “Abrupt visual semantic drift.” Evidence shows the frames
before and after the peak distance, while report diagnostics preserve the
path-free distance series, scene baseline, provider, and model. Rapid camera
movement, occlusion, reasonable deformation, and large lighting changes may
all create the same signal. This heuristic is not identity recognition and
does not determine whether a character or person was replaced.

## Optional `prompt_alignment`

OpenCLIP encodes a user-supplied prompt and representative frames from each
scene into one normalized feature space. Descriptive mode records the
scene-level similarity curve without creating a Finding. Threshold mode creates
“Low prompt-frame similarity” only when the user explicitly supplies a
model-specific threshold.

CLIP similarity is not complete semantic verification. Negation, quantities,
complex actions, spatial relationships, cultural context, and details outside
the sampled frames may be represented poorly. The value is not a calibrated
probability that a prompt was followed.

## Optional `text_stability`

The OCR provider detects text, confidence, normalized boxes, and timestamps on
sampled frames. Tracks are matched only within a scene using spatial overlap,
text similarity, and temporal continuity. Candidate Findings describe
frequently changing text, short corrupt-looking observations, unexpected
appearance/disappearance, or unstable recognition in a stable location.

Normal subtitle changes, scene cuts, scrolling text, and isolated low-confidence
results are excluded heuristically. OCR errors can themselves look like
temporal instability, so the output is not proof that rendered text changed.

## Calibration and evaluation

The generated fixtures exercise known temporal patterns and protect against
engineering regressions. They do not represent the diversity of real generated
video and must not be cited as real-world accuracy. For meaningful evaluation,
build an authorized, independently annotated dataset, separate development and
held-out test sets, freeze thresholds before final evaluation, and report every
detector separately with temporal overlap and event-level metrics.
