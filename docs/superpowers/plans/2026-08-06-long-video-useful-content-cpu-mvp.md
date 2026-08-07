# Long Video to Useful Content CPU MVP Implementation Plan

Date: 2026-08-06

Status: ready for implementation review; execution is not yet authorized

**Goal:** Deliver VideoScope task mode C as a local, CPU-first workflow that
turns one authorized long video into a reviewed, playable, independently
verified useful-content package with exact source provenance.

**Architecture:** Add a versioned `videoscope.content` domain beside the frozen
Check foundation and the A Publish Ready, B Video Rescue, and D Safe Sharing
domains. A deterministic content map feeds one of three explicit planners:
Faithful Clean, Chaptered Full, or Selected Clips. The user edits and locks a
storyboard, previews every content-changing join, confirms one canonical plan
digest, and only then can a streaming FFmpeg executor create a new output. An
independent verifier and fixed artifact publisher decide whether the result is
`completed`, `partial`, `needs_review`, or `failed`.

**Tech stack:** Python 3.11+, Pydantic, NumPy, OpenCV headless, FFmpeg/ffprobe
argument-array subprocesses, Typer, FastAPI, React, strict TypeScript, Vitest,
pytest, Ruff, and mypy. The base path remains offline, CPU-only, GPU-free, and
model-free.

## Execution contract

- Implement tasks in order. A later task may start only when its focused tests
  and `python scripts/validate.py` pass.
- Use test-driven changes: add the smallest failing contract test, confirm the
  expected failure, implement, then run focused and unified validation.
- Each proposed commit below is local and task-scoped. Creating these commits,
  pushing, opening a Pull Request, tagging, releasing, or deploying requires
  separate user authorization.
- Never modify the source video. All transformed media is written to staging,
  verified independently, and published atomically to a new output tree.
- Never delete content solely because it is silent. An automatic Faithful Clean
  proposal needs configured corroborating observable evidence or an explicit
  user-authored exclude range.
- Locked keep ranges always win. Locked exclude ranges must never appear in the
  output. A target duration is a planning constraint, not a promise.
- Content-changing actions require a successful bounded preview and explicit
  confirmation of the exact canonical plan digest.
- The mandatory source map uses half-open intervals and covers every output
  interval without gaps, overlaps, or out-of-bounds source references.
- Public artifacts use normalized output-root-relative POSIX paths. Source
  paths, usernames, transcript text not explicitly exported, preview material,
  and private evidence never enter the public result tree.
- Do not introduce automatic ASR, remote APIs, identity inference, generated
  titles or summaries, semantic highlight ranking, creative reordering by
  default, auto-reframing, interpolation, super-resolution, generative repair,
  or an uncalibrated overall content/importance/virality score.
- Do not start Advanced AI work until every task and final gate in this plan is
  complete.

## Target file structure

```text
src/videoscope/content/
  __init__.py          stable public exports
  errors.py            structured and sanitized workflow errors
  models.py            strict versioned domain models
  serialization.py     canonical UTF-8 JSON and atomic writes
  transcript.py        local SRT/WebVTT validation and normalization
  features.py          reusable structural feature providers
  mapping.py           deterministic ContentMap construction
  planner.py           the three deterministic storyboard planners
  timeline.py          half-open interval and source-map composition
  preview.py           bounded join previews and identities
  commands.py          pure FFmpeg/ffprobe argument builders
  executor.py          staged native media execution
  verification.py      independent result checks and terminal gate
  artifacts.py         private/public isolation and atomic publication
  report.py            offline HTML content report
  pipeline.py          prepare, revise, preview, confirm, execute, cancel

src/videoscope/web/content_jobs.py

web/src/
  contentI18n.ts
  hooks/useContentLifecycle.ts
  components/ContentView.tsx
  components/ContentGoalSelector.tsx
  components/ContentMapTimeline.tsx
  components/ContentStoryboard.tsx
  components/ContentJoinPreview.tsx
  components/ContentPlanReview.tsx
  components/ContentResult.tsx

tests/content/
```

---

## Task 1: Freeze portfolio nomenclature and the C public contract

**Files:**

- Modify: `docs/product-spec.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`
- Create: `docs/content-schema.md`
- Create: `docs/long-video-content.md`
- Create: `tests/content/__init__.py`
- Create: `tests/content/test_documentation_contract.py`

**Steps:**

- [ ] Add a documentation contract test proving that the stable public mapping
  is A Publish Ready, B Video Rescue, C Long Video to Useful Content, and D Safe
  Sharing.
- [ ] Confirm the test fails on historical normative phrases such as
  `Resolve B: Safe Sharing` and `Resolve C: Video Rescue`.
- [ ] Replace stale normative prose and navigation labels. Preserve existing
  serialized schema values, CLI names, API routes, file names, and compatibility
  aliases; this is a nomenclature migration, not a data migration.
- [ ] Document the three C goals, input and transcript privacy boundaries,
  lifecycle, terminal outcomes, private/public artifact trees, canonical digest,
  mandatory source map, and Advanced AI phase boundary.
- [ ] Explicitly state that `AnalysisReport` remains frozen and C has a separate
  versioned technical report.

**Focused verification:**

```powershell
python -m pytest tests/content/test_documentation_contract.py -v
python scripts/validate.py
```

**Proposed commit:** `docs: define Long Video to Useful Content contract`

---

## Task 2: Add strict content-domain models and canonical JSON

**Files:**

- Create: `src/videoscope/content/__init__.py`
- Create: `src/videoscope/content/errors.py`
- Create: `src/videoscope/content/models.py`
- Create: `src/videoscope/content/serialization.py`
- Create: `tests/content/test_models.py`
- Create: `tests/content/test_serialization.py`

**Required interfaces:**

- `ContentGoal`: `faithful_clean`, `chaptered_full`, `selected_clips`.
- `ContentOutcome`: `completed`, `partial`, `needs_review`, `failed`,
  `cancelled`.
- Strict models: `ContentConfig`, `ContentSegment`, `ContentMap`,
  `ContentChapter`, `StoryboardItem`, `Storyboard`, `ContentAction`,
  `ContentPlan`, `ContentConfirmation`, `ContentSourceMapping`,
  `ContentChangeLog`, `ContentVerificationCheck`,
  `ContentVerificationReport`, `ContentArtifact`, and
  `ContentTechnicalReport`.
- Deterministic helpers for segment, chapter, storyboard-item, action, preview,
  mapping, and plan IDs plus a canonical `content_plan_digest`.
- Structured errors for input, transcript, mapping, planning, preview,
  confirmation, media, verification, artifact, cancellation, and internal
  failures. Public messages are bounded and sanitized.

**Steps:**

- [ ] Write RED tests for reverse, infinite, negative, overlapping, and
  out-of-bounds intervals; invalid SHA-256; unknown fields; unsafe artifact
  paths; duplicate IDs; inconsistent order; and invalid terminal reports.
- [ ] Add deterministic-ID and digest-invalidation tests covering changes to the
  source hash, transcript hash, effective config, locks, storyboard, preview
  identity, action order, and verification policy.
- [ ] Implement all models with `ConfigDict(extra="forbid")`, finite numeric
  validation, half-open intervals, stable enum strings, normalized relative POSIX
  paths, and explicit schema version `0.1`.
- [ ] Implement UTF-8 canonical JSON with `ensure_ascii=False`,
  `allow_nan=False`, stable keys, newline termination, validation before write,
  atomic replacement, and no partial file after failure.
- [ ] Test round trips in paths containing spaces, Chinese, and Unicode.

**Focused verification:**

```powershell
python -m pytest tests/content/test_models.py tests/content/test_serialization.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): add stable domain and JSON contracts`

---

## Task 3: Import and validate local SRT and WebVTT evidence

**Files:**

- Create: `src/videoscope/content/transcript.py`
- Create: `tests/content/test_transcript.py`
- Create: `tests/fixtures/content/valid_中文.srt`
- Create: `tests/fixtures/content/valid.vtt`
- Create: `tests/fixtures/content/malformed.srt`

**Steps:**

- [ ] Add RED tests for BOM handling, CRLF/LF, comma and dot milliseconds,
  multiline Unicode cues, HTML-like text preservation as text, overlapping cues,
  negative/reverse/out-of-range times, malformed blocks, duplicate cue IDs,
  maximum cue count, maximum text length, and deterministic normalization.
- [ ] Implement local-only SRT/WebVTT parsing without network access, model
  import, or shell invocation.
- [ ] Normalize validated cues into strict `TranscriptCue` values and calculate a
  hash over the normalized representation; never trust file extension alone.
- [ ] Treat invalid timing as structured transcript evidence failure. The
  pipeline may continue without transcript evidence but must surface
  `needs_review`; it must not silently use malformed text.
- [ ] Ensure public reports contain cue IDs and only explicitly exported text.
  Private normalized cues stay under `content-review-private/`.

**Focused verification:**

```powershell
python -m pytest tests/content/test_transcript.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): validate local timed transcripts`

---

## Task 4: Build reusable structural feature providers

**Files:**

- Create: `src/videoscope/content/features.py`
- Create: `tests/content/test_features.py`
- Modify only if required: `src/videoscope/video/probe.py`
- Modify only if required: `src/videoscope/video/sampling.py`

**Steps:**

- [ ] Define read-only provider protocols for metadata, scene boundaries, frame
  samples, silence/loudness intervals, near-black observations, and repeated-frame
  observations. Providers return versioned evidence and warnings, not plans.
- [ ] Add RED tests proving one shared probe and sampling pass, deterministic
  ordering, configured timeouts, sanitized stderr, cancellation, provider
  isolation, and partial results when one optional provider fails.
- [ ] Reuse existing probe, scene, sampling, and detector measurements through
  stable adapters. Do not instantiate one Detector from another and do not mutate
  an existing `AnalysisReport`.
- [ ] Add a pure FFmpeg silence/loudness command builder using argument arrays,
  `shell=False`, bounded output, and locale-independent parsing.
- [ ] Prove no-audio inputs continue with an explicit unavailable feature and no
  fabricated silence evidence.

**Focused verification:**

```powershell
python -m pytest tests/content/test_features.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): collect shared structural evidence`

---

## Task 5: Construct the deterministic content map

**Files:**

- Create: `src/videoscope/content/mapping.py`
- Create: `tests/content/test_mapping.py`

**Steps:**

- [ ] Add RED tests for boundary union, scene and silence alignment, signal
  coalescing, transcript cue references, representative evidence selection,
  source-order indices, locked keep/exclude precedence, user chapters, empty
  features, short videos, and determinism under provider-result permutation.
- [ ] Implement interval math rather than per-frame arrays where possible.
  Segment boundaries come only from validated source duration, observations,
  transcript cues, and user-authored ranges.
- [ ] Record observable measurements, algorithm/provider versions, effective
  thresholds, eligibility, limitations, and private relative evidence for every
  segment. Do not add an importance or global content score.
- [ ] Make a failed provider visible in `ContentMap.warnings` and execution
  records while retaining other valid evidence.
- [ ] Reject a user range outside source duration rather than silently clipping
  it. Adjacent exactly compatible intervals may merge deterministically.

**Focused verification:**

```powershell
python -m pytest tests/content/test_mapping.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): build deterministic content maps`

---

## Task 6: Plan editable storyboards for all three C goals

**Files:**

- Create: `src/videoscope/content/timeline.py`
- Create: `src/videoscope/content/planner.py`
- Create: `tests/content/test_timeline.py`
- Create: `tests/content/test_planner.py`

**Steps:**

- [ ] Add RED tests for half-open subtraction, union, ordering, duration
  conservation, source-map composition, exact-edge locks, and zero-length
  rejection.
- [ ] Add Faithful Clean tests proving silence alone is never removable,
  configured corroborating evidence is required, guard context survives,
  accepted ranges are exact, source order is preserved, locked keep wins, locked
  exclude is absent, and unsafe target-duration pressure cannot force deletion.
- [ ] Add Chaptered Full tests proving the entire source remains present,
  boundaries are structural and deterministic, neutral chapter names are used
  without trusted text, user names are preserved, and invalid chapters fail.
- [ ] Add Selected Clips tests for explicit keep ranges, labels, optional
  per-clip export, source-order default, overlap handling, and a separate explicit
  reorder acknowledgement bound into the plan digest.
- [ ] Implement planners as pure deterministic functions. Every keep, remove,
  chapter, reorder, and fallback decision records its reason and source evidence.
- [ ] When no safe shortening exists, produce a full-length reviewable plan or a
  structured fallback to Chaptered Full/manual Selected Clips; do not invent a
  highlight.

**Focused verification:**

```powershell
python -m pytest tests/content/test_timeline.py tests/content/test_planner.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): plan faithful useful-content storyboards`

---

## Task 7: Create private bounded join previews and bind confirmation

**Files:**

- Create: `src/videoscope/content/preview.py`
- Create: `tests/content/test_preview.py`
- Modify only if reusable contracts require it: `src/videoscope/resolve/preview.py`

**Steps:**

- [ ] Add RED tests for preview range bounds, first/last-source boundaries,
  maximum preview duration, exact accepted action ranges, retained-source
  lifetime, filename/path safety, cancellation, failed preview cleanup, and
  deterministic preview identity.
- [ ] Produce source-left, source-right, and joined preview media only under
  `content-review-private/preview/`; previews are never public artifacts.
- [ ] Bind each preview identity to source hash, transcript hash, action ID,
  exact ranges, encoding parameters, and resulting preview hash.
- [ ] Require every content-changing action to have a successful matching
  preview before confirmation. A stale/missing preview blocks only the affected
  action and leaves the plan reviewable.
- [ ] Implement retained-media ownership explicitly; close handles and release
  temporary sources on success, cancellation, refresh recovery, expiry, and
  deletion.

**Focused verification:**

```powershell
python -m pytest tests/content/test_preview.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): bind private join previews to plans`

---

## Task 8: Execute confirmed ranges and write exact source maps

**Files:**

- Create: `src/videoscope/content/commands.py`
- Create: `src/videoscope/content/executor.py`
- Create: `tests/content/test_commands.py`
- Create: `tests/content/test_executor.py`

**Steps:**

- [ ] Add pure command-builder tests for Windows/Unix executable names, Unicode
  paths, hard joins, bounded audio fades, video with/without audio, selected clip
  export, chapter metadata, safe codecs, timeout, and absence of shell syntax.
- [ ] Add executor RED tests for stale plan/confirmation/source/transcript hashes,
  unaccepted actions, exact range fidelity, source-order enforcement, explicit
  reorder confirmation, subprocess failure, cancellation, staging cleanup, and
  source byte identity before/after execution.
- [ ] Stream each confirmed source range with FFmpeg. Do not load the complete
  video or full-resolution frame sequence into memory.
- [ ] Build `source-map.json` from the confirmed storyboard and measured output
  boundaries. Each mapping records output range, exact source range, order,
  transition, unchanged/transformed state, and originating storyboard/action ID.
- [ ] Use hard joins by default. A bounded audio fade is allowed only when
  present in the confirmed plan; no generated video fill or decorative
  transition is allowed.
- [ ] Write only to a pending staging tree. Nothing becomes public until
  verification succeeds.

**Focused verification:**

```powershell
python -m pytest tests/content/test_commands.py tests/content/test_executor.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): render confirmed timelines with provenance`

---

## Task 9: Independently verify media, mappings, locks, and joins

**Files:**

- Create: `src/videoscope/content/verification.py`
- Create: `tests/content/test_verification.py`

**Steps:**

- [ ] Add injected-result RED tests for output/clip decoding, duration and stream
  inventory, source-map coverage, output/source bounds, locked keep presence,
  locked exclude absence, source order, explicit reorder, chapter/subtitle
  timing, source hash, public allowlist, and path privacy.
- [ ] Add media-regression tests for new long black intervals, new repeated-frame
  intervals, audio discontinuity, and fixed A/V residual. Verification must use
  independent probes/measurements rather than executor success flags.
- [ ] Implement required/optional check classification. Required failed checks
  yield `failed`; required inconclusive checks yield `needs_review`; verified
  incomplete selected clips may yield `partial` with exact missing ranges.
- [ ] Make `completed` impossible unless every required check passes and every
  mandatory artifact exists.
- [ ] Sanitize command diagnostics and never include transcript contents,
  absolute source/workspace paths, or tracebacks in public reports.

**Focused verification:**

```powershell
python -m pytest tests/content/test_verification.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): independently verify useful-content outputs`

---

## Task 10: Publish fixed artifacts and an offline traceable report

**Files:**

- Create: `src/videoscope/content/artifacts.py`
- Create: `src/videoscope/content/report.py`
- Create: `src/videoscope/reporting/templates/content_report.html.j2`
- Create: `tests/content/test_artifacts.py`
- Create: `tests/content/test_report.py`

**Steps:**

- [ ] Add RED tests for the exact private and public allowlists, path traversal,
  symlink/reparse-point escape, collision, atomic replacement, cancellation,
  incomplete staging, expiry, cleanup, and public/private leakage.
- [ ] Publish only verified files to `content-output/`: `useful-content.mp4`,
  `chapters.json`, `source-map.json`, `changes.json`, `technical-report.json`,
  `report.html`, explicitly requested validated `subtitles.srt`, and explicitly
  requested `clips/`.
- [ ] Render an offline HTML report from validated models only. Include goal,
  outcome, source coverage, chapters, storyboard decisions, exact change log,
  source mappings, verification status, limitations, and playable relative
  artifacts. Do not rerun analysis or invent recommendations in HTML.
- [ ] Escape all text, load no CDN/font/telemetry/remote resource, show no
  absolute path, and make missing optional evidence non-fatal.
- [ ] Keep transcript-normalized JSON, evidence, preview, plan, and draft
  storyboard in `content-review-private/` only.

**Focused verification:**

```powershell
python -m pytest tests/content/test_artifacts.py tests/content/test_report.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): publish verified traceable content packages`

---

## Task 11: Orchestrate the lifecycle and expose the CLI

**Files:**

- Create: `src/videoscope/content/pipeline.py`
- Modify: `src/videoscope/cli.py`
- Create: `tests/content/test_pipeline.py`
- Create: `tests/test_content_cli.py`

**Lifecycle:**

```text
created -> probing -> mapping -> planning -> awaiting_review
        -> previewing -> ready_to_confirm -> rendering -> verifying
        -> completed | partial | needs_review | failed | cancelled
```

**Steps:**

- [ ] Add pipeline RED tests for all three goals, progress order, provider
  failure, no-safe-removal fallback, stale review, preview failure, exact
  confirmation, render failure, verification failure, cancellation at every
  stage, retry, workspace retention, cleanup, and deterministic reruns.
- [ ] Implement one core pipeline shared by CLI and Web. Separate `prepare`,
  storyboard revision, `preview`, `confirm`, `execute`, `cancel`, and result-read
  operations; never duplicate media logic in an adapter.
- [ ] Add `videoscope content INPUT --goal ... --output ...` with local transcript,
  keep/exclude/locked ranges, chapter markers, target duration, reviewed plan,
  `--yes`, `--keep-workspace`, `--quiet`, and JSON-only/report controls.
- [ ] Interactive execution displays the exact digest and asks for confirmation
  after previews. Non-interactive content-changing execution requires an
  explicitly supplied reviewed plan plus `--yes`; it cannot auto-accept newly
  generated proposals.
- [ ] Use established exit codes: success even when content changes exist;
  distinct input/config, media, needs-review/verification, cancellation, and
  internal failures. Document the exact mapping and test it.

**Focused verification:**

```powershell
python -m pytest tests/content/test_pipeline.py tests/test_content_cli.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(content): add end-to-end content pipeline and CLI`

---

## Task 12: Add loopback-only local Web jobs and API

**Files:**

- Create: `src/videoscope/web/content_jobs.py`
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/storage.py`
- Modify: `src/videoscope/web/app.py`
- Create: `tests/web/test_content_api.py`
- Modify: `docs/web-api.md`

**Required routes:**

```text
POST   /api/content/jobs
GET    /api/content/jobs/{job_id}
GET    /api/content/jobs/{job_id}/events
PUT    /api/content/jobs/{job_id}/storyboard
POST   /api/content/jobs/{job_id}/previews
POST   /api/content/jobs/{job_id}/confirm
GET    /api/content/jobs/{job_id}/artifacts/{path}
DELETE /api/content/jobs/{job_id}
```

**Steps:**

- [ ] Add RED tests for bounded upload, final ffprobe validation, unsafe MIME and
  extension, path traversal, artifact allowlist, Host/Origin policy, no wildcard
  CORS, random job IDs, SSE event order/reconnect, concurrent revision conflict,
  stale digest, cancellation, expiry, recovery after restart, deletion, and
  sanitized errors.
- [ ] Reuse the existing application data directory, storage safety helpers, CPU
  concurrency controls, retention policy, and same core `ContentPipeline`.
- [ ] Keep uploaded source, transcript, storyboard, previews, and drafts private.
  Only fixed verified public artifacts are downloadable through the artifact
  route.
- [ ] Persist enough versioned state for refresh recovery without persisting
  transient handles. Recover terminal state deterministically after interruption.
- [ ] Keep the default server bound to loopback and do not add accounts, cloud
  storage, public share hosting, or remote analysis.

**Focused verification:**

```powershell
python -m pytest tests/web/test_content_api.py -v
python scripts/validate.py
```

**Proposed commit:** `feat(web): add local useful-content job API`

---

## Task 13: Build the bilingual accessible C workbench

**Files:**

- Create: `web/src/contentI18n.ts`
- Create: `web/src/hooks/useContentLifecycle.ts`
- Create: `web/src/components/ContentView.tsx`
- Create: `web/src/components/ContentGoalSelector.tsx`
- Create: `web/src/components/ContentMapTimeline.tsx`
- Create: `web/src/components/ContentStoryboard.tsx`
- Create: `web/src/components/ContentJoinPreview.tsx`
- Create: `web/src/components/ContentPlanReview.tsx`
- Create: `web/src/components/ContentResult.tsx`
- Create: `web/src/components/ContentView.test.tsx`
- Create: `web/src/hooks/useContentLifecycle.test.tsx`
- Create: `web/src/contentI18n.test.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/types.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

**Steps:**

- [ ] Add typed API-client tests and lifecycle-hook tests for upload, progress,
  refresh recovery, revision conflict, preview, digest confirmation, execution,
  cancellation, deletion, SSE reconnect, and terminal errors.
- [ ] Add component RED tests for all three goals, empty/no-safe-proposal state,
  structural lanes, exact range editing, lock controls, keyboard endpoint steps,
  chapter renaming, reorder warning, join preview, plan-digest review, result
  source-map seeking, partial/needs-review/failed states, and mobile layout.
- [ ] Implement English and Simplified Chinese labels from one dictionary. The
  `what912` attribution remains invariant and is not translated.
- [ ] Synchronize source player, structural lanes, storyboard, join preview,
  output estimate, and source-map highlighting. Avoid drag-only interaction;
  provide labelled buttons/inputs and visible focus states.
- [ ] Preserve A/B/D behavior and existing report/workspace routes. Update public
  task-mode navigation to the stable A/B/C/D mapping without changing serialized
  compatibility values.
- [ ] Respect `prefers-reduced-motion`, avoid remote fonts/CDNs, prevent horizontal
  overflow, and keep large evidence lazy/bounded.

**Focused verification:**

```powershell
Set-Location web
npm test -- --run
npm run build
Set-Location ..
python scripts/validate.py
```

**Proposed commit:** `feat(web): add useful-content review workbench`

---

## Task 14: Generate deterministic content fixtures and real media gates

**Files:**

- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`
- Create: `tests/content/test_fixture_content.py`
- Create: `tests/content/test_native_media.py`
- Create: `tests/content/test_performance.py`
- Modify: `scripts/smoke_test.py`

**Fixtures:**

- `content_meeting_structure.mp4`: ordered visual sections with speech-like
  audio, meaningful short pauses, and one corroborated long low-information gap.
- `content_tutorial_chapters.mp4`: distinct ordered scenes and chapter gaps.
- `content_locked_context.mp4`: a removable-looking interval overlapping a
  locked keep range.
- `content_join_regression.mp4`: boundaries able to expose black/freeze/audio
  join regressions.
- Valid, overlapping, out-of-range, malformed, and Chinese SRT/WebVTT files.

**Steps:**

- [ ] Generate all media locally with FFmpeg lavfi/programmatic assets using
  argument arrays and no downloads. Record source recipe, expected ranges,
  tolerances, and purpose in the manifest; do not commit large generated media.
- [ ] Generate twice and assert deterministic manifest/annotation data. If
  container byte hashes cannot be deterministic across supported FFmpeg builds,
  assert decoded signal/metadata determinism and document that boundary rather
  than falsifying a hash guarantee.
- [ ] Run native Faithful Clean and prove exactly accepted removals, locked
  survival, source byte identity, source-map duration, output decode, and no new
  required black/freeze/audio/A-V regression.
- [ ] Run native Chaptered Full and prove the entire source timeline survives and
  chapters are valid.
- [ ] Run native Selected Clips and prove exact accepted ranges, source-order
  default, explicit reorder behavior, individual clips, and concatenated mapping.
- [ ] Add performance-bound tests for sample reuse, bounded preview duration,
  bounded memory-facing collections, transcript/chapter/storyboard limits,
  cancellation, and deterministic cleanup. Do not present synthetic timing as
  real-world performance.
- [ ] Extend the clean-wheel smoke runner with separate purpose-built C inputs for
  all three goals; do not reuse one fixture to fake distinct contracts.

**Focused verification:**

```powershell
python scripts/generate_test_videos.py --force
python -m pytest tests/content/test_fixture_content.py tests/content/test_native_media.py tests/content/test_performance.py -v
python scripts/validate.py
```

**Proposed commit:** `test(content): prove native useful-content workflows`

---

## Task 15: Complete distribution, documentation, and clean-wheel smoke

**Files:**

- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `CITATION.cff`
- Modify: `docs/release-checklist.md`
- Modify: `release-audit.md`
- Modify: `scripts/audit_distribution.py`
- Modify: `.github/workflows/ci.yml` only if required by existing CI structure
- Add package-data declarations only for required C templates/static assets

**Steps:**

- [ ] Set the integrated development version chosen at implementation review
  (proposed `0.6.0.dev0`) consistently in package metadata, CLI, reports,
  changelog, citation, and docs. Do not create a release tag.
- [ ] Document installation, FFmpeg requirement, all three CLI workflows, local
  transcript privacy, Web workflow, exact output tree, source-map semantics,
  limitations, configuration, and recovery/deletion behavior.
- [ ] Update CI to generate fixtures and run focused native C coverage on Linux
  and Windows/Python 3.11 and 3.12 without AI models, GPU, network-dependent
  tests, PyPI upload, or GitHub Release.
- [ ] Build wheel and sdist. Audit them for generated videos, runs, workspaces,
  caches, private transcripts/previews/evidence, absolute personal paths, and
  accidental AI/Web dependencies. Confirm required templates are present.
- [ ] In a clean temporary virtual environment, install the exact built base
  wheel and run `--version`, `doctor`, one Check, A, D, B, and all three C smoke
  workflows. Confirm no model download or network access.
- [ ] Install the exact wheel with `[web]` in a separate clean environment and
  run a loopback API smoke. Do not install `[ai]` or `[ocr]` for the base gate.
- [ ] Record exact commands, package hashes, test counts, skips, platform,
  FFmpeg/ffprobe versions, external access, and remaining human gates. Never
  claim an unrun cross-platform or long-video test passed.

**Focused verification:**

```powershell
python scripts/validate.py
python -m build
python scripts/audit_distribution.py
Set-Location web
npm ci
npm test -- --run
npm run build
Set-Location ..
python scripts/smoke_test.py --wheel (Get-ChildItem dist\*.whl | Select-Object -First 1 -ExpandProperty FullName)
```

**Proposed commit:** `release: prepare Long Video to Useful Content CPU MVP`

---

## Task 16: Final independent audit and integration decision

**Files:**

- Modify only audited corrections required by evidence.
- Update: `release-audit.md`
- Update: `docs/release-checklist.md`

**Steps:**

- [ ] Review every C diff against the approved design, AGENTS rules, stable
  A/B/C/D nomenclature, frozen v0.1 contracts, and explicit non-goals.
- [ ] Search production code and artifacts for `shell=True`, `os.system`, remote
  resources, model downloads, path traversal, unsafe archive/artifact serving,
  credentials, usernames, email addresses, absolute personal paths, transcript
  leakage, private preview leakage, and unescaped HTML.
- [ ] Audit source immutability, exact accepted action ranges, lock precedence,
  plan/preview/digest binding, retained-source lifecycle, public allowlist,
  source-map coverage, fail-closed verification, cleanup, cancellation, and
  deterministic reruns.
- [ ] Run the full Python validation, native fixture suite, frontend test/build,
  distribution audit, and exact-wheel clean smoke again from a clean state.
- [ ] Separate results into passed, failed, needs human inspection, externally
  unverified, and release blockers. Fix real blockers with focused regression
  tests; never weaken assertions or relabel an inconclusive gate as passed.
- [ ] Perform human browser/media review only with explicit authorization:
  bilingual desktop/mobile flows, keyboard operation, join audibility, chapter
  navigation, selected-clip fidelity, download usability, and no private data in
  public artifacts.
- [ ] When all automated blockers close, present the local branch and exact
  commits for user approval. Do not merge, push, open a PR, tag, publish, or
  deploy without a new explicit instruction.

**Final verification:**

```powershell
python scripts/validate.py
python scripts/generate_test_videos.py --force
python -m pytest tests/content tests/web/test_content_api.py tests/test_content_cli.py -v
python -m build
python scripts/audit_distribution.py
Set-Location web
npm test -- --run
npm run build
Set-Location ..
git status --short
```

**Proposed corrective commits:** one narrow local commit per independently
reviewed blocker. Do not create an empty “audit passed” commit.

## Completion definition

C CPU MVP is complete only when:

1. all three goals create real playable results or an honest review/failure
   outcome;
2. every content-changing output has an exact validated source map;
3. no unconfirmed or stale action can execute;
4. locks, source order, source immutability, private/public isolation, and
   fail-closed verification are proven by unit and native media tests;
5. CLI and bilingual local Web use the same pipeline and recover safely;
6. the clean base wheel performs all three C workflows without AI, OCR, GPU,
   network, or model download;
7. build, distribution audit, frontend tests/build, unified validation, and
   exact-wheel smoke pass with recorded evidence;
8. remaining human and external gates are explicit; and
9. Advanced AI has not started.
