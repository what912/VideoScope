# GenVideoScope 0.7.0 development release audit

Audit date: 2026-08-07

Candidate: `genvideoscope 0.7.0.dev0`

Public integration: pull request `what912/VideoScope#18`, squash-merged to `main`

Scope: the local-first CPU product line—Check, A Publish Ready, D Safe Sharing,
B Video Rescue, C Long Video to Useful Content—and the optional, review-first
Advanced AI assistance layer. This is engineering evidence, not a security
certification or a real-world semantic-accuracy/usefulness claim.

## Passed automated checks

### Repository and native media

- `python scripts/validate.py` passed Ruff and formatting for 362 files, strict
  mypy for 300 source files, and pytest with 1,417 passed and 17 explicit
  optional/environment skips on Windows/Python 3.12.
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

- `npm test` passed 114 tests across 18 files.
- TypeScript checks and `npm run build` passed and the packaged static dashboard
  was synchronized.
- English and Simplified Chinese Advanced AI and C review flows, stable
  Check/A/B/C/D navigation, keyboard controls, lifecycle recovery,
  cancellation, deletion and literal `what912` attribution have automated
  contract coverage.
- The Advanced AI API is loopback-only. It has a bounded semaphore, rejects
  stale C revisions and changed inputs, and keeps transcript/evidence/review
  artifacts outside public artifact routes.
- The GitHub Pages application passed lint, TypeScript, 520 tests across 55
  files, a production build, and exact allowlist verification for 15
  deterministically generated project-authored media files.
- React Router was migrated to the patched 8.3.0 package and Vite to 8.2.0;
  `npm audit --audit-level=high` reports zero known vulnerabilities for the
  public-site dependency tree at the audit time.

### Build and distribution

- `python -m build --no-isolation` built
  `genvideoscope-0.7.0.dev0-py3-none-any.whl` and the matching sdist. The
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
- The exact base wheel passed a clean-environment smoke covering `--version`,
  `doctor`, CPU Check, A Publish Ready, manual-region D Safe Sharing,
  Conservative and Balanced B Video Rescue, and all three confirmed C goals.
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
- The first `main` Windows/Python 3.12 run reported one non-reproduced Rescue
  status mismatch on the intentionally damaged middle-range fixture. The same
  tree had already passed in the pull request, its exact-job rerun passed, and
  eight repeated local native runs passed. This candidate keeps the strict
  gate and makes future failure output list only the non-passing checks; the
  transient remains a monitored native-media risk rather than a waived test.
- The loopback workbench has no public account, TLS, billing, quota or
  multi-tenant isolation boundary and must not be exposed as a public server.

## Human inspection required

1. Use authorized meeting, tutorial, lecture, interview and screen-recording
   media; judge every proposal, transcript, join, subtitle and output.
2. Evaluate a locally cached Faster Whisper model and an explicitly selected
   Ollama model. Record model/version and separate grounding errors from product
   workflow defects.
3. Test English/Simplified Chinese, keyboard-only focus, reduced motion, mobile
   layout, refresh/recovery, cancellation, deletion and browser console output.
4. Profile CPU, peak memory and temporary disk on representative 30-minute,
   one-hour and multi-hour inputs.
5. Review transitive licenses/vulnerabilities, PyPI ownership, the exact Git
   index, exact-commit CI and external archive checksums before a stable tag.

## Externally unverified

- GitHub Actions passed on the public integration tree for Ubuntu and Windows,
  Python 3.11 and 3.12. Ubuntu reported 1,426 passed and 8 explicit skips;
  Windows reported 1,417 passed and 17 explicit skips. The independent public
  site and distribution jobs also passed.
- Native macOS media and player behavior remains unverified. CI verifies native
  FFmpeg workflows on Ubuntu and Windows, but not every codec or target player.
- No representative private user media, uncommon codec corpus, encrypted
  input, multi-hour run, current-browser manual session, real ASR/Ollama/AI/OCR
  model, GPU or model download was used.
- No PyPI upload or production multi-user AI hosting boundary was performed.

## Release decision

The CPU product and optional review-first Advanced AI layer have green local and
public CI baselines and are suitable for an openly labeled development
prerelease. A stable release claim remains premature until the human,
real-model, supply-chain and remaining platform gates above close.

The static public website may run browser-side CPU diagnostics and link to local
installation. It must not imply that GitHub Pages runs Python, FFmpeg or private
AI models on a server.
