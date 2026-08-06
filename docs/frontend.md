# Local dashboard

The `web/` directory contains the React, strict TypeScript, and Vite dashboard.
It is an API client only: probing, sampling, scene segmentation, detectors,
evidence selection, and report construction remain in the Python
`AnalysisPipeline`.

## Install and verify

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

## Development

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

## Independent mock mode

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

## Interface behavior

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

## Screenshots

- `docs/assets/dashboard-home.jpg`: upload and detector selection.
- `docs/assets/dashboard-report.jpg`: selected Finding, timeline, evidence,
  and detector execution status.

Both screenshots use the explicitly labelled mock report and must not be
described as measured accuracy or a real Benchmark result.
