# VideoScope front ends

VideoScope has two separate React/TypeScript front ends with different trust
boundaries:

- `site/` is the static, anonymous-first GitHub Pages product site. It can
  decode a selected local video and run four bounded CPU heuristics directly
  in the browser.
- `web/` is the optional local FastAPI dashboard. It is an API client only:
  probing, FFmpeg sampling, scene segmentation, detectors, evidence selection,
  and report construction remain in the Python `AnalysisPipeline`.

Do not describe a browser result as a desktop result. The two workflows use
different decoders, sampling schedules, schemas, and feature implementations.

## Public browser site (`site/`)

### Install and verify

Node.js 22.13 or newer and FFmpeg are required. From the repository root, use
the complete release lifecycle:

```powershell
cd site
npm ci
npm run media:prepare
npm run check
npm run media:review
```

The media generator is procedural and uses no remote input. Its generated MP4
and WebP binaries are ignored by Git; GitHub Actions regenerates them before
building. `npm run check` performs the technical release gate, including lint,
strict TypeScript, Vitest, the production Vite build, generated-media
verification, the built-runtime URL audit, and measured bundle budgets.
`npm run media:review` is the separate visual release gate. Both technical and
visual review must pass before release. Base-path output is `/VideoScope/`.

Development and local production preview:

```text
npm run dev -- --host 127.0.0.1
npm run build
npm run preview -- --host 127.0.0.1
```

The public routes are `/`, `/workspace`, `/compare`, `/report/:reportId`,
`/auth`, `/privacy`, and `/docs`. Production routes are below the
`/VideoScope/` base. The checked-in `404.html` restores direct GitHub Pages
refreshes without accepting a cross-origin redirect.

### Browser analysis boundary

The browser service:

- decodes one selected file through the browser media stack;
- samples frames once and reuses bounded frame features;
- runs near-black, possible-freeze, scene-relative-blur, and global-flicker;
- persists reports and compact evidence thumbnails in IndexedDB;
- never stores the original video in IndexedDB;
- exposes detector failures separately from “no Findings.”

The browser service does not use FFmpeg/ffprobe, AI providers, OCR, Benchmark,
or the Python Web API. Supported containers listed by the upload form are only
an initial filter; actual decodability depends on the browser and the encoded
codec. A direct HTTPS URL is fetched only after a network/privacy confirmation
and is subject to the source host's CORS policy.

### Optional auth and sharing

Anonymous local analysis is always available. To make authentication available,
set both `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`. Sharing is a separate
gate and additionally requires:

```text
VITE_SUPABASE_SHARE_ENABLED=true
```

The production Supabase project, callback allowlist, OAuth provider, email
magic link, migration, and two-account row-level-policy verification are
external release gates. With variables absent, the unavailable adapters are
used and no sign-in/share request is attempted.

Sharing requires a final consent step and sends a sanitized report projection,
not the original video or evidence image files. Disabling the feature does not
revoke links previously published by a configured service.

### Language, identity, media, and accessibility

The site supports English and Simplified Chinese. The literal creator mark
`what912` is brand identity and is never translated. All product media is
procedurally generated under `site/public/media` from the checked-in manifest
and provenance records; no remote input or third-party media source is used.
Production code does not hotlink media files.

Automated DOM contracts, accessible names, severity text, reduced-motion
styles, focus states, responsive CSS, and build budgets are checked locally.
Keyboard-only, screen-reader, external axe/equivalent, contrast, Firefox, and
full viewport acceptance remain human browser release gates until actually
recorded.

## Local FastAPI dashboard (`web/`)

### Install and verify

```text
cd web
npm install
npm test
npm run build
```

`npm run build` type-checks the application, writes the normal Vite output to
`web/dist`, and copies that exact output into
`src/videoscope/web/static`. The Python package includes the copied assets so
`videoscope serve` can serve the production dashboard from `/`.

No fonts, scripts, analytics, UI components, or assets are loaded from a CDN.

### Development

Use two terminals:

```text
# terminal 1, repository root
videoscope serve --port 8765

# terminal 2
cd web
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` and `/openapi.json` to the
local backend. Production mode needs only:

```text
videoscope serve --port 8765
```

Then open `http://127.0.0.1:8765/`.

### Independent mock mode

`web/src/mocks/mock-report.json` is a clearly labelled interface fixture. It is
not a detector result, Benchmark result, or accuracy claim.

Open:

```text
http://127.0.0.1:5173/?mock=1
```

Mock mode lets front-end contributors choose a local file, review lifecycle
screens, interact with a Finding timeline, and take documentation screenshots
without FFmpeg or model weights. It never replaces the production API path.
The query parameter `job` demonstrates refresh restoration:

```text
http://127.0.0.1:5173/?mock=1&job=mock-dashboard
```

### Interface behavior

- Uploads use the typed API client in `web/src/api.ts`; components contain no
  hard-coded API host.
- SSE reconnects with the last received sequence in the `after` query.
- `job` remains in the URL and a refresh restores state from the API.
- Clicking a Finding, timeline interval, or evidence frame seeks the video.
- Current playback highlights any active visible Finding interval.
- Severity is represented by text, symbols, and color.
- A `detector_error` is shown separately and is never presented as an empty
  successful result.
- AI and OCR detectors expose their unavailable installation reason and cannot
  be selected while unavailable.

The default upload limit shown by the UI matches the default 1,024 MiB server
limit. Operators changing `--max-upload-mib` should also communicate that
policy to dashboard users.

### Screenshots

- `docs/assets/dashboard-home.jpg`: upload and detector selection.
- `docs/assets/dashboard-report.jpg`: selected Finding, timeline, evidence,
  and detector execution status.

Both screenshots use the explicitly labelled mock report and must not be
described as measured accuracy or a real Benchmark result.
