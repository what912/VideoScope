# Public site release checklist

Recorded: 2026-07-31

Candidate: `https://what912.github.io/VideoScope/`

Status: **NOT DEPLOYED**

This checklist separates observed local evidence from unverified external
conditions. A checked item means the command or inspection was actually
performed in this worktree. It does not imply that the candidate URL is live.

## Passed locally

- [x] GitHub Pages base is `/VideoScope/`.
- [x] The workflow targets Node 22 and runs lint, strict TypeScript, Vitest,
  build, media/runtime URL audit, and artifact upload before deployment.
- [x] The 404 route restoration is same-origin/base constrained and covered by
  automated tests.
- [x] The production CSP blocks remote scripts, inline application scripts,
  generic `unsafe-eval`, frames, and plugins. It permits only
  `wasm-unsafe-eval` for the checked-in browser hashing dependency; the 404
  recovery script is pinned by SHA-256.
- [x] Anonymous operation has no dependency on configured authentication.
- [x] Authentication has an unavailable adapter for missing configuration.
- [x] Sharing is separately disabled unless explicitly enabled.
- [x] Share sanitization excludes the original video, evidence image files,
  source filename, input hash, and path/object-URL runtime keys. Prompt is
  opt-in; sanitized detector parameters, execution data, and runtime summaries
  remain visible in the final consent disclosure.
- [x] Real report routes do not substitute marketing demo data when a report is
  missing, invalid, revoked, or expired.
- [x] English and Simplified Chinese dictionaries have parity; `what912` is a
  literal brand mark.
- [x] Browser detector thresholds are typed configuration values.
- [x] Detector failures are rendered separately from “no Findings.”
- [x] Public decorative media is local, role-unique, procedurally generated,
  and recorded in the checked-in manifest and provenance record.

Command results are recorded below after the final run rather than inferred
from prior tasks.

## Automated validation record

| Check | Result |
| --- | --- |
| `npm ci` | NOT RUN in Task 7; the existing local dependency tree was used for the focused provenance scan |
| `npm run media:prepare` | PASS in Task 6; generated 15 project-authored media outputs with no remote input |
| `npm run check` | PASS in Task 6; 52 Vitest files / 472 tests passed, and the production build and generated-media verification passed |
| `npm run media:review` | PASS in Task 8; generated the ignored 3x3 `runs/media-review/contact-sheet.webp` from all nine manifest posters using the local FFmpeg executable |
| `ffprobe -v error -show_entries stream=codec_name,width,height -of json ../runs/media-review/contact-sheet.webp` | PASS in Task 8; reported a `webp` stream at 1920x1080 |
| `python scripts/validate.py` | BLOCKED in Task 7; Ruff, mypy, and pytest are unavailable in this environment |

The media generator uses no remote input. Generated MP4/WebP binaries are
ignored by Git, and GitHub Actions regenerates them before the production
build. FFmpeg is a build prerequisite. No validation command is allowed to
download model weights or call an AI provider. Technical validation and visual
media review are independent release-blocking gates.

## Original-media visual review

- [x] The automated contact sheet contains each of the nine manifest posters
  once, in deterministic manifest order, with a 3x3 640x360-tile layout.
- [x] The local FFmpeg/ffprobe review artifact is a 1920x1080 WebP and remains
  under ignored `runs/media-review/`.
- [x] Normal-motion browser inspection at loopback `/VideoScope/` passed for
  Home, Workspace, Compare, and Report at desktop and mobile widths. Distinct
  role identity, overlay legibility, crop safety, restrained motion, local
  poster fallback, and absence of people, third-party brands, stale
  stock-subject labels, and private data were inspected.
- [x] Final route-level media acceptance passed the root-controller mobile
  overflow and forced reduced-motion measurements after fix round 2.

The contact sheet is an automated media-composition check. It does not replace
the route-level browser acceptance gate. The completed browser review recorded:

- Home screenshots at 1440x1000 and 390x844 under ignored
  `runs/media-review/`; the hero heading computed to
  `rgb(241, 243, 238)` and remained legible over the media.
- The initial Home mobile measurement recorded `innerWidth` 390 and document
  `scrollWidth` 375. The initial Workspace measurement recorded
  `scrollWidth` 379 at `innerWidth` 390. Because browser chrome and scrollbars
  can make `innerWidth` wider than the layout viewport, these comparisons do
  not close the gate; the corrected check uses `documentElement.clientWidth`.
- The corrected Workspace measurement after fix round 1 reported
  `documentElement.clientWidth` 375 and document/body `scrollWidth` 379. DOM
  isolation identified the `00:06.0` final timeline ruler label extending to
  378.68px. After fix round 2, the final Workspace measurement recorded
  `documentElement.clientWidth`, document `scrollWidth`, and body `scrollWidth`
  all at 375px. The Workspace toolbar remained internally scrollable at
  `clientWidth` 341 and `scrollWidth` 566 without widening the page. The
  ignored `runs/media-review/workspace-mobile-final.png` screenshot showed no
  page-level bottom scrollbar.
- The hero video reported `readyState` 4, `loop=true`, and `paused=false`; it
  advanced from 1.85s to 3.08s and, after a boundary wait, wrapped to 2.33s.
  Its poster is a local WebP.
- Compare and Report passed desktop/mobile visual inspection. The contact
  sheet showed nine distinct procedural roles with no people, third-party
  brands, or private data, and the updated DOM contained no stale
  stock-subject labels.

The poster branch is covered by existing `ViewportVideo` tests and a real
Chrome headless run with `--force-prefers-reduced-motion`. NetLog recorded a
request for `hero-optical.webp`, no request for `hero-optical.mp4`, and no MP4
request anywhere; the ignored `runs/media-review/home-reduced-motion.png` and
offline variant visually showed the poster. Chrome attempted its standard
built-in Google service URLs despite background-network flags, but application
asset requests remained on localhost and no project code requested a remote
asset.

The first sdist build showed that the intentionally untracked
`docs/CODEX-HANDOFF-2026-07-30.md` was collected by the documentation include.
The file was not modified or staged. `MANIFEST.in` now explicitly excludes
`docs/CODEX-HANDOFF-*.md`; the subsequent no-isolation build and archive audit
confirmed that the handoff document is absent from both release artifacts.

## Browser acceptance

- [x] Homepage inspected at the local production base.
- [x] Workspace inspected with a real generated fixture result.
- [x] Compare route inspected with real local inputs.
- [x] Report route inspected from a real local browser report.
- [x] Local production routes rendered without horizontal overflow at 1440,
  900, and 390 CSS-pixel widths.
- [ ] Keyboard-only navigation and dialog focus return are verified.
- [x] Reduced-motion behavior is verified with the OS/browser preference.
- [x] English/Simplified Chinese switching rendered correctly and `what912`
  remained literal.
- [x] Language persistence after repeated reloads is verified.
- [x] Bundled-sample smoke test completed in the in-app browser: a 6-second
  1280×720 local asset produced a new `browser-*` report, all four CPU detector
  executions completed, and the Workspace showed zero intervals.
- [x] Cancellation smoke test completed: Research mode was cancelled, displayed
  “Analysis cancelled,” restored “Start analysis,” and did not navigate away.
- [ ] Local-file analysis network panel shows no upload.
- [ ] JSON export contains no absolute path, original media, or demo content.
- [ ] Firefox acceptance completed.
- [ ] Chromium acceptance completed.
- [ ] External axe/equivalent scan, contrast check, screen-reader pass completed.

An in-app browser run rendered the home, Workspace, Compare, Report, Auth,
Privacy, Docs, and 404 routes at the three widths above. Real local analyses
were used for Workspace, Compare, and Report, and the page console showed no
warnings or errors after the final detector run. This is an in-app-browser
acceptance record, not a separate Firefox or Chromium certification.

## Fixture acceptance

The browser acceptance used H.264 transcodes of the deterministic FFmpeg
fixtures because their canonical MPEG-4 Part 2 encoding is not universally
browser-decodable. Resolution, frame rate, duration, visual content, and
manifest labels were retained; `tests/fixtures/manifest.json` was not changed.

| Fixture | Browser observation |
| --- | --- |
| `clean_motion.mp4` | No Findings from the four CPU detectors |
| `black_segment.mp4` | `near_black` matched `2.0–3.5s`; freeze/blur also flagged the intentionally static dark interval and remain documented heuristic overlap |
| `freeze_segment.mp4` | `possible_freeze` matched `2.0–4.0s` |
| `blur_segment.mp4` | `scene_relative_blur` matched `2.0–4.0s` |
| `flicker_segment.mp4` | `global_flicker` matched `2.0–4.0s` after scene-boundary and residual-edge regressions were fixed |
| `scene_cut.mp4` | No long flicker Finding and no freeze interval crossing a scene cut; flat single-color scenes can still produce scene-local freeze/blur heuristics |

These synthetic results are engineering regression evidence only. They are not
real-video detector accuracy, precision, or recall claims.

## Screenshot record

- [x] `docs/assets/public-home.png` — captured from the local production preview
  at `/VideoScope/`; it proves homepage rendering only
- [x] `docs/assets/public-workspace.png` — real local flicker analysis
- [x] `docs/assets/public-compare.png` — real clean-versus-flicker comparison
- [x] `docs/assets/public-report.png` — report persisted from a real local
  browser analysis

Each screenshot must come from the actual local production build. Workspace,
Compare, and Report screenshots must use a real fixture analysis, not the
marketing demo. Missing screenshots remain an explicit release gate.

## Supabase production gates

- [ ] A private account-deletion request channel is selected and published.
- [ ] Account ownership verification and privileged deletion are implemented
  without exposing a service-role credential to the static client.
- [ ] Account deletion is tested with two disposable accounts, including
  cross-account rejection, session invalidation, and completion confirmation.
- [ ] Supabase project created.
- [ ] Checked-in migration applied.
- [ ] GitHub Pages callback/redirect URLs allowlisted.
- [ ] Email magic link configured and tested.
- [ ] GitHub OAuth configured and tested.
- [ ] Required public build variables configured.
- [ ] Row-level policies tested using two separate accounts.
- [ ] Share creation, expiry, revocation, and revoked-link view tested.

Until these are complete, sign-in and sharing must remain visibly unavailable.
Anonymous local analysis can still be published.

## Publication gates

- [ ] Public repository `what912/VideoScope` exists and contains the reviewed
  commit.
- [x] User explicitly authorized publication to a public `what912/VideoScope`
  repository and GitHub Pages.
- [ ] GitHub Pages is enabled for the Actions deployment source.
- [ ] Pages workflow completes successfully.
- [ ] Signed-out production checks pass for:
  - `https://what912.github.io/VideoScope/`
  - `https://what912.github.io/VideoScope/workspace`
  - `https://what912.github.io/VideoScope/compare`
  - `https://what912.github.io/VideoScope/report/demo`
- [ ] Production privacy/network inspection is recorded.

No `git push`, Pages deployment, GitHub Release, PyPI upload, or Supabase
production mutation has yet been performed. GitHub publication still requires
an authenticated GitHub CLI session and successful Pages workflow; Supabase
auth/share must remain disabled until every production gate above is complete.
