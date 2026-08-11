# GenVideoScope 0.8.0 stable release audit

Audit date: 2026-08-11

Candidate: `genvideoscope 0.8.0`

Public integration: Draft PR [#25](https://github.com/what912/VideoScope/pull/25)
is based on the exact public `main` tree and its candidate commit passed the
required GitHub Actions verification.

Scope: the local-first CPU product line—Check, A Publish Ready, D Safe Sharing,
B Video Rescue, C Long Video to Useful Content—and the optional, review-first
Advanced AI assistance layer. This is engineering evidence, not a security
certification or a real-world semantic-accuracy/usefulness claim.

## Passed automated checks

### Repository and native media

- Exact-commit GitHub Actions run
  [31464199254](https://github.com/what912/VideoScope/actions/runs/31464199254)
  passed Ubuntu and Windows on Python 3.11/3.12, public-site generation and
  audit, native A/B/C/D workflows, repository verification, distribution
  audit, and a clean wheel install/smoke. Windows installer run
  [31464199147](https://github.com/what912/VideoScope/actions/runs/31464199147)
  passed audited build, clean install, loopback startup, smoke analysis,
  shutdown, uninstall, and artifact upload.
- `python scripts/validate.py` passed Ruff and formatting for 382 files and
  strict mypy for 316 source files. With local FFmpeg 9.0, the base pytest
  process passed 1,434 tests with 17 explicit optional/environment skips, then
  a fresh process passed all 21 native Rescue fixture tests.
- `python scripts/generate_test_videos.py --force` generated and decoded 29
  local synthetic videos twice with FFmpeg 9.0. File hashes matched between
  runs. No media was downloaded and generated videos are excluded from the
  distributions.
- C native gates proved exact reviewed removal with locked-keep precedence,
  complete-timeline chapters and Unicode subtitles, exact selected clips,
  explicit reorder acknowledgement, playable output, source immutability,
  complete source maps, stream/duration checks, and no new required
  black/freeze/audio/A-V or sustained join regression.
- The native Fake-AI gate ran a generated meeting fixture twice, obtained
  identical suggestion IDs and batch digest, accepted only one exact reviewed
  highlight, and passed C selected-clips rendering and source-map verification.
- Advanced-AI evaluation tests report chapter/highlight temporal IoU, event
  precision, recall, F1, coverage and boundary errors separately. They do not
  produce an uncalibrated aggregate quality score.

### Frontend and local API

- `npm test` passed 116 tests across 19 files.
- TypeScript checks and `npm run build` passed and the packaged static dashboard
  was synchronized.
- English and Simplified Chinese Advanced AI and C review flows, stable
  Check/A/B/C/D navigation, keyboard controls, lifecycle recovery,
  cancellation, deletion and literal `what912` attribution have automated
  contract coverage.
- The Advanced AI API is loopback-only. It has a bounded semaphore, rejects
  stale C revisions and changed inputs, and keeps transcript/evidence/review
  artifacts outside public artifact routes.
- The GitHub Pages application passed lint, TypeScript, 530 tests across 58
  files, a production build, and exact allowlist verification for 15
  deterministically generated project-authored media files.
- React Router was migrated to the patched 8.3.0 package and Vite to 8.2.0;
  `npm audit --audit-level=high` reports zero known vulnerabilities for the
  public-site dependency tree at the audit time.

### Build and distribution

- `python -m build --no-isolation` built
  `genvideoscope-0.8.0-py3-none-any.whl` and the matching sdist. The
  standard isolated command could not bootstrap `setuptools` in the restricted
  local environment; the build still rebuilt the wheel from the produced sdist.
- `scripts/audit_distribution.py` passed both archives and required the
  Advanced AI runtime, providers, evaluation code and documentation.
- The audit rejects videos, runs, caches, logs, generated fixtures, private
  transcript/evidence/preview trees, pending/staging trees, public job outputs,
  workspaces and personal absolute paths.
- The base distribution declares no AI, ASR, OCR or Web dependency. Faster
  Whisper remains in the separate `asr` extra; Ollama uses only a user-started
  loopback endpoint and never pulls a model implicitly.
- The exact base wheel passed an isolated offline smoke covering `--version`,
  `doctor`, CPU Check, A Publish Ready, manual-region D Safe Sharing,
  Conservative and Balanced B Video Rescue, and all three confirmed C goals.
  The 2026-08-11 local run was explicitly offline (`--no-index --no-deps`) and
  reused already-verified base dependencies; exact clean dependency resolution
  remains a GitHub Actions gate.
- The frozen Windows connector audit passed and the 0.8.0 installer was built
  without FFmpeg, model weights, credentials or development-only packages.
  Paid code signing is explicitly deferred; the release must retain the
  bilingual unknown-publisher warning and SHA-256 asset.
- Final archive checksums must be generated after this tracked document is
  frozen and stored outside the sdist because an embedded sdist hash would be
  self-referential.

## Security and privacy audit

- Production scans found no `shell=True`, `os.system`, stored credential,
  common API key/token pattern or configured remote media-processing call.
- External media commands use argument arrays, bounded diagnostics and checked
  return codes. FFmpeg remains an external executable and is not copied into
  source or archives.
- Source files remain unchanged. Exact accepted action IDs, input/transcript
  hashes, effective configuration, suggestion/plan digests, preview identity
  and reorder acknowledgement are bound before media changes are allowed.
- Suggestions start rejected. Text summaries and titles cannot edit media;
  accepted chapter/highlight ranges still pass through C's ordinary private
  preview, exact confirmation, native rendering and verification path.
- Public JSON/HTML reject private absolute paths and unsafe artifact paths.
  AI cancellation, deletion, provider failure and stale state fail closed.
- Web uploads are bounded and ffprobe-validated. Artifact paths are normalized
  and restricted. The service binds loopback by default with no wildcard CORS.
- The public origin is exact, requires an expiring pairing session, and cannot
  write provider secrets. BYOK keys are accepted only by the loopback UI,
  excluded from OpenAPI/responses, held in process memory, and cleared at
  shutdown. Remote use requires per-run data-transfer consent.
- The default public-site account is an encrypted device account. PBKDF2-derived
  AES-GCM protects the local profile; the passphrase and derived key are not
  persisted. The product clearly states that there is no server recovery or
  automatic cloud synchronization.

## Risks and known limitations

- CPU signals and AI suggestions are heuristic. Silence, low visual change,
  transcript wording or model output do not prove that content is unimportant.
- Optional Advanced AI can transcribe locally and propose grounded chapters,
  highlights, summaries and titles. It does not establish truth, guarantee a
  useful edit, auto-accept media changes or provide a quality/virality score.
- Fake providers prove contracts, determinism, caching and failure isolation;
  they do not measure real Faster Whisper or Ollama semantic quality.
- FFmpeg build, codec, VFR, damaged-media and player differences remain
  operational risks. Reports, filenames, hashes, transcripts, prompts,
  thumbnails, previews, video and audio can all contain sensitive information.
- An earlier `main` Windows/Python 3.12 run and the second Draft PR
  Windows/Python 3.11 run each reported one Rescue status mismatch on the
  intentionally damaged middle-range fixture inside the long-lived monolithic
  pytest process. In the same jobs, the dedicated native Rescue step passed;
  isolated local runs passed as well. The final candidate therefore keeps every
  test and every strict threshold but runs the 21 native Rescue fixture tests in
  a fresh pytest process, with structured non-passing-check diagnostics. The
  exact final commit passed both Windows versions and both Ubuntu versions.
- The loopback workbench has no TLS, central billing, quota or multi-tenant
  isolation boundary and must not be exposed as a public server. The device
  account is local UI identity, not a server security boundary.

## Human inspection required

1. Use authorized meeting, tutorial, lecture, interview and screen-recording
   media; judge every proposal, transcript, join, subtitle and output.
2. Evaluate a locally cached Faster Whisper/Ollama pair and one explicitly
   funded compatible BYOK endpoint. Record model/version, exact data consent and
   provider charges; separate grounding errors from workflow defects.
3. Test English/Simplified Chinese, keyboard-only focus, reduced motion, mobile
   layout, refresh/recovery, cancellation, deletion and browser console output.
4. Profile CPU, peak memory and temporary disk on representative 30-minute,
   one-hour and multi-hour inputs.
5. Review transitive licenses/vulnerabilities, PyPI ownership, the exact Git
   index, exact-commit CI and external archive checksums before a stable tag.

## Externally unverified

- Native macOS media and player behavior remains unverified. CI verifies native
  FFmpeg workflows on Ubuntu and Windows, but not every codec or target player.
- No representative private user media, uncommon codec corpus, encrypted
  input, multi-hour run, current-browser manual session, real
  ASR/Ollama/BYOK/AI/OCR model, GPU or model download was used.
- No PyPI upload or production multi-user AI hosting boundary was performed.

## Release decision

The CPU product, local connector, device account and optional review-first AI
layer have green local and exact-commit CI engineering gates and are suitable
for the stable 0.8.0 release. Unrun representative private-media,
real-provider, macOS and long-duration checks remain disclosed limitations and
prohibit accuracy or universal-compatibility claims.

The static public website may run browser-side CPU diagnostics and link to local
installation. It must not imply that GitHub Pages runs Python, FFmpeg or private
AI models on a server.
