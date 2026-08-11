# VideoScope Full Local Four-Mode Demo Source Design

Date: 2026-08-12

Status: approved design, awaiting written-spec review

## 1. Objective

Create one project-authored, copyright-clean source video that a zero-beginner
can load into VideoScope's complete local mode to demonstrate four independent
workflows with real outputs:

- A: Publish Ready;
- B: Video Rescue;
- C: Long Video to Useful Content;
- D: Safe Sharing.

The video is a validation source, not a marketing-only animation and not a
benchmark of real-world accuracy. Each workflow must preserve the source and
write a separate output. Claims are limited to what the actual local run and
verification artifacts show.

## 2. Deliverables

Local review deliverables live under `runs/full-local-demo/` until the user
approves publication:

- `VideoScope-Full-Local-Demo-Source.mp4`;
- `demo-manifest.json` with exact ranges, synthetic-risk coordinates, hashes,
  rendering versions, and intended validation uses;
- `README-demo.md` with zero-beginner A/B/C/D instructions;
- `source-contact-sheet.webp` and one result contact sheet per completed mode;
- one separate output directory for each A/B/C/D run;
- `verification-summary.json` recording actual statuses, actions, checks,
  limitations, and artifact hashes.

Tracked source files may live under `demos/full-local-four-mode/`. Rendered
video, temporary frames, local reports, and module outputs remain ignored until
separately reviewed and authorized for publication.

## 3. Source Media Contract

- Duration target: 42 seconds, tolerance one frame.
- Canvas: 1280x720 landscape.
- Frame rate: constant 24 fps.
- Video: H.264, yuv420p.
- Audio: AAC, stereo, 48 kHz.
- Container: MP4 with the metadata and fast-start state described below.
- Language: concise English plus Simplified Chinese visual labels.
- Assets: project-authored HTML/CSS/SVG, generated tones, and deterministic
  geometry only. No downloaded footage, remote image, remote font, or model.
- Personal data: none. Every apparent identifier is visibly marked fictional.

The final source remains normally decodable and browser-previewable. Problems
are deliberate, bounded, and observable; the source is not corrupted so
severely that one workflow prevents the other three from reading it.

## 4. Visual Identity

The composition uses VideoScope's Video Observatory identity:

- Obsidian: `#0B0E10` background;
- Deep Graphite: `#151A1D` panels;
- Soft Ivory: `#F1F4EF` primary text;
- Scope Cyan: `#56E0D0` detection and selection;
- Signal Violet: `#8D7DF7` advanced analysis;
- Diagnostic Lime: `#B7F36A` verified/pass;
- Warning Amber: `#F6B84A` review-needed;
- Critical Coral: `#FF6F61` privacy and serious issue markers.

Typography uses local system sans-serif fonts and tabular numerals. Motion is
functional: scan lines, timecode movement, range highlighting, and evidence
markers. It avoids decorative particles, remote assets, excessive glow, and
science-fiction HUD clutter.

## 5. Timeline and Observable Content

### 00:00-00:05 — Clean hook

Show the title "ONE SOURCE. FOUR LOCAL OUTCOMES." and its Chinese equivalent,
with a moving observatory grid and a clean synchronized cue tone. This segment
is intended to be retained by Useful Content.

### 00:05-00:10 — Bounded rescue evidence

Introduce deterministic near-dark exposure, high-frequency global luminance
variation, mild softening, and low-level noisy audio. Labels state that the
condition is an intentional project-authored test. The visual remains
decodable and the text remains partially legible.

This segment is designed to provide observable evidence for Video Rescue. It
does not guarantee that every Rescue action will be recommended; actual
scanner measurements and confirmation-gated planning remain authoritative.

### 00:10-00:20 — Useful three-step tutorial

Show three concise steps:

1. choose a local file;
2. review the measured plan;
3. confirm a separate output.

Each step has distinct motion and synchronized audio cues. This is the main
Useful Content chapter/highlight candidate.

### 00:20-00:25 — Low-information pause and repeated take

Hold the timeline, repeat a prior phrase card, and reduce information density.
This provides a reviewable segment that Useful Content can exclude without
claiming the segment is objectively worthless.

### 00:25-00:32 — Synthetic privacy review zone

Show a fixed right-side contact panel labelled in both languages:

`FICTIONAL DATA / 虚构数据`

- `demo.user@example.invalid`;
- `+1 202-555-0107`;
- `00.0000, 000.0000`;
- `PRIVATE TONE / 私密提示音`.

The `.invalid` domain, reserved 555 number, and zero coordinates prevent the
demo from containing a real identifier. The panel stays within normalized
bounding box `[0.58, 0.18, 0.94, 0.78]`. A distinctive locally generated tone
plays over the same interval. Safe Sharing can therefore demonstrate explicit
visual redaction and exact audio muting after human review.

### 00:32-00:36 — Mild motion instability and retake

Apply bounded deterministic camera-like displacement and repeat a short
instruction. This gives Rescue and Useful Content a second reviewable region
without simulating an identity or semantic failure.

### 00:36-00:42 — Clean verified ending

Return to clean, stable motion and show "SOURCE PRESERVED. OUTPUT VERIFIED."
with the Chinese equivalent. This segment is intended to be retained by Useful
Content and used as the final report/contact-sheet frame.

## 6. Controlled Post-Processing

HyperFrames renders a clean deterministic base. A local Python orchestrator
then invokes FFmpeg/ffprobe with argument arrays and `shell=False` to create the
validation source:

- apply only the declared time-bounded luminance, softening, and motion
  effects;
- apply bounded audio attenuation/noise and the privacy cue interval;
- attach clearly synthetic metadata fields for Safe Sharing metadata removal;
- omit fast-start optimization so Publish Ready can produce a verified
  fast-start copy;
- calculate SHA-256 by streaming;
- probe the final media and atomically write the manifest.

The orchestrator must not download assets, modify the source after hashing,
use `shell=True`, include personal paths in public artifacts, or encode a test
result based on the filename.

## 7. Four-Mode Validation

### A. Publish Ready

Use built-in profile `compatible_mp4`. Review and confirm the exact generated
plan. Expected classes of action are compatibility transcode/remux as measured,
metadata stripping, fast-start, and cover extraction. Acceptance requires:

- separate H.264/AAC MP4 output;
- source hash unchanged;
- declared profile checks passed;
- cover and public report present;
- no claim beyond the actual plan and verification result.

### B. Video Rescue

Run the real local Rescue scanner, review evidence/previews, and confirm only
actions supported by its measured plan. Candidate evidence includes the
00:05-00:10 luminance/audio region and 00:32-00:36 motion region. Acceptance
requires:

- source remains unchanged;
- a separate faithful output is verified;
- any improvement copy is published only if its existing preview and
  verification gates pass;
- `needs_review`, `partial`, or failed outcomes remain visibly distinct;
- the summary records which candidate anomalies were not acted upon.

### C. Long Video to Useful Content

Prepare chapters/highlights from the same source and review the suggested
ranges. The reference editorial selection is:

- keep 00:00-00:05;
- keep 00:10-00:20;
- keep 00:36-00:42;
- review/exclude 00:05-00:10, 00:20-00:25, 00:25-00:32, and 00:32-00:36.

Acceptance requires source-time mappings, reviewed chapters/highlights, a
separate deliverable, and no assertion that excluded material is universally
useless.

### D. Safe Sharing

Use audience profile `public`. Add an explicit manual visual risk for normalized
box `[0.58, 0.18, 0.94, 0.78]` during 00:25-00:32, select the profile's allowed
redaction style, and add an explicit manual audio mute risk for the same range.
Acceptance requires:

- metadata categories required by `public` are removed;
- the exact reviewed region is visually redacted for the exact interval;
- the exact reviewed audio interval is muted;
- final human review remains required;
- source remains unchanged and the share copy is separate.

## 8. Verification and Quality Gates

Before delivery:

1. `npx hyperframes lint` passes.
2. HyperFrames validation and layout inspection pass at dense samples,
   including all seven scene hero frames.
3. A high-quality MP4 render completes at 24 fps.
4. ffprobe confirms duration, streams, frame rate, pixel format, audio rate,
   and declared metadata.
5. Contact sheets confirm all intended labels remain readable and privacy data
   stays within the declared box.
6. The generator is run twice; final source and manifest hashes are identical.
7. Each A/B/C/D workflow is run locally using the real current pipeline.
8. `verification-summary.json` records actual status, checks, limitations, and
   hashes; failed or review-needed states are not rewritten as success.
9. `python scripts/validate.py` passes after any tracked source changes.

## 9. Error Handling

- Missing HyperFrames, browser, FFmpeg, or ffprobe stops generation with a
  clear actionable error.
- A render, probe, or module failure leaves the previous approved artifact
  intact and writes no partial final file.
- Any module result other than its real completed state is recorded exactly as
  returned and remains available for diagnosis.
- Local artifacts never contain personal absolute paths, API keys, account
  credentials, or real identity data.

## 10. Non-Goals

- No generated human face or identity claim.
- No real email, phone number, address, coordinates, or voice recording.
- No benchmark accuracy claim.
- No automatic confirmation of Rescue, Content, Publish Ready, or Safe Sharing
  actions.
- No cloud upload, model download, remote TTS, stock footage, or licensed
  music.
- No publication, GitHub push, release asset upload, or website deployment in
  this task without separate authorization.

## 11. Success Criterion

A zero-beginner can use one clearly documented local source to walk through
all four modules and receive separate, inspectable artifacts. The demo is
successful only when the actual reports show what each module did, what it did
not do, and whether human review is still required.
