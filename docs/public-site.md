# VideoScope public browser site

Status as of 2026-08-01: **live on GitHub Pages over HTTPS**.

- Public URL: `https://what912.github.io/VideoScope/`
- Public source repository: `https://github.com/what912/VideoScope`
- Production base path: `/VideoScope/`
- Creator mark: `what912` (literal in every language)

The initial public deployment baseline was the reviewed root commit
`651b0643191a12f4474ae62db5d3df7d1e5e65fa` (tree
`551a532cf7b423bbffdbe09c1010dc0e6e992b2e`, zero parents, 438 paths). CI run
`30665501160` and Pages run `30665501278` both completed successfully. The
signed-out production checks are recorded in
`public-site-release-checklist.md`.

The production site and local production build have both been exercised. The
production bundled sample completed all four CPU detectors and produced a
recoverable browser report. Deterministic H.264 fixture transcodes were also
used locally for detector interval regression checks; detailed observations,
known heuristic overlap, screenshots, and remaining manual checks are recorded
in `public-site-release-checklist.md`.

## What visitors can use anonymously

A visitor can choose a local video and run a bounded CPU preview without an
account. The browser decodes and samples the file locally, then runs:

1. `near_black` — sustained near-black samples;
2. `possible_freeze` — sustained near-repeated samples;
3. `scene_relative_blur` — sharpness drops relative to a scene baseline;
4. `global_flicker` — potential high-frequency global luminance changes with
   scene-boundary guards.

Findings contain intervals, detector-specific severity/score/confidence,
observable descriptions, limitations, parameters, and compact evidence. A
detector failure is displayed as an error and is never rewritten as “no issue.”
There is no overall or universal quality score.

The Workspace links video time, Finding selection, timeline, diagnostic
overlay, evidence, detail, and metric cursor. Compare accepts two local videos
or compatible browser reports and compares detector results side by side.
Report offers Creator and Research views, UTF-8 JSON download, and a browser
print layout labelled “Print / Save as PDF.” VideoScope does not include a PDF
file generator.

## Browser and desktop are different products

| Capability | Public browser | Desktop package |
| --- | --- | --- |
| Local file analysis | Browser decoder and Canvas | FFmpeg/ffprobe |
| Four CPU detectors | Bounded browser implementations | Python implementations |
| JSON/report view | Local browser schema and print view | Versioned JSON + offline HTML |
| Benchmark | No | Yes |
| AI/OpenCLIP/DINOv2 | No | Optional local providers |
| OCR/PaddleOCR | No | Optional local provider |
| Local Web API | No | Optional FastAPI service |
| Full codec reach | Browser-dependent | FFmpeg-build-dependent |

The browser may reject a container/codec combination that the desktop package
can process. It does not inspect every encoded frame, and short events between
samples may be missed. Results are heuristic observations, not proofs of
creative intent, model quality, or measured detector accuracy.

## Privacy and network boundaries

For a local file, anonymous analysis does not upload the original video.
Session object URLs are released when the file is replaced, cancelled, or
cleared. Saved IndexedDB data can include video metadata, configuration,
Findings, review state, and compact evidence thumbnails, but not the original
video or an absolute local path.

Direct URL import is different. After an explicit confirmation, the browser
contacts the exact HTTPS host entered by the visitor. That host can observe the
visitor's IP address and request metadata, and its CORS policy can block the
request. VideoScope does not proxy the media.

Authentication and sanitized sharing are optional Supabase-backed features.
With the environment variables absent, their unavailable adapters prevent
network calls while anonymous analysis remains available. Sharing is separately
disabled unless `VITE_SUPABASE_SHARE_ENABLED=true`; a final consent step shows
the sanitized fields that would leave the device. The original video and
evidence image files are excluded.

## Bilingual interface

The navigation, upload flow, processing stages, errors, Workspace, Compare,
Report, Privacy, Docs, authentication, and sharing surfaces have English and
Simplified Chinese dictionaries. The selected language persists in the current
browser. `VideoScope` and `what912` remain brand literals and are not
translated.

## Local development

Node.js 22.22 or newer:

```powershell
cd site
npm ci
npm run media:prepare
npm run check
npm run media:review
npm run dev -- --host 127.0.0.1
```

For a production-equivalent local build:

```powershell
cd site
npm run build
npm run preview -- --host 127.0.0.1
```

Open the URL reported by Vite and include the `/VideoScope/` base path.

## Project-authored media lifecycle

The public site does not use remote fonts, analytics, CDN scripts, or hotlinked
decorative media. Nine distinct media roles are represented by procedurally
generated local MP4/WebP assets. The generator uses no remote input; its
manifest and provenance record are checked in, while generated binaries are
ignored by Git. FFmpeg is a build prerequisite. GitHub Actions regenerates the
media before building the site.

- `site/public/media/media-sources.json`
- `site/public/media/PROVENANCE.md`

Run `npm run media:prepare` before `npm run check`. The technical checks block
release if generated media, runtime URL policy, or measured bundle budgets are
invalid. `npm run media:review` is an additional visual release gate; technical
and visual review must both pass before release.

## Optional Supabase configuration

Copy `site/.env.example` to a local environment file and set:

```text
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
VITE_SUPABASE_SHARE_ENABLED=false
```

Before production auth or sharing can be described as working, an operator
must create the Supabase project, apply the checked-in migration, configure
GitHub Pages callback URLs, enable email magic link and GitHub OAuth as desired,
provide GitHub Actions variables, verify row-level policies with two separate
accounts, establish and test the private account-deletion process described in
`supabase/README.md`, and verify share revocation/expiry. The Pages workflow
maps only the public URL, anonymous key, and sharing flag into the Vite build;
it never accepts a service-role key. None of those external steps were
performed by the local implementation task, so those variables must remain
unset for the anonymous-only production deployment.

## GitHub Pages

`.github/workflows/pages.yml` builds the static site with Node 22 on pushes to
`main` or manual dispatch. It runs lint, strict TypeScript, Vitest, production
build, media/runtime audits, and then uploads `site/dist`. It does not publish
PyPI, create a GitHub Release, run AI providers, or download model weights.

The public `what912/VideoScope` repository contains the reviewed release source,
GitHub Pages uses the Actions deployment source, HTTPS is enforced, and the
deployment workflow completed successfully. Raw deep-route requests such as
`/VideoScope/workspace` currently receive GitHub Pages HTTP 404 before the
checked-in `404.html` performs a same-origin client-side route restoration.
The application route then loads and reload recovery was verified, but clients
that reject the initial 404 status may not follow that recovery.

## Known limitations

- Browser codec support varies by browser, operating system, and encoded media.
- Direct URL import requires HTTPS and permission from the source CORS policy.
- Sampling can miss brief anomalies between selected frames.
- Thresholds are engineering defaults, not calibration against a representative
  independent real-video test set.
- Dark scenes, fades, static shots, low texture, intentional soft focus, camera
  motion, lighting effects, and scene segmentation errors can cause false
  positives or false negatives.
- Firefox acceptance, external accessibility tooling, keyboard/screen-reader
  passes, and a dedicated production network-panel proof remain manual gates.
- Optional Supabase auth/share is not production-configured in this repository.

See `public-site-release-checklist.md` for measured local results and remaining
external gates.
