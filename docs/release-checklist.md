# GenVideoScope v0.7.0 Advanced AI development release checklist

Target:

- repository: `GenVideoScope`
- PyPI distribution: `genvideoscope`
- Python package: `videoscope`
- CLI: `videoscope`
- development version: `0.7.0.dev0`
- candidate tag: not assigned; tagging requires a separate release audit

Do not publish while any item marked **blocking** is unresolved.

## Source and identity

- [x] `pyproject.toml` name is `genvideoscope`.
- [x] package import and CLI remain `videoscope`.
- [x] `videoscope --version` prints `VideoScope 0.7.0.dev0` in the final
  clean-wheel run.
- [x] GitHub repository name and links use `GenVideoScope`.
- [ ] PyPI name availability and project ownership are checked manually.
- [ ] tag points to the exact reviewed commit.

## Security and privacy

- [x] secret and personal-path scans are clean after excluding deliberate test
  strings.
- [x] no production `shell=True`, `os.system`, or user-built shell command.
- [x] upload size, prompt size, and configuration size are bounded.
- [x] ffprobe, not extension or MIME, validates uploaded video content.
- [x] artifact, evidence, and upload paths have traversal tests.
- [x] default Web bind, Host, Origin, and CORS behavior are local-only.
- [x] HTML uses autoescaping and rejects unsafe local artifact paths.
- [x] reports contain no absolute workspace path.
- [ ] maintainers manually review what filename, prompt, SHA-256, timestamps,
  evidence frames, and bundled video reveal before sharing a report.

## Dependency boundaries

- [x] base wheel installs no AI, OCR, or Web runtime.
- [x] `ai`, `ocr`, `asr`, `web`, and `all` remain explicit extras.
- [x] optional imports are lazy.
- [x] no model weights are included or downloaded by base tests.
- [ ] the exact resolved transitive licenses are reviewed.
- [ ] `NOTICE` and `docs/third-party-licenses.md` match declarations.
- [x] FFmpeg remains an external executable and is not bundled.

## API and compatibility

- [x] report schema remains `0.1`, or migration notes are added before changing
  it.
- [x] `AnalysisConfig` rejects unknown fields.
- [x] CLI exit codes remain documented and tested.
- [x] Detector protocol, requirements, configuration model, Finding shape, and
  deterministic ordering remain tested.
- [x] optional detector/provider failure preserves completed CPU results.

## Advanced AI trust contract

- [x] Advanced AI accepts only a trusted transcript or an explicitly enabled,
  local Faster Whisper provider; the base wheel does not import or install it.
- [x] Ollama is restricted to loopback endpoints, never pulls a model, and
  records the selected provider/model with every suggestion batch.
- [x] Suggestions are grounded to transcript time ranges and carry rationale,
  evidence, limitations, confidence and deterministic IDs.
- [x] Every suggestion starts rejected. Non-interactive application requires
  an explicit reviewed manifest or `--accept-all`; the Web UI requires an
  explicit decision for each item.
- [x] Accepted chapter/highlight ranges enter the ordinary C review, preview,
  exact-confirmation, rendering and verification path. Text-only summaries and
  titles never edit media.
- [x] AI errors, cancellation, stale C revisions and input-hash changes fail
  closed without discarding the source or bypassing CPU results.
- [x] Fake providers cover deterministic and failure paths without network,
  model download, GPU or private-media upload.
- [ ] Maintainers manually evaluate an authorized real transcript/model pair;
  Fake-provider engineering tests are not semantic-quality evidence.

## Publish Ready contract

- [x] source videos remain byte-for-byte unchanged and outputs use a separate
  directory with `publish-ready.mp4`.
- [x] the exact built-in profiles are `compatible_mp4`,
  `social_vertical_9_16`, and `social_horizontal_16_9`.
- [x] social profiles use scale-and-pad without cropping the source frame.
- [x] interactive processing displays a plan and asks for confirmation;
  non-interactive processing requires an explicitly reviewed `--yes`.
- [x] `publish-ready.mp4`, `cover.jpg`, `changes.json`, and
  `technical-report.json` exist in a successful clean-wheel smoke run.
- [x] output verification is `passed`; `needs_review` exits with status `5` and
  is never presented as completed.
- [x] public JSON uses output-relative POSIX paths and does not disclose source
  or workspace paths.
- [x] FFmpeg/ffprobe remain external; the tested build provides H.264
  (`libx264`) and AAC encoding.
- [x] documentation does not present technical verification as artistic-quality
  proof or promise that current platform rules are permanent.

## Safe Sharing contract

- [x] source bytes and SHA-256 remain unchanged; all rendering writes a new
  `share-safe.mp4` only after exact-digest confirmation.
- [x] `privacy-review-private/` and `share-package/` are physically separate;
  public artifact routes and archives cannot traverse into private evidence.
- [x] public packages contain exactly `share-safe.mp4`, `changes.json`,
  `privacy-summary.json`, `verification.json`, `technical-report.json`, and
  `manifest.json`.
- [x] public JSON contains no raw OCR text, unredacted evidence, GPS, username,
  private path, job root, pending directory or staging identity.
- [x] manual visual and audio selections are duration-validated and included in
  the confirmed deterministic plan.
- [x] scanner failures are visible; missing OCR supplies an actionable manual
  fallback and cannot mean “no text risk”.
- [x] a preview exists only in the private root and is reviewed before the exact
  plan digest is submitted.
- [x] digest mismatch, duplicate confirmation, cancellation, failed rescan and
  incomplete required checks cannot publish `completed`.
- [x] `needs_review` is visibly distinct from completion and preserves an edit/
  rerun path without claiming absolute safety or authorizing any public artifact.
- [x] only `completed` atomically publishes the six-file public package;
  `needs_review`, `partial`, `failed`, and `cancelled` remove pending candidates.
- [x] the private preview executor limits FFmpeg input/output to the configured
  preview duration before visual processing and never creates `share-package/`.
- [x] explicit Web deletion removes retained local job data without following a
  symlink, junction, reparse point or parent traversal.
- [x] docs state that automatic proposals are heuristic, anonymous face regions
  are not identity recognition, sensitive speech is manual in the CPU MVP, and
  human review is mandatory.

## Video Rescue contract

- [x] Conservative and Balanced both complete an exact-digest confirmed flow in
  the clean base-wheel smoke environment.
- [x] Conservative publishes a verified `faithful-rescue.mp4` and no improved
  artifact; Balanced publishes distinct, independently verified faithful and
  improved media when the fixture supplies measured improvement evidence.
- [x] the source SHA-256 is unchanged after preview, confirmation, processing,
  verification, cancellation, partial salvage, and Web recovery.
- [x] private previews/staging stay in `rescue-review-private/`; fixed public
  documents and verified media only appear in `rescue-output/`.
- [x] public Rescue JSON validates schema 0.2, records artifact roles, uses only
  output-relative POSIX paths, and contains no personal/workspace path.
- [x] schema 0.2 legacy action records preserve whether `action_executions` was
  absent or explicitly empty; canonical writers emit the ledger and HTML labels
  a missing legacy ledger as unknown rather than successful.
- [x] an interrupted persisted Rescue state without an exact previewed-action
  binding fails closed and requires a new preparation rather than execution.
- [x] partial salvage publishes exact source mappings and unrecovered ranges;
  `needs_review` and `failed` never masquerade as complete recovery.
- [ ] base CI installs no AI/OCR extra, downloads no model, and requires no GPU.
- [x] documentation states that filters cannot reconstruct absent source
  information and makes no unmeasured recovery/accuracy/performance claim.

## Long Video to Useful Content contract

- [x] Faithful Clean, Chaptered Full and Selected Clips use one local CPU
  pipeline and one stable content schema without changing the frozen v0.1 report.
- [x] every content-changing action has exact source ranges, a bounded private
  preview identity and exact-set confirmation bound to source, transcript,
  configuration, storyboard, plan, locks and verification policy.
- [x] locked keep ranges survive removals; default source order is preserved;
  reordering requires an explicit configuration switch and acknowledgement.
- [x] source bytes are unchanged; verified outputs include a complete exact
  source map and public paths never expose source/workspace absolute paths.
- [x] private transcript text, evidence, waveforms, thumbnails and previews stay
  in `content-review-private/`; only allowlisted verified files enter
  `content-output/`.
- [x] native FFmpeg tests prove exact reviewed removal, full-timeline chapters,
  selected clips, explicit reorder, decoding, stream inventory, join regression
  checks, A/V residual and deterministic decoded fixture signals.
- [x] the clean base-wheel smoke exercises all three goals without AI, OCR, GPU,
  model download, remote API or video upload.
- [ ] representative authorized meeting/tutorial/recording inputs receive human
  usefulness, playback, join-audibility and performance review.

## Performance and cleanup

- [x] input hashing and upload use bounded chunks.
- [x] FFmpeg extracts sampled frames without loading the video into memory.
- [x] pipeline samples frames once and shares the resulting context.
- [x] matching AI detectors share provider instances and embedding cache keys.
- [x] original video is not bundled unless explicitly requested.
- [x] workspaces, upload staging, cancelled jobs, and expired jobs are cleaned.
- [ ] long-video memory and disk behavior is manually profiled on a realistic
  authorized input.
- [x] configured C transcript, chapter, storyboard, sample and private-preview
  limits are validated and cancellation/cleanup paths are covered.

## Documentation and examples

- [x] README first screen has value, install, analyze, screenshot, CPU detectors,
  and local-first statement.
- [x] source, wheel, extras, Web, Benchmark, and API commands match the candidate.
- [x] CPU heuristics and AI/OCR heuristics are clearly distinguished.
- [x] limitations do not claim unmeasured accuracy.
- [x] examples import or parse under supported Python versions.
- [ ] PowerShell and POSIX shell examples are manually checked on their target
  platforms.
- [ ] `examples/privacy-review.example.json` validates against the strict Web
  review request schema after replacing the example risk ID with one from the
  current private risk map.

## Required automated verification

Run from the repository root:

```powershell
python scripts/generate_test_videos.py --force
python scripts/validate.py
python -m pytest tests/resolve/test_fixture_publish.py -q
python -m pytest tests/privacy/test_fixture_privacy.py -q
python -m pytest tests/rescue/test_fixture_rescue.py -q
cd web
npm test
npm run build
cd ..
python -m build
python scripts/audit_distribution.py dist
$wheel = (Get-ChildItem dist\*.whl | Select-Object -First 1).FullName
python scripts/smoke_test.py --wheel $wheel
```

Clean-profile checks:

```powershell
python -m pip install dist/genvideoscope-0.7.0.dev0-py3-none-any.whl
python -m videoscope --version
python -m videoscope doctor
python -m videoscope publish tests/fixtures/generated/publish_av.mp4 `
  --profile compatible_mp4 `
  --output publish-smoke `
  --yes
python -m videoscope privacy tests/fixtures/generated/privacy_manual_visual.mp4 `
  --output privacy-smoke `
  --scan-only
python -m videoscope rescue tests/fixtures/generated/rescue_dark_noise.mp4 `
  --output rescue-smoke `
  --strategy balanced
```

The base-wheel smoke installs no AI, OCR, or Web extra and performs no model
download. Optional extras require separate, explicitly authorized release checks.

Local audit evidence on 2026-08-06:

- unified validation: Ruff and format passed, mypy passed 300 files, pytest
  passed 1,414 tests with 17 explicit optional/environment skips;
- required real fixtures: 36 passed in 226.32 seconds;
- supplemental native media gates: 208 passed, one platform-capability skip;
- frontend: 113 tests passed and the production build passed;
- standard isolated build, both distribution audits, and the clean-wheel Publish
  Check / Publish Ready / Safe Sharing / Conservative Rescue / Balanced Rescue /
  Faithful Clean / Chaptered Full / Selected Clips smoke passed; a separate clean
  `[web]` environment passed the loopback API health gate.

These local results do not close the exact-commit CI or human gates below.

## Manual local Web acceptance

- [ ] Long Video to Useful Content: run authorized meeting, tutorial, lecture,
  interview and screen recording inputs through all three goals; inspect every
  proposed removal, lock, join preview, chapter, clip and source-map entry.
- [ ] Long Video to Useful Content: listen to joins, verify subtitle timing and
  test downloads in current Firefox/Chromium and intended local players.
- [ ] Long Video to Useful Content: profile 30-minute, one-hour and multi-hour
  CPU, peak memory and temporary disk behavior; keep unrun platforms explicit.

- [ ] Video Rescue: run representative authorized phone, camera,
  screen-recording, meeting, and abnormal-export inputs; compare source,
  faithful, and improved media in actual target players on Windows.
- [ ] Video Rescue: record native Linux/macOS FFmpeg and player results, or keep
  those platforms explicitly unverified.
- [ ] Video Rescue: verify English/Simplified Chinese, invariant `what912`,
  keyboard-only focus, mobile layout, refresh/recovery, cancellation, explicit
  deletion, source hash preservation, and no browser console errors.

- [ ] On loopback, upload `publish_av.mp4` and inspect each Profile plan and
  preview before confirming once.
- [ ] Observe ordered SSE stages, download `publish-ready.mp4`, and play it in
  current Firefox and Chromium builds.
- [ ] Verify cancellation both before confirmation and during processing.
- [ ] Verify refresh recovery, keyboard focus, mobile layout, and an injected
  `needs_review` presentation.
- [ ] Exercise a representative VFR input and an authorized long video on each
  supported operating system, recording CPU, memory, temporary-disk, timing,
  and target-player observations.
- [ ] Safe Sharing: upload `privacy_manual_visual.mp4`, choose each audience at
  least once, inspect source playback and the risk timeline, add/resize a visual
  region, add an audio interval, review the private preview, and confirm the
  exact displayed digest.
- [ ] Safe Sharing: observe progress and terminal artifact download, refresh at
  review/confirmation/result stages, cancel active work, start a new task without
  deleting the old one, then explicitly delete retained local data.
- [ ] Switch English and Simplified Chinese, confirm literal `what912` is
  unchanged, navigate controls by keyboard, inspect responsive mobile layout,
  and record any console error.

## Human release gate

- [ ] Review [release-audit.md](../release-audit.md).
- [ ] Confirm no unpublished private media is present in the Git index.
- [ ] Review the complete staged diff.
- [ ] Confirm CI passes on Windows and Linux, Python 3.11 and 3.12.
- [ ] Confirm the candidate on a clean macOS environment if macOS is advertised.
- [ ] Enable GitHub private vulnerability reporting.
- [ ] Create the commit and local candidate tag only after all blockers close.

Suggested commands, intentionally not executed by the audit:

```powershell
git add .
git commit -m "release: prepare Long Video to Useful Content CPU MVP"
git tag v0.6.0-dev.0
```

Publishing commands belong to a later, separately authorized release operation.
