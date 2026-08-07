# VideoScope C Long Video to Useful Content CPU MVP Design

Date: 2026-08-06

Status: approved for implementation planning

Scope: the C task mode that follows the stabilized Check foundation, A Publish
Ready, D Safe Sharing, and B Video Rescue. Advanced AI remains a later phase.

## 1. Product promise

Long Video to Useful Content turns one authorized local long video into a
reviewed, playable, traceable content package. It does not merely return a list
of timestamps or a prose summary. The user receives a real output video plus a
source map, chapters, change record, and verification report.

The CPU MVP is intentionally faithful. It can help a person remove clearly
reviewed dead space, organize a long recording into chapters, and assemble
explicitly selected clips while preserving source order and context. It does not
claim to understand what is “important”, invent highlights, fabricate quotes, or
silently rewrite a story.

The shortest user loop is:

```text
upload -> choose a useful-content goal -> inspect content map
       -> edit and lock a storyboard -> preview joins -> confirm exact plan
       -> render -> verify -> download video and source map
```

## 2. Stable portfolio naming

The public task-mode letters are fixed:

- A: Publish Ready;
- B: Video Rescue;
- C: Long Video to Useful Content;
- D: Safe Sharing.

Older development documents temporarily called Safe Sharing “Resolve B” and
Video Rescue “Resolve C”. Those labels are historical development-line labels,
not the portfolio task letters. Before C implementation begins, normative product,
architecture, roadmap, navigation, and API documentation must use the stable
portfolio mapping above or spell out the mode name without a letter.

No serialized A, B, or D schema value is renamed merely to make prose consistent.
Schema compatibility and user-facing nomenclature are separate migrations.

## 3. Target users and jobs

The CPU MVP serves people who already have a long recording and need a usable
deliverable without learning a nonlinear editor:

- meeting and interview owners who need a clean, chaptered recording;
- teachers and tutorial creators who need a shorter ordered lesson;
- streamers and event organizers who already know the ranges they want to keep;
- families and travelers who want to assemble selected moments without losing
  provenance;
- researchers who need deterministic source mappings for every output segment.

It is not a general creative editor. It is a guided, review-gated transformation
for common “make this long video usable” work.

## 4. MVP goals

The first release exposes three explicit goals.

### 4.1 Faithful Clean

Keep source order. Propose only observable low-information ranges for review:
long leading/trailing dead space, long silence with low visual change, sustained
near-black intervals, and sustained repeated frames. Each proposal includes its
signal, thresholds, guard ranges, confidence limitation, and before/after join
preview. Nothing is removed until the user accepts the exact range.

Silence alone is never enough to delete a range because pauses can be meaningful.
Automatic proposals require a configurable combination of signals or an explicit
user selection. Locked ranges always win.

### 4.2 Chaptered Full

Preserve the full timeline and create editable chapter boundaries from scene and
silence structure. Without trusted text evidence, titles remain neutral and
observable, such as `Chapter 01` with its timestamp. The user can rename chapters
before confirmation. This mode is a useful fallback when no safe shortening
proposal exists.

### 4.3 Selected Clips

Let the user create, resize, order, and label keep ranges. The default preserves
source order. Reordering requires a separate explicit switch and a warning that
context may change. Every resulting clip and concatenated output interval maps to
one exact half-open source range.

## 5. Inputs and privacy

Required input:

- one local video validated by ffprobe.

Optional inputs:

- an SRT or WebVTT transcript supplied by the user;
- user chapter markers;
- keep, exclude, and locked time ranges;
- target duration as a planning constraint, never a promise;
- a local title and output label.

Transcript content, thumbnails, waveforms, and draft storyboards are private
review material. They stay under `content-review-private/` and are never included
in a shareable package unless the user explicitly includes a derived subtitle or
chapter artifact. Public JSON never contains the original local path.

The CPU MVP does not upload media, call an external transcription service, or
download a speech model. A transcript can be supplied locally, but automatic ASR
belongs to a later optional provider.

## 6. Deterministic content map

The content map is descriptive evidence, not a global quality or importance
score. It combines reusable, read-only features:

- ffprobe metadata and stream inventory;
- scene ranges;
- bounded frame samples;
- silence and loudness intervals from parameter-array FFmpeg calls;
- near-black and repeated-frame observations;
- optional validated transcript cues;
- user-authored keep, exclude, locked, and chapter ranges.

Each `ContentSegment` records:

- deterministic segment ID;
- source time range and optional frame bounds;
- observable signal types and measurements;
- optional transcript cue references, never an invented quotation;
- selection eligibility and limitations;
- representative private evidence;
- source-order index.

Thresholds and guard windows live in strict configuration models. No production
logic branches on fixture filenames, hashes, or sample paths.

## 7. Domain model and schemas

C uses a new `videoscope.content` package. It does not extend `AnalysisReport` or
reuse A/B/D terminal reports as mutable storage.

Initial versioned models:

- `ContentConfig`;
- `ContentGoal`;
- `ContentMap` and `ContentSegment`;
- `ContentChapter`;
- `Storyboard` and `StoryboardItem`;
- `ContentAction` and `ContentPlan`;
- `ContentConfirmation`;
- `ContentSourceMapping`;
- `ContentChangeLog`;
- `ContentVerificationCheck` and `ContentVerificationReport`;
- `ContentArtifact` and `ContentTechnicalReport`.

IDs, ordering, plan digests, JSON encoding, finite-number validation, atomic
writes, and relative POSIX artifact paths follow the established Resolve safety
contracts. A confirmation binds the exact storyboard, locked ranges, plan,
preview identity, configuration, source hash, and verification policy.

Unknown fields are rejected. A stale digest, changed source, changed transcript,
or changed locked range invalidates the confirmation.

## 8. Planning rules

Planning is deterministic for the same source hash, optional transcript hash,
configuration, and user decisions.

Rules:

1. locked keep ranges cannot be shortened, removed, or split;
2. locked exclude ranges cannot appear in the output;
3. Faithful Clean preserves source order and never overlaps source ranges;
4. minimum retained context guards both sides of a proposed removal;
5. target duration can rank eligible proposals but cannot force unsafe deletion;
6. if evidence is insufficient, the plan stays full-length or waits for manual
   selection instead of inventing highlights;
7. transcript cues can support keyword filtering only when cue timing validates;
8. every content-changing action requires preview and explicit confirmation;
9. a failed independent action does not silently change later source mappings;
10. the plan records why each segment is kept, removed, or user-selected.

There is no universal “interestingness” score and no overall content score.

## 9. Preview and editing

The Web workbench is a timeline and storyboard editor, not a static report.

It provides:

- source player with frame-step and playback-speed controls;
- structural timeline lanes for scenes, silence, observable issues, transcript
  cues, locked ranges, and proposed output;
- accessible keep/remove/lock controls with exact timestamps;
- editable chapters and storyboard items;
- join previews containing bounded context from both sides;
- estimated output duration and source coverage, labelled as estimates;
- source-map highlighting when seeking output or source video;
- English/Simplified Chinese UI and invariant `what912` attribution;
- refresh recovery, cancellation, explicit deletion, and mobile review layout.

Keyboard users can select a range, adjust endpoints by a configured step, lock it,
and preview the join without drag-only controls.

## 10. Execution and artifacts

Execution uses local FFmpeg/ffprobe argument arrays. It streams source ranges and
does not load a long video into memory. The source file is read-only and hash
checked before and after processing.

Private review tree:

```text
content-review-private/
  content-map.json
  storyboard.json
  preview/
  evidence/
  transcript-normalized.json
```

Public result tree:

```text
content-output/
  useful-content.mp4
  chapters.json
  source-map.json
  changes.json
  technical-report.json
  report.html
  subtitles.srt              # only when explicitly requested and validated
  clips/                     # only for Selected Clips export
```

`source-map.json` is mandatory for every content-changing result. It records each
output range, exact source range, ordering, transition, and whether the range is
unchanged or transformed. The MVP supports hard joins and bounded audio fades;
decorative transitions and generated fill are excluded.

## 11. Independent verification

A result can be `completed` only when all required checks pass:

- output and every published clip decode;
- duration and stream inventory match the confirmed plan within tolerance;
- source mappings are in bounds, non-overlapping where required, and reproduce
  output duration within tolerance;
- locked keep ranges are fully present and locked excludes are absent;
- source order is preserved unless explicit reorder confirmation exists;
- joins do not introduce a new long black or repeated-frame interval;
- audio continuity and fixed A/V residual remain within configured tolerances;
- requested chapters and subtitles are time-valid and within output duration;
- public artifacts match a fixed allowlist and contain no private/absolute path;
- source SHA-256 is unchanged.

An inconclusive required check produces `needs_review`; a missing mandatory
artifact or unsafe mapping produces `failed`. Independently verified partial clips
may be published only with `partial` status and explicit missing ranges.

## 12. CLI and local Web API

Proposed CLI:

```text
videoscope content INPUT --goal faithful-clean --output OUTPUT
videoscope content INPUT --goal chaptered-full --transcript cues.srt --output OUTPUT
videoscope content INPUT --goal selected-clips --keep 12.0:48.5 --keep 90:130
```

The CLI first writes a private content map and plan. Interactive execution asks
for the exact digest after preview. Non-interactive content-changing execution
requires an explicit reviewed plan file plus `--yes`; it cannot silently accept
new automatic proposals.

The local Web API uses `/api/content/jobs` with the existing safe storage,
bounded upload, host/origin policy, SSE sequencing, CPU limiter, cancellation,
retention, and deletion contracts. It calls the same core pipeline as the CLI.

## 13. Error handling and fallbacks

- no safe shortening proposals: offer Chaptered Full or manual Selected Clips;
- no audio: continue with visual structure and show silence analysis unavailable;
- invalid transcript timing: exclude transcript evidence and require review;
- one feature provider fails: preserve other evidence and mark the map partial;
- join preview fails: block confirmation of the affected action;
- target duration cannot be reached safely: report the achievable estimate;
- render failure: remove pending public output and preserve private review state;
- verification failure: never label the result completed;
- cancellation: stop new work, terminate cancellable child processes, remove
  staging, and preserve only explicitly retained private state.

Diagnostics are bounded and sanitized. They do not expose source paths,
transcript text, external-command dumps, or server tracebacks.

## 14. Performance boundaries

- one streaming probe and one shared sampling pass per source configuration;
- bounded thumbnail size and count, with disk-backed private evidence;
- chunked hashing and upload;
- interval math instead of per-frame arrays where possible;
- configurable maximum transcript cues, chapters, storyboard items, and preview
  duration;
- no automatic full-resolution frame cache;
- deterministic cleanup of preview, staging, and expired job trees.

Synthetic tests prove engineering behavior, not hour-long performance. Authorized
30-minute and multi-hour media profiling remains a release gate.

## 15. Test strategy

### Model and pure-function tests

- invalid and overlapping ranges, Unicode, finite values, deterministic IDs;
- lock precedence, target-duration ranking, order preservation, and digest
  invalidation;
- transcript cue parsing and timing rejection;
- source-map composition and duration conservation;
- public/private path separation and canonical JSON round trips.

### Deterministic local fixtures

Generate without external media:

- `content_meeting_structure.mp4`: alternating speech-like audio, pauses, and
  stable visual sections;
- `content_tutorial_chapters.mp4`: distinct ordered scenes and chapter gaps;
- `content_locked_context.mp4`: removable-looking interval overlapping a locked
  keep range;
- `content_join_regression.mp4`: boundaries that expose unsafe black/freeze or
  audio joins;
- local SRT/WebVTT cues with valid, overlapping, out-of-range, Unicode, and
  malformed cases.

### Real gates

- source remains byte-identical;
- exact accepted removals and no unaccepted removal;
- locked content survives byte-time mapping;
- output plays and source-map duration matches;
- no new required black/freeze/audio-continuity regression;
- clean base-wheel smoke for all three goals without AI, OCR, Web, GPU, network,
  or model download;
- bilingual Web lifecycle, refresh, cancellation, deletion, and keyboard review.

## 16. Explicit non-goals for the CPU MVP

- automatic speech recognition or speaker identity;
- semantic highlight ranking without a trusted local transcript/provider;
- generated summaries, titles, quotes, B-roll, thumbnails, or narration;
- face identity, emotion, or demographic inference;
- automatic creative reordering;
- auto-reframing, smart crop, stabilization, interpolation, super-resolution, or
  generative repair;
- cloud upload, accounts, team collaboration, or public share hosting;
- a global quality, importance, or virality score;
- a promise that a target duration will be reached.

These belong to later optional AI/provider work only after the faithful C
pipeline, source mapping, confirmation, and verification contracts are stable.

## 17. Acceptance and phase boundary

The C CPU MVP is ready for implementation only after review confirms:

1. the portfolio letter mapping and nomenclature migration;
2. the three goals and their non-goals;
3. transcript privacy and no-default-ASR boundary;
4. mandatory storyboard confirmation and source mapping;
5. independent verification and fail-closed statuses;
6. CLI/API/artifact names and base-install boundary.

After design approval, a separate implementation plan must split documentation,
models, mapping, feature providers, planning, preview, execution, verification,
CLI, Web, fixtures, performance, and release audit into independently testable
tasks. No advanced AI task may begin before that plan is completed and the C CPU
MVP passes its release gates.
