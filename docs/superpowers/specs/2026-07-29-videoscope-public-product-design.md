# VideoScope Public Product Website and Browser Workbench Design

Status: proposed after visual-direction approval  
Date: 2026-07-29  
Target public repository: `what912/VideoScope`  
Target public URL: `https://what912.github.io/VideoScope/`

## 1. Outcome

Build a bilingual public website and browser-local VideoScope workbench that:

- explains within the first viewport that VideoScope locates observable video
  quality changes by time interval and shows evidence frames;
- lets anonymous visitors analyze a supported local video without uploading the
  original file to VideoScope;
- provides interactive workspace, comparison, and report experiences;
- keeps optional sign-in separate from the core local analysis flow;
- preserves the CPU-first, local-first, evidence-first product contract;
- publishes as a static application on GitHub Pages under the `what912`
  account;
- keeps the existing Python CLI, FastAPI service, and local dashboard as the
  more complete desktop/professional path.

The approved visual direction is **Video Observatory**: a restrained
cinematic interface built from real video surfaces, scan lines, frame grids,
time scales, evidence boxes, signal plots, and optical details. It absorbs
useful principles from motion-design, cinematic AI, screen-recording, video
review, and creator tools without copying any referenced brand.

## 2. Product truth and scope

### 2.1 Public launch capabilities

The public site may truthfully provide:

- local file selection and drag-and-drop;
- browser-native decoding of supported formats;
- fixed-rate local sampling with a documented sample cap;
- the four browser CPU heuristics:
  - `near_black`;
  - `possible_freeze`;
  - `scene_relative_blur`;
  - `global_flicker`;
- interactive time-range Findings and evidence frames;
- local JSON export;
- a browser-generated printable report view;
- detector-by-detector comparison of two locally selected videos;
- English and Simplified Chinese;
- optional registration and sign-in when Supabase is configured.

### 2.2 Truth-preserving restrictions

The public site must not:

- present an uncalibrated overall quality score;
- combine unrelated detector signals into a universal ranking;
- imply that browser heuristics are equivalent to the complete FFmpeg-backed
  desktop pipeline;
- present optional AI or OCR examples as browser CPU results;
- claim identity recognition, confirmed prompt violations, confirmed physical
  errors, or automatic repair;
- silently upload an original video, prompt, evidence frame, or complete
  report;
- imply that synthetic fixtures establish real-world accuracy;
- display a fabricated Benchmark, model result, or detector confidence as a
  measured production fact.

Marketing demonstrations can use structured mock data only when the whole
surface is visibly labelled `INTERACTIVE DEMO`, and any optional AI/OCR signal
inside it is individually labelled `OPTIONAL` or `DEMO`. A real user report
never merges with demo data.

### 2.3 Browser versus desktop boundary

The browser workbench is a convenient local preview. Browser media APIs cannot
reliably expose all container, codec, frame-rate, audio, or timestamp details.
Its reports therefore keep the distinct `0.1-browser` schema identifier and
carry an explicit browser-analysis warning.

The desktop CLI and local Web application remain the complete product for:

- FFmpeg/ffprobe validation;
- exact external-tool diagnostics;
- complete metadata;
- offline HTML artifact bundles;
- Benchmark and threshold calibration;
- optional OpenCLIP, DINOv2, and PaddleOCR providers;
- the shared model runtime and embedding cache;
- local FastAPI jobs and SSE progress.

The website can explain and link to these capabilities but cannot simulate
them in a real browser report.

## 3. Technical architecture

### 3.1 Repository surfaces

The repository keeps three explicit surfaces:

```text
src/videoscope/   Python CLI, pipeline, detectors, reports, local Web API
web/              local FastAPI dashboard client
site/             public GitHub Pages marketing site and browser-local tool
```

`site/` will be migrated from the current vinext/Next-compatible build to a
standard Vite, React, and strict TypeScript single-page application. This
reduces static-hosting complexity and matches GitHub Pages directly.

The public site does not import or duplicate Python implementation code.
Browser analysis stays in a typed public-site service with algorithms and
limitations documented independently.

### 3.2 Public application modules

```text
site/src/
  app/                 router, providers, error boundary
  components/          shared brand and product components
  features/
    home/
    upload/
    analysis/
    workspace/
    compare/
    report/
    auth/
  services/
    browser-analysis/
    report-store/
    share/
    auth/
  data/
    demo-report.ts
    media-manifest.ts
  i18n/
    en.ts
    zh-CN.ts
  styles/
    tokens.css
    globals.css
  types/
```

The browser analysis service exposes a stable adapter:

```text
analyzeLocalVideo(file, options, signal, onProgress) -> BrowserReport
```

UI components consume typed report data and never run detector math directly.
The service boundary permits a later local-API adapter without rewriting the
workspace.

### 3.3 Routing on GitHub Pages

User-facing routes:

- `/` — product website and Upload Lab;
- `/workspace` — current local analysis workspace;
- `/compare` — two-video comparison;
- `/report/:id` — local or explicitly shared report;
- `/auth/callback` — optional Supabase callback;
- `/privacy` — local-first and sharing policy;
- `/docs` — installation and product boundary overview.

Vite uses the repository base `/VideoScope/`. A GitHub Pages `404.html`
redirect shim restores SPA routes on refresh while preserving the original
path and query. Router tests cover the base path and direct refresh logic.

## 4. Identity, registration, and sign-in

### 4.1 Optional by design

Anonymous access is the default. No visitor must register to:

- upload a local file to the browser;
- run browser-local detectors;
- inspect Findings;
- compare two local files;
- export a local JSON report.

The header provides `Sign in`, but the primary call to action is always
`Analyze a video`.

### 4.2 Supabase Auth

Optional authentication uses Supabase Auth with:

- email magic link registration/sign-in;
- GitHub OAuth;
- session restoration;
- sign-out;
- account deletion instructions.

Public client configuration comes from Vite environment variables:

```text
VITE_SUPABASE_URL
VITE_SUPABASE_ANON_KEY
```

The anonymous key is treated as public configuration, not a secret. Row-level
security must restrict every user-owned table by `auth.uid()`. If Supabase is
not configured, the site remains fully usable anonymously and the sign-in
dialog shows a truthful configuration-unavailable state.

### 4.3 Account data

An account may store only:

- display language;
- theme;
- preferred analysis mode and detector selection;
- a report index containing user-supplied title, creation time, detector
  counts, and a local report identifier;
- explicit share records.

Original videos, sampled frames, evidence images, prompts, and complete reports
remain local by default.

### 4.4 Explicit sharing

`Create share link` is a separate consent flow. Before transmission, the dialog
lists exactly what will leave the device:

- sanitized report JSON;
- explicitly selected evidence images;
- optional user-entered report title.

It excludes the original video, absolute paths, local cache data, and prompt
unless the user separately opts in. The upload does not begin until the user
confirms. A shared report is read-only and can be revoked by its owner.

The initial public launch can ship account sign-in before share uploads. The
share button remains disabled with a clear `Not configured` explanation until
storage and row-level policies have been deployed and tested.

## 5. Internationalization

Supported locales:

- `en`;
- `zh-CN`.

All product copy, validation, progress stages, detector names, limitations,
empty states, dialogs, accessibility labels, and report explanations live in
central locale dictionaries. Components do not contain duplicated translated
strings.

Locale resolution order:

1. explicit user choice;
2. saved authenticated preference;
3. local storage preference;
4. browser language;
5. English fallback.

The language selector changes content immediately without navigation. The
literal creator mark `what912` never changes between locales.

Chinese and English share the same information hierarchy. Text containers
allow natural reflow rather than forcing matching line breaks.

## 6. Visual system

### 6.1 Brand concept

VideoScope is a **Video Observatory**, not a generic SaaS dashboard. The brand
mark combines an aperture, a frame, and a scanning axis. It remains legible at
16 pixels.

The visual hierarchy is:

1. real moving video;
2. diagnostic overlays and time position;
3. evidence and explanation;
4. atmospheric optical decoration.

Decoration never competes with the product surface.

### 6.2 Design tokens

Core dark tokens:

- Obsidian Black: `#050708`;
- Deep Graphite: `#0B1013`;
- Elevated Panel: `#10171B`;
- Soft Ivory: `#F1F3EE`;
- Muted Text: `#89959B`.

Signal tokens:

- Scope Cyan: `#62EAD8`;
- Signal Violet: `#9D8CFF`;
- Diagnostic Lime: `#BAEF72`;
- Warning Amber: `#F2B75F`;
- Critical Coral: `#FF756F`.

Tokens cover color, typography, spacing, radius, shadow, focus ring, and motion
duration. Severity always uses text or a symbol in addition to color.

### 6.3 Typography

Use a local/system sans-serif stack with Chinese-compatible fallbacks. No
remote font request is required. Titles use strong proportion and tight
tracking; timestamps, frame numbers, rates, and metrics use a monospaced
numeric stack.

### 6.4 Motion

Motion communicates analysis:

- a scan line follows the visible playhead;
- timeline Findings reveal in temporal order;
- metric traces respond to the current time;
- selecting a Finding moves the playhead, overlay, evidence, and detail panel;
- progress advances through named stages;
- comparison players stay synchronized when sync is enabled.

Animations use transform and opacity, remain short and interruptible, and
respect `prefers-reduced-motion`. Reduced-motion mode removes autoplay
decorative movement and replaces it with representative poster frames.

## 7. Media system

Every major media surface uses a different asset. No page creates the
appearance of variety by repeatedly cropping one source.

Approved visual asset roles:

| Role | Source concept |
| --- | --- |
| Hero atmosphere | blue-to-green abstract optical light |
| Product proof window | moving city nightlife |
| Upload Lab atmosphere | orange-and-blue liquid macro |
| Diagnosis narrative | fashion subject shot |
| Compare A | reflective green hills and lake |
| Compare B | starry night transitioning to sunrise |
| Evidence image A | sunset reflected on a lake |
| Evidence image B | people silhouetted by city lights |
| Evidence image C | artist in a vintage studio |

The initial media-source proposal was superseded before public publication by
the approved project-authored procedural media design dated 2026-07-31. No
third-party media source or download URL is part of the release plan.

## 8. Homepage

### 8.1 Header

The sticky header contains:

- VideoScope mark and name;
- Product, Features, Compare, Research, Open Source, Docs;
- GitHub;
- language switch;
- optional Sign in;
- Analyze a video;
- permanent `what912` attribution.

It begins transparent over the hero and gains an obscured graphite background
after scrolling.

### 8.2 Hero

The first viewport combines brand expression and actual product proof:

- `See what your video hides.`;
- a concise description of interval-level diagnosis and evidence;
- Analyze a video;
- View interactive demo;
- GitHub;
- local-first statement;
- a cinematic moving background;
- a real-looking but clearly labelled interactive demo window with video,
  scan line, timeline, Findings, and detector status.

The demo does not show an overall score. It shows a count of review intervals
and independent detector signals.

### 8.3 Upload Lab

The Upload Lab provides:

- drag-and-drop;
- file selection;
- a sample-video path;
- optional direct media URL entry;
- analysis mode selection;
- supported-format and resource guidance.

URL entry accepts only direct, browser-decodable media URLs whose server allows
cross-origin access. Before fetching, it explains that the browser will contact
the source host directly and that the source may observe the visitor's IP.
VideoScope does not proxy or extract videos from social platforms.

Modes:

- Quick Scan — four browser CPU detectors;
- Deep Analysis — denser sampling within safe caps;
- Research Mode — parameters and raw diagnostic series;
- Compare Videos — detector-by-detector comparison;
- Batch Evaluate — marked desktop-only and linked to CLI documentation.

### 8.4 Analysis progress

Progress is stage-based:

1. validating input;
2. reading browser metadata;
3. sampling local frames;
4. identifying scene changes;
5. running selected detectors;
6. selecting evidence;
7. assembling report;
8. complete.

The user can cancel. Cancellation revokes object URLs and releases in-memory
frames. There is no indefinite spinner without a stage label.

### 8.5 Interactive diagnosis narrative

A desktop sticky section keeps the player visible while narrative steps change
the selected Finding, overlay, timeline, and metric trace. It does not hijack
scrolling. Mobile uses a normal vertical sequence with manual tabs.

Demo topics can include browser CPU Findings and separately labelled optional
AI/OCR examples. The currently selected topic determines the video, annotation,
time interval, copy, and limitation.

### 8.6 Metrics spectrum

Metrics appear as a responsive temporal spectrum, not a repeated feature-card
grid. Hover or keyboard focus reveals:

- metric name;
- detector ID;
- detector-local score meaning;
- active time;
- limitation.

CPU and optional providers occupy separate labelled groups.

### 8.7 Compare, workflow, open source, and final CTA

The homepage includes:

- an interactive synchronized comparison preview;
- Upload → Analyze → Inspect → Improve;
- plugin protocol and JSON examples;
- reproducible Benchmark explanation without accuracy claims;
- a compact final upload entry;
- GitHub and documentation links.

## 9. Workspace

The `/workspace` route is the complete browser tool, not a screenshot.

Desktop layout:

- top global toolbar;
- collapsible left video/project rail;
- central video and diagnostic timeline;
- right Finding list and detail;
- expandable bottom signal panel.

Mobile layout:

- video and timeline first;
- detector filters below;
- Finding detail in a bottom sheet;
- project rail in a drawer.

Core interactions:

- play/pause;
- seek and frame-step approximation;
- playback speed;
- current-time display;
- Finding and detector filters;
- click a Finding to seek and highlight;
- click evidence to seek;
- mark as reviewed locally;
- copy timestamp;
- JSON export;
- print-friendly report;
- new analysis;
- clear local data.

Finding rows show title, interval, severity text/symbol, confidence, observable
description, evidence thumbnail, and reviewed state. Failed detectors appear in
a dedicated error group.

The workspace presents no universal score. Summary surfaces show:

- Finding count;
- counts by severity;
- completed, skipped, and failed detectors;
- browser analysis limitations.

## 10. Compare

`/compare` accepts two local files or two reports. It provides:

- side-by-side video;
- synchronized or independent playback;
- shared seek;
- frame-step approximation;
- swap A/B;
- aligned detector timelines;
- detector-level difference table;
- selected interval details;
- evidence pairs;
- neutral observable summary.

Comparison never declares one video universally better. It can state, for
example, that A has fewer detected luminance-flicker intervals while B has a
higher scene-relative sharpness baseline.

Different durations remain independent unless the user selects normalized
timeline mode. Missing detectors and incompatible browser reports are shown as
unknown, not zero.

## 11. Report

`/report/:id` supports:

- locally stored browser reports;
- explicitly shared sanitized reports;
- demo reports under a dedicated demo identifier.

Creator View uses concise explanations and review priorities. Research View
adds detector IDs, versions, parameters, confidence, raw diagnostic summaries,
and limitations. Both views consume the same report data.

The report includes:

- video metadata available to the browser;
- detector execution status;
- severity distribution;
- temporal Finding map;
- Finding details and evidence;
- browser-specific warning;
- analysis time and version;
- JSON download;
- print styles.

There is no PDF-generation claim until a real PDF export is implemented.
`Print / Save as PDF` is labelled as a browser print action.

## 12. Data and local persistence

Large media and evidence remain in memory only for the active session unless
the user explicitly exports them. Object URLs are revoked when replaced or
when the page unloads.

IndexedDB may store:

- compact browser report JSON;
- report title and local identifier;
- reviewed flags;
- preferences;
- small evidence thumbnails within a documented quota.

It must not silently persist the full original video. The report library shows
storage usage and provides delete-all controls.

The demo report is a single centralized typed fixture. Components never define
their own conflicting demo metrics.

## 13. Error and empty states

Required states:

- no file selected;
- unsupported media type;
- browser cannot decode the file;
- duration unavailable;
- file exceeds configured public limit;
- canvas unavailable or blocked;
- memory pressure or sample cap reached;
- URL blocked by CORS;
- analysis cancelled;
- one detector failed;
- analysis produced no Findings;
- local report missing;
- shared report revoked or expired;
- authentication provider unavailable;
- offline while attempting optional sign-in/share.

Messages explain the next action and do not expose local paths or internal
stacks. A detector error never becomes “no issue found.”

## 14. Security and privacy

- no original video upload in anonymous browser analysis;
- no remote analytics by default;
- no remote fonts or runtime CDN dependencies;
- strict content security policy compatible with local media and Supabase when
  enabled;
- dynamic text rendered through React escaping;
- sanitized report sharing schema;
- no HTML created from untrusted strings;
- direct URL import requires an explicit network notice;
- object URLs and in-memory frames are released;
- Supabase tables use row-level security;
- OAuth callback validates state through the provider client;
- environment files and real service keys are excluded from Git;
- `what912` attribution is fixed literal content.

## 15. Accessibility

- semantic landmarks and heading order;
- visible keyboard focus;
- labelled form controls;
- keyboard-operable timeline and Findings;
- severity text/symbol in addition to color;
- alt text for evidence;
- captions or non-audio presentation for decorative video;
- reduced-motion behavior;
- minimum readable contrast;
- dialogs with focus trapping and escape handling;
- touch targets suitable for mobile;
- no autoplay audio.

## 16. Performance

Performance budget principles:

- hero video has a compact poster and a constrained locally bundled version;
- only the hero media can preload metadata;
- all other videos and images lazy-load;
- offscreen decorative video pauses;
- reduced-data mode uses posters;
- browser sampling works incrementally and yields to the main thread;
- decoded frames are not retained twice;
- report thumbnails are capped in dimensions and quality;
- routes below the homepage are code-split;
- charts use SVG/CSS and avoid a large 3D or UI framework.

Target checks use Lighthouse as diagnostics, not as fabricated guarantees.
The implementation plan will set concrete size budgets after measuring the
selected local media.

## 17. Testing

Public-site automated coverage:

- strict TypeScript;
- lint;
- production build under `/VideoScope/`;
- router refresh and base-path handling;
- English and Chinese dictionary completeness;
- language persistence and fixed `what912`;
- browser analysis detector unit tests;
- deterministic Finding order;
- upload validation and cancellation;
- no-Finding and detector-error views;
- timeline interval geometry and keyboard use;
- comparison synchronization and duration mismatch;
- report Creator/Research switching;
- HTML escaping and share-schema sanitization;
- auth unavailable, anonymous, signed-in, and signed-out states;
- no external network during base tests;
- asset manifest and no hotlinked production media;
- reduced-motion behavior.

Manual acceptance:

- desktop, tablet, and mobile widths;
- Firefox, Chromium, and Safari-compatible behavior where available;
- local analysis of generated fixture videos;
- click Finding → video seek → timeline/overlay/detail synchronization;
- compare playback and swap;
- refresh `/workspace`, `/compare`, and `/report/:id`;
- anonymous use without Supabase;
- optional auth after real Supabase configuration;
- public GitHub Pages access in a signed-out browser.

## 18. Deployment

GitHub repository:

```text
https://github.com/what912/VideoScope
```

Public site:

```text
https://what912.github.io/VideoScope/
```

GitHub Actions will:

1. install the pinned Node version;
2. install `site/` dependencies from the lockfile;
3. run tests, lint, and TypeScript checks;
4. build with the `/VideoScope/` base;
5. upload the static artifact;
6. deploy to GitHub Pages.

The workflow does not publish PyPI, download AI models, expose service-role
keys, or deploy the local FastAPI API.

Supabase configuration is supplied through GitHub Actions variables/secrets.
The public anonymous key may be a variable; any privileged key is prohibited
from the client and deployment workflow.

## 19. Acceptance criteria

The design is complete when:

- the first viewport communicates interval-level video diagnosis;
- every major media surface uses a distinct locally bundled asset;
- the public site works without sign-in;
- English and Simplified Chinese cover the whole product;
- `what912` is permanently visible and language-independent;
- local browser analysis produces real heuristic Findings for supported files;
- no real report contains demo data;
- timeline, video, overlays, evidence, and Finding detail are synchronized;
- workspace, compare, and report are functional routes;
- no uncalibrated overall quality score appears;
- optional AI/OCR examples are unmistakably optional/demo;
- original video remains local by default;
- optional account and sharing flows disclose transmitted data;
- direct-route refresh works on GitHub Pages;
- tests, lint, TypeScript, and production build pass;
- the deployed public URL works for unauthenticated visitors.

## 20. Known dependencies and blockers

- A Supabase project and allowed callback URLs are required before real email
  and GitHub sign-in can be verified.
- GitHub Pages must be enabled for `what912/VideoScope`.
- Public-site media must be procedurally generated from the checked-in manifest
  before production deployment.
- Browser codec support varies; the UI must maintain a desktop-install
  fallback.
- Direct URL analysis depends on the source server's CORS policy.
- Real-world detector calibration remains outside this website design and
  must not be implied by its polish.
