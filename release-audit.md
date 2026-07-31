# GenVideoScope 0.2.0 release audit

Audit date: 2026-07-29  
Candidate: `genvideoscope 0.2.0rc1`  
Public identities: GitHub repository `GenVideoScope`, PyPI distribution
`genvideoscope`, Python package `videoscope`, CLI `videoscope`

This audit records checks performed against the release candidate. It is not a
security certification and it does not claim detector accuracy on real
generated videos.

## Passed

### Security and privacy

- Scans of first-party source, configuration, documentation, and examples found
  no committed API key, access token, personal email address, Windows user
  profile path, or local media file.
- External commands use argument arrays with `shell=False`; no production use
  of `shell=True` was found.
- Uploads are read in bounded chunks and rejected after the configured byte
  limit. Extension and MIME are advisory; the shared pipeline still validates
  the media with ffprobe.
- Job and artifact paths use generated identifiers, resolved-path containment,
  and traversal rejection.
- The local server defaults to loopback. Host headers and browser origins are
  restricted to loopback unless the operator explicitly enables network
  access. Wildcard CORS is not enabled.
- Jinja auto-escaping protects the offline report, report URLs are generated
  from controlled relative paths, and the React application does not inject
  untrusted HTML.
- API errors and FFmpeg diagnostics are sanitized before exposure. Reports do
  not contain workspace paths.
- Full input video is not bundled into a report unless the user explicitly
  requests it.
- Base tests have an enforced non-loopback socket guard.

### Dependencies and packaging

- The base dependency group contains CPU analysis libraries only. Torch,
  OpenCLIP, PaddleOCR, PaddlePaddle, FastAPI, Uvicorn, and multipart support are
  absent from a clean base-wheel installation.
- `ai`, `ocr`, and `web` are separate optional extras. `all` is opt-in. The
  development extra includes Web dependencies only so it can run the complete
  API test suite.
- A clean `wheel[ai]` installation registered providers without importing
  Torch/OpenCLIP during registration and without downloading model weights.
- Direct Python and dashboard dependencies and their declared licenses are
  inventoried in `docs/third-party-licenses.md`.
- Wheel and sdist content audits found no generated test video, `runs`
  directory, virtual environment, `node_modules`, cache, model weight, or
  personal absolute path.
- FFmpeg remains an external executable and is not redistributed by the
  project.

### Performance and lifecycle

- File hashing and uploads are streamed; the complete video is not loaded into
  memory.
- Frame sampling is a single pipeline stage reused by all detectors.
- The AI runtime lazy-loads shared providers and keys embedding caches by video
  hash, timestamp, model, and preprocessing version.
- Reports reference evidence thumbnails and do not embed the source video by
  default.
- Workspaces are removed after successful analysis unless explicitly retained;
  Web jobs have expiry cleanup and cancellation support.

### Stable interfaces

- Reports declare schema version `0.1`; the candidate tool version is
  `0.2.0rc1`.
- Analysis and detector configuration is Pydantic-validated.
- CLI exit codes remain `0` for completed analysis (including findings), `2`
  for input/configuration errors, `3` for unprocessable video, and `4` for
  internal analysis failure.
- Detectors use the shared protocol, requirements model, configuration model,
  execution record, and deterministic Finding structure.
- Detector failures are isolated and represented as failed executions rather
  than discarding the report.

### Documentation, examples, and verification

- README installation and CLI commands were exercised against the built wheel.
- Example PowerShell, shell, batch API, custom detector, and configuration files
  are present and validated by tests.
- Documentation distinguishes CPU and optional AI/OCR heuristics, states
  limitations, and does not present synthetic-fixture results as real-world
  accuracy.
- Clean base-wheel analysis of `black_segment.mp4` produced JSON and offline
  HTML without an absolute workspace path.
- Clean Web-extra smoke testing covered health, dashboard serving, detector
  listing, upload, job completion, report retrieval, and shutdown.
- The React dashboard completed all 10 tests and a production build.
- Repository verification completed lint, format, strict type checking, and
  tests. The final suite passed 228 tests; 3 real-model optional tests were
  skipped by design.
- Wheel and sdist build and distribution-content audit completed successfully.

## Risks

- Reports intentionally retain the input filename, SHA-256 hash, optional
  prompt, selected metadata, findings, and evidence thumbnails. A user must
  review these artifacts before sharing them.
- `videoscope serve --allow-network` broadens the trust boundary and provides
  no authentication or multi-user isolation. It is intended only for a trusted
  network with an operator-supplied protective layer.
- The local job executor has configurable worker counts and per-upload limits,
  but the queued-job count and aggregate retained storage are not globally
  quota-limited. Untrusted network exposure can cause resource exhaustion.
- The DINOv2 provider uses an explicitly authorized Torch Hub download path.
  That still carries upstream code and model supply-chain risk; pin and review
  the exact repository revision and weight terms before organizational use.
- Python version ranges allow transitive resolution to change over time.
  Distributors needing reproducible environments should publish reviewed
  constraints or lock files.
- Detector outputs are heuristic observations. Static scenes, intentional
  black frames, motion, occlusion, lighting, OCR errors, and model limitations
  can create false positives or false negatives.
- The tested temporary Windows FFmpeg 8.1.2 essentials build identifies itself
  as GPLv3. It was used only as an external local test tool and is not included
  in either distribution.

## Not yet verified

- The GitHub Actions Linux/Windows and Python 3.11/3.12 matrix has not run on
  the final public commit.
- Native Linux and macOS smoke tests were not run on this Windows workstation.
- Real OpenCLIP/DINOv2 weights, CUDA execution, PaddleOCR models, and multilingual
  OCR behavior were not exercised; offline fakes cover the contracts.
- Detector accuracy, memory use, and throughput on a representative,
  independently labelled real-video corpus have not been measured.
- All direct licenses were reviewed, but a final resolved transitive dependency
  license and vulnerability scan remains a distributor responsibility.
- Browser behavior should receive a final manual pass in Firefox, Chromium, and
  a keyboard-only workflow after the release artifact is fixed.

## Human actions required

1. Confirm GitHub `GenVideoScope` and PyPI `genvideoscope` availability and
   ownership.
2. Create the public repository URLs, security contact route, and project
   maintainer metadata; replace any remaining repository-URL placeholders.
3. Review exact resolved transitive licenses, known vulnerabilities, model
   cards, and weight terms for every installation profile being advertised.
4. Review report screenshots and sample artifacts for private filenames,
   prompts, metadata, and evidence.
5. Run the final CI matrix on the exact commit, inspect wheel/sdist artifacts,
   and sign or attest them according to the maintainer's release policy.
6. Only after all gates pass, create the commit/tag and perform PyPI/GitHub
   publication manually.

## Release blockers

There is no known failing local code or build check. Public release remains
blocked until all of these manual gates pass:

- the exact final commit passes the GitHub Actions matrix;
- package/repository name ownership and canonical URLs are confirmed;
- the resolved transitive dependency and optional model-license review is
  accepted;
- a maintainer completes the privacy and browser review.

No commit, Git tag, push, PyPI upload, or GitHub Release was performed during
this audit.
