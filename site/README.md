# VideoScope public browser site

This directory contains the public VideoScope product site: a static React,
TypeScript, and Vite application deployed at
`https://what912.github.io/VideoScope/`.

Anonymous visitors can analyze a local video in the browser with four bounded
CPU heuristics, inspect Findings on an interactive timeline, compare reports,
and export JSON or use the print layout. The Python/FFmpeg desktop package is a
separate implementation with a wider codec surface and benchmark support.

## Requirements

- Node.js 22.13 or newer;
- npm (the checked-in lockfile is used with `npm ci`);
- FFmpeg and ffprobe on `PATH` when preparing or verifying project-authored
  decorative media.

No model download or AI provider is needed for the public site.

## Development

From the repository root:

```powershell
cd site
npm ci
npm run media:prepare
npm run dev -- --host 127.0.0.1
```

Open the Vite URL with the `/VideoScope/` base path. The primary commands are:

```text
npm run lint          ESLint with zero warnings allowed
npm run typecheck     strict TypeScript check
npm test              Vitest suite
npm run media:prepare generate the local MP4/WebP media set with FFmpeg
npm run build         production Vite build
npm run media:verify  validate generated media and production references
npm run check         run all release-oriented checks in order
```

Generated media binaries are ignored by Git. Their deterministic source
descriptions and provenance are checked in at
`public/media/media-sources.json` and `public/media/PROVENANCE.md`.

## Architecture

- `src/app` contains route composition and shared application providers.
- `src/components` contains reusable product, video, timeline, report, and
  status components.
- `src/features` contains browser analysis, authentication, comparison,
  reporting, sharing, and workspace behavior.
- `src/data` and `src/types` centralize mock/demo data and typed contracts.
- `src/services` keeps browser persistence and service boundaries outside UI
  components.
- `scripts` contains media generation, safety audits, and release regression
  tests.

The Vite production base is `/VideoScope/`. GitHub Pages has no native SPA
rewrite, so `public/404.html` only restores same-origin routes beneath that
base. A raw deep URL can initially return HTTP 404 before client-side recovery;
the page then restores the requested application route.

## Privacy and local files

Local-file analysis uses browser decoding, Canvas sampling, and IndexedDB. The
original local video is not uploaded or persisted, and the application does
not store an absolute local path. Saved reports may contain metadata,
configuration, Findings, review state, and compact evidence thumbnails.

Direct URL import has a different boundary: after explicit consent, the
browser contacts the exact user-entered HTTPS host. That host can observe the
request, and CORS can block it. VideoScope does not proxy URL imports.

The production build does not load remote fonts, analytics, CDN scripts, or
hotlinked decorative media.

## Optional Supabase features

Authentication and sanitized sharing use an optional Supabase adapter. Copy
`.env.example` to a local environment file only when testing a configured
project:

```text
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
VITE_SUPABASE_SHARE_ENABLED=false
```

When the URL/key are absent, sign-in is visibly unavailable and anonymous
local analysis still works without Supabase requests. Sharing also remains
disabled unless `VITE_SUPABASE_SHARE_ENABLED=true`. Configuring environment
variables alone is not proof that authentication or sharing is production
ready; apply the checked-in migration and complete the security gates in
`../docs/public-site-release-checklist.md` first. Never expose a Supabase
service-role key to this static client.

## Deployment

`.github/workflows/pages.yml` prepares project-authored media, runs lint,
typecheck, tests, build and audits, then publishes `dist` through GitHub Pages.
It does not publish PyPI, create a GitHub Release, download AI models, or enable
the optional Supabase features.

Release evidence, known limitations, and remaining manual checks are recorded
in `../docs/public-site.md` and
`../docs/public-site-release-checklist.md`.
