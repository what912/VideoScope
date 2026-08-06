# VideoScope Safe Sharing CPU MVP Design

Status: user-approved conversational design, pending written-spec review

Date: 2026-08-02

Scope: VideoScope Resolve mode D, implemented after the Publish Ready MVP and
before Video Rescue

## 1. Product outcome

Safe Sharing turns a local source video into a separately written sharing copy.
It helps an ordinary user review visible, audible, and embedded privacy risks,
choose what to redact, preview the result, and download a verified share package.

The MVP must deliver a playable output rather than only a list of risks. It is
local-first, CPU-first, source-read-only, and confirmation-gated. It never claims
that every privacy risk was found or that an output is absolutely safe.

The first MVP uses a hybrid workflow:

- automatically strip supported container, stream, and chapter metadata;
- use lightweight local CPU methods to propose face regions, QR/barcode regions,
  and suspicious text regions;
- let the user add, remove, resize, and approve visual regions;
- let the user mark audio intervals for muting;
- require preview and an exact plan-digest confirmation before full rendering;
- rescan the output and use `needs_review` whenever a required check cannot pass.

Video Rescue is a separate subsystem. It follows this MVP and does not share a
single implementation specification with Safe Sharing.

## 2. Product boundaries

### 2.1 Included

- Share-audience profiles for public publishing, work or client delivery,
  school, family, and an external AI service.
- Metadata inspection and removal.
- Anonymous face-region proposals without identity recognition.
- QR and barcode region proposals.
- Suspicious on-screen text proposals for phone numbers, email addresses,
  addresses, account identifiers, and similar user-reviewable patterns.
- Manual visual regions and manual audio mute intervals.
- Blur, pixelation, solid fill, crop, allow, and mute decisions where applicable.
- Risk timeline, evidence, confidence, limitations, and explicit human decisions.
- Short preview, deterministic plan digest, full render, verification, and local
  artifacts.
- CLI, shared core API, local Web API, and React workbench integration.

### 2.2 Excluded

- Real-person identity recognition or an identity database.
- A claim that face detection reveals who a person is.
- Default network access, external upload, or model download.
- Automatic speech privacy recognition in the CPU MVP.
- A promise to discover all sensitive content or make a video absolutely safe.
- Overwriting the source file.
- Putting private evidence or original sensitive text in the public share package.
- Hiding a detector or verification failure as an empty risk list or success.

## 3. User workflow

1. The user selects the sharing audience and a versioned privacy profile.
2. The product states where processing happens, that the source is read-only,
   and which optional capabilities are available.
3. The user selects a local video.
4. VideoScope probes and hashes the input, scans metadata, samples frames, and
   runs the enabled CPU privacy scanners.
5. The workbench presents a risk map on the video and timeline.
6. The user reviews each risk as `allow`, `redact`, or `unreviewed`, and can edit
   or add regions and audio intervals.
7. The planner creates an ordered, immutable redaction plan and a short preview.
8. The user compares source and preview, then submits the exact plan digest.
9. The executor renders a new video into staging and never mutates the source.
10. The verification gate rescans the candidate output.
11. Only a fully passing result becomes `completed`; unresolved or unverifiable
    high-risk items produce `needs_review`.
12. The user downloads the share package, revises the plan, starts another task,
    or explicitly deletes local task data.

The lifecycle extends the existing Resolve lifecycle:

```text
created -> inspecting -> scanning -> awaiting_review -> planning
        -> previewing -> awaiting_confirmation -> processing -> verifying
        -> completed | needs_review | partial | failed | cancelled
```

Transitions are persisted, recoverable, ordered, and monotonic. Cancellation
stops later work and cleans unfinished staging data while preserving artifacts
the user explicitly asked to retain.

## 4. Architecture

Safe Sharing reuses the existing Resolve job, plan, executor, confirmation,
artifact, SSE, recovery, and verification foundations. It adds privacy-specific
components behind explicit interfaces rather than embedding algorithms in the
CLI, API, or React components.

### 4.1 PrivacyScanner

Each scanner has a separate Pydantic configuration model, version, requirements,
and unit tests. Scanners produce risk candidates only; they do not render media.

The CPU MVP scanners are:

- `metadata_privacy`: reports removable container, stream, chapter, filename,
  device, software, author, title, creation-time, location, and attachment risks.
- `anonymous_face_region`: returns anonymous tracks such as `face_track_01` and
  never attempts identity matching.
- `qr_barcode_region`: uses local OpenCV-compatible detection and decoding.
- `suspicious_text_region`: uses the shared OCR Provider when installed and
  enabled; otherwise the UI clearly degrades to manual visual regions.
- `manual_visual_region`: validates user-created normalized rectangles and their
  time ranges.
- `manual_audio_interval`: validates user-selected mute intervals.

Failure of one scanner is recorded independently. Metadata cleanup and manual
redaction remain available when optional OCR is unavailable.

### 4.2 PrivacyRiskMap

The risk map is a new, versioned Resolve-domain document and is not added to the
frozen v0.1 `AnalysisReport`. Every risk contains:

- deterministic ID derived from input hash, scanner ID, risk type, interval, and
  normalized region when present;
- scanner ID and version;
- risk type and share-audience profile;
- finite start and end seconds;
- optional normalized rectangle or audio interval;
- detector-internal confidence and observable evidence;
- neutral description and limitations;
- recommended action;
- human decision: `unreviewed`, `allow`, or `redact`;
- optional user-edited region and chosen redaction style.

Risk ordering is deterministic by start time, risk priority, scanner ID, and ID.
Raw private OCR text and unredacted evidence remain in the private review area.

### 4.3 RedactionPlanner

The planner converts a reviewed risk map into an ordered, deterministic plan.
Every action declares:

- action ID, algorithm version, parameters, and affected interval;
- affected visual region, audio range, stream, chapter, or metadata scope;
- whether the action changes content semantics;
- required backend and optional package;
- whether explicit confirmation is mandatory;
- degradation and failure behavior.

The confirmation digest covers input hash, profile, complete effective
configuration, reviewed decisions, ordered actions, preview identity, expected
artifacts, source-read-only commitment, and verification policy. A worker must
reject a stale or mismatched digest in constant time.

### 4.4 RedactionExecutor

The executor uses argument-array FFmpeg subprocesses and bounded OpenCV frame
processing. It writes only to the job workspace and output directory.

- Visual tracks interpolate between reviewed key regions and add a configurable
  guard margin.
- Track gaps or confidence drops expand the protected region or stop for review;
  they never silently stop redaction.
- Blur, pixelation, solid fill, and crop are deterministic actions.
- Audio mute operates only on explicitly reviewed intervals.
- Metadata removal covers global, per-stream, chapter, attachment, filename, and
  generated thumbnail exposure according to the selected profile.
- Final files are delivered using temporary files and atomic replacement.

### 4.5 PrivacyVerificationGate

The output is probed and rescanned using the same applicable scanner versions and
configuration. Required checks include:

- decodability, expected duration, streams, and profile compatibility;
- absence of forbidden global, stream, and chapter metadata;
- continuous coverage of every confirmed visual region across its full interval;
- QR/barcode re-decoding failure for regions selected for redaction;
- no recovery of the original selected OCR result inside redacted regions;
- configured silence energy inside mute intervals and retained signal outside;
- no new severe black, freeze, or invalid crop interval;
- no absolute paths, usernames, private evidence, or raw sensitive text in the
  public package.

Unverified checks are not passing checks. High-risk uncertainty produces
`needs_review`, with a specific explanation and an edit-and-rerun path.

### 4.6 Artifact isolation

Artifacts are strictly separated:

```text
privacy-review-private/
  risk-map.json
  plan.json
  confirmation.json
  evidence/
  preview/

share-package/
  share-safe.mp4
  privacy-summary.json
  changes.json
  verification-report.json
```

The public package contains only output-root-relative forward-slash paths. It
does not contain unredacted evidence, original sensitive OCR text, absolute
paths, usernames, GPS, internal caches, or identity guesses.

## 5. Interfaces

### 5.1 CLI

The CLI entry point is:

```text
videoscope privacy INPUT --output OUTPUT
```

Non-interactive execution may scan and write a private risk map. Media-changing
execution requires either an interactive confirmation or a supplied matching
plan digest. Quality findings never alter the successful process exit code, but
input, processing, configuration, and internal failures use stable non-zero
codes consistent with existing CLI conventions.

### 5.2 Local Web API

The API reuses the existing job manager, storage, SSE, cancellation, retention,
and artifact authorization. Privacy-specific endpoints expose risk review,
region edits, preview preparation, confirmation, status, and artifacts through
the same core service used by the CLI. The API does not reimplement scanning or
redaction logic.

All artifact paths are allowlisted, normalized, containment-checked, and
restricted to their appropriate private or share scope. Error responses remove
absolute paths and sensitive OCR content.

### 5.3 Workbench

Desktop layout:

```text
top:    task status, local-processing notice, save/export controls
left:   audience profile, risk categories, processing strategy
center: video player, anonymous overlays, manual region tools
bottom: privacy timeline, audio intervals, zoom and frame controls
right:  risk list, decision, evidence, confidence and limitations
```

Selecting a risk seeks the video, highlights its interval and region, and opens
the decision controls. Region edits can apply to the current frame, one track,
or an explicit interval. A user can mark a false positive as allowed, and the
decision is recorded.

The terminal view offers download, unresolved-risk review, new task, and an
explicit local-data deletion action. Starting a new task clears UI and URL state
without implicitly deleting server artifacts.

Mobile keeps review, playback, timeline, confirmation, and download in a bottom
drawer. It directs complex frame-accurate region editing to desktop rather than
pretending to provide equal precision on a small screen.

## 6. Error handling and conservative degradation

- Scanner errors are visible and isolated; they do not become zero risks.
- Missing OCR provides an installation message and manual-region fallback.
- Missing FFmpeg or an undecodable input fails with a structured, sanitized
  error before a plan can be confirmed.
- Track gaps, malformed boxes, stale confirmation digests, and output-validation
  failures cannot produce `completed`.
- Cancellation and retry are idempotent and do not duplicate execution.
- A partially useful output may be retained only as a clearly labelled private
  artifact, never as a verified share package.
- Base tests remain offline, CPU-only, GPU-free, and model-free.

## 7. Testing and acceptance

Synthetic fixtures are generated locally and include:

- global, stream, chapter, author, title, device, location, and attachment tags;
- moving face-like regions with temporary occlusion and reappearance;
- static, moving, scaled, and edge-adjacent QR codes;
- Chinese and English phone, email, address, and account-like text;
- ordinary non-sensitive text as a negative sample;
- reviewed audio mute intervals and audible control intervals;
- a no-risk negative sample;
- input and output paths containing spaces, Chinese, and other Unicode.

Automated assertions must verify:

- source bytes and SHA-256 remain unchanged;
- private and share directories are isolated;
- public artifacts contain no private evidence, raw sensitive OCR text, absolute
  path, username, or GPS value;
- selected metadata is absent from the output probe;
- each redaction persists across the complete target interval, not one frame;
- selected QR codes cannot be decoded after redaction;
- selected text regions do not reproduce the original OCR result;
- mute-interval energy is below its configured threshold while control intervals
  retain audio;
- cancellation, failure, or digest mismatch cannot publish success artifacts;
- identical input and configuration produce deterministic risks, plans, IDs,
  decisions, and ordering;
- the CPU baseline works without OCR, GPU, model weights, or network access.

Release gates are:

- repository Python validation, lint, format, typing, and tests pass;
- React tests, strict TypeScript checks, and production build pass;
- real FFmpeg end-to-end redaction and rescan tests pass;
- wheel and sdist contain the current checked-in dashboard assets and no fixtures,
  private job data, cache, or personal path;
- manual browser acceptance covers upload, review, edit, preview, confirmation,
  recovery, cancellation, download, new task, and explicit deletion;
- Windows and Linux CI pass;
- documentation uses conservative wording and lists limitations.

Synthetic fixtures are engineering regression data, not evidence of real-world
privacy-detection accuracy. Broader annotated evaluation is required before any
accuracy claim.

## 8. Delivery order

Safe Sharing is implemented in small independently reviewed increments:

1. versioned privacy models, serialization, and deterministic IDs;
2. metadata scanner and share-package isolation;
3. manual visual regions and audio intervals;
4. anonymous face and QR/barcode CPU proposals;
5. optional OCR suspicious-text proposals and fallback behavior;
6. redaction planner, digest, preview, and explicit confirmation;
7. visual/audio executor and atomic artifact delivery;
8. output verification and `needs_review` policy;
9. CLI and local Web API;
10. React workbench, recovery, accessibility, and responsive behavior;
11. synthetic fixtures, end-to-end tests, documentation, packaging, and release
    audit.

Video Rescue starts only after this specification has its own completed plan and
implementation review. No Safe Sharing task may silently add rescue algorithms.
