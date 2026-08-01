# VideoScope Public Product Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current `site/` prototype with a bilingual, GitHub Pages-ready VideoScope product website and browser-local diagnostic workbench, including real CPU heuristic analysis, comparison, reports, optional Supabase authentication, and the approved Video Observatory visual system.

**Architecture:** Build `site/` as a strict TypeScript Vite SPA with route-level code splitting. Keep browser analysis behind a typed service boundary, keep report data in a local IndexedDB repository, and make authentication/share adapters optional so anonymous local analysis works with no network or service configuration. Continue treating the Python CLI/FastAPI surfaces as the complete desktop path and never merge demo data into real reports.

**Tech Stack:** React 19, TypeScript 5.9, Vite 8, React Router, Motion, Vitest, Testing Library, ESLint, native Canvas/HTMLMediaElement APIs, streaming SHA-256 through `hash-wasm`, IndexedDB through `idb`, optional `@supabase/supabase-js`, GitHub Actions, GitHub Pages.

## Global Constraints

- Follow `AGENTS.md`, `docs/product-spec.md`, `docs/architecture.md`, `docs/roadmap.md`, `docs/report-schema.md`, and the approved design spec before every task.
- Preserve `src/videoscope/` and `web/`; this plan changes the public `site/` surface and supporting documentation/workflows only.
- Do not show an overall quality score or combine detector signals into a universal ranking.
- Clearly label all marketing fixture content `INTERACTIVE DEMO`; label AI/OCR examples `OPTIONAL` or `DEMO`.
- Real browser reports contain only results calculated from the selected input.
- Anonymous analysis must not send the original video, prompt, sampled frames, evidence, or report to a server.
- Base tests must be offline, CPU-only, deterministic, and must not initialize Supabase.
- Keep the fixed literal `what912` visible in every locale.
- Do not hotlink production media. Generate project-authored media from the
  checked-in manifest and serve it from `site/public/media/`.
- Do not add service-role keys, privileged credentials, or production secrets to the client or repository.
- Commit commands below are proposed review checkpoints. Execute them only when the user has explicitly authorized commits.
- After each task run its focused tests. After every implementation task run `npm run check` in `site/`; before completion also run the repository-wide `python scripts/validate.py`.

---

## File Map

### Remove obsolete public-site implementation

- Delete: `site/app/`
- Delete: `site/build/`
- Delete: `site/db/`
- Delete: `site/drizzle/`
- Delete: `site/examples/`
- Delete: `site/worker/`
- Delete: `site/.openai/hosting.json`
- Delete: `site/drizzle.config.ts`
- Delete: `site/next.config.ts`
- Delete: `site/postcss.config.mjs`
- Delete: `site/tests/rendered-html.test.mjs`

### Replace public-site foundation

- Modify: `site/package.json`
- Modify: `site/package-lock.json`
- Modify: `site/tsconfig.json`
- Modify: `site/vite.config.ts`
- Modify: `site/eslint.config.mjs`
- Modify: `site/.gitignore`
- Add: `site/index.html`
- Add: `site/public/404.html`
- Add: `site/public/robots.txt`
- Add: `site/public/site.webmanifest`
- Add: `site/src/main.tsx`
- Add: `site/src/vite-env.d.ts`
- Add: `site/src/app/App.tsx`
- Add: `site/src/app/AppProviders.tsx`
- Add: `site/src/app/AppErrorBoundary.tsx`
- Add: `site/src/app/router.tsx`
- Add: `site/src/test/setup.ts`
- Add: `site/vitest.config.ts`

### Types, data, and localization

- Add: `site/src/types/analysis.ts`
- Add: `site/src/types/compare.ts`
- Add: `site/src/types/report.ts`
- Add: `site/src/types/auth.ts`
- Add: `site/src/data/demo-report.ts`
- Add: `site/src/data/media-manifest.ts`
- Add: `site/src/i18n/types.ts`
- Add: `site/src/i18n/en.ts`
- Add: `site/src/i18n/zh-CN.ts`
- Add: `site/src/i18n/I18nProvider.tsx`
- Add: `site/src/i18n/dictionary-parity.test.ts`

### Design system and global shell

- Add: `site/src/styles/tokens.css`
- Add: `site/src/styles/globals.css`
- Add: `site/src/styles/print.css`
- Add: `site/src/components/brand/ScopeMark.tsx`
- Add: `site/src/components/brand/CreatorBadge.tsx`
- Add: `site/src/components/layout/Header.tsx`
- Add: `site/src/components/layout/MobileNavigation.tsx`
- Add: `site/src/components/layout/Footer.tsx`
- Add: `site/src/components/layout/PageTransition.tsx`
- Add: `site/src/components/feedback/EmptyState.tsx`
- Add: `site/src/components/feedback/LoadingState.tsx`
- Add: `site/src/components/feedback/ErrorState.tsx`

### Local media pipeline

- Add: `site/scripts/prepare-media.mjs`
- Add: `site/scripts/verify-media.mjs`
- Add: `site/public/media/media-sources.json`
- Add: `site/public/media/ATTRIBUTION.md`
- Add generated: `site/public/media/*.mp4`
- Add generated: `site/public/media/*.webp`
- Add: `site/src/components/media/ViewportVideo.tsx`
- Add: `site/src/components/media/ViewportVideo.test.tsx`

### Browser analysis

- Add: `site/src/services/browser-analysis/contracts.ts`
- Add: `site/src/services/browser-analysis/config.ts`
- Add: `site/src/services/browser-analysis/errors.ts`
- Add: `site/src/services/browser-analysis/hash.ts`
- Add: `site/src/services/browser-analysis/metrics.ts`
- Add: `site/src/services/browser-analysis/intervals.ts`
- Add: `site/src/services/browser-analysis/sampler.ts`
- Add: `site/src/services/browser-analysis/scene-segmentation.ts`
- Add: `site/src/services/browser-analysis/evidence.ts`
- Add: `site/src/services/browser-analysis/detectors/near-black.ts`
- Add: `site/src/services/browser-analysis/detectors/possible-freeze.ts`
- Add: `site/src/services/browser-analysis/detectors/scene-relative-blur.ts`
- Add: `site/src/services/browser-analysis/detectors/global-flicker.ts`
- Add: `site/src/services/browser-analysis/analyze-local-video.ts`
- Add: `site/src/services/browser-analysis/url-import.ts`
- Add: `site/src/services/browser-analysis/*.test.ts`
- Add: `site/src/services/browser-analysis/detectors/*.test.ts`

### Upload and processing

- Add: `site/src/features/upload/analysis-modes.ts`
- Add: `site/src/features/upload/AnalysisModeSelector.tsx`
- Add: `site/src/features/upload/UploadDropzone.tsx`
- Add: `site/src/features/upload/UrlImportDialog.tsx`
- Add: `site/src/features/upload/ProcessingPipeline.tsx`
- Add: `site/src/features/upload/useAnalysisJob.ts`
- Add: `site/src/features/upload/upload-validation.ts`
- Add: `site/src/features/upload/*.test.tsx`

### Shared diagnostic UI

- Add: `site/src/components/diagnostics/VideoPlayer.tsx`
- Add: `site/src/components/diagnostics/DiagnosticOverlay.tsx`
- Add: `site/src/components/diagnostics/DiagnosticTimeline.tsx`
- Add: `site/src/components/diagnostics/TimelineMarker.tsx`
- Add: `site/src/components/diagnostics/IssueList.tsx`
- Add: `site/src/components/diagnostics/IssueCard.tsx`
- Add: `site/src/components/diagnostics/IssueDetailPanel.tsx`
- Add: `site/src/components/diagnostics/MetricBar.tsx`
- Add: `site/src/components/diagnostics/MetricChart.tsx`
- Add: `site/src/components/diagnostics/DetectorStatusList.tsx`
- Add: `site/src/components/diagnostics/diagnostic-geometry.ts`
- Add: `site/src/components/diagnostics/*.test.tsx`

### Homepage

- Add: `site/src/features/home/HomePage.tsx`
- Add: `site/src/features/home/Hero.tsx`
- Add: `site/src/features/home/ProductProofWindow.tsx`
- Add: `site/src/features/home/UploadLab.tsx`
- Add: `site/src/features/home/InteractiveDiagnosisDemo.tsx`
- Add: `site/src/features/home/MetricsSpectrum.tsx`
- Add: `site/src/features/home/ComparePreview.tsx`
- Add: `site/src/features/home/WorkflowSection.tsx`
- Add: `site/src/features/home/OpenSourceSection.tsx`
- Add: `site/src/features/home/FinalCta.tsx`
- Add: `site/src/features/home/HomePage.test.tsx`

### Workspace

- Add: `site/src/features/workspace/WorkspacePage.tsx`
- Add: `site/src/features/workspace/ProjectRail.tsx`
- Add: `site/src/features/workspace/WorkspaceToolbar.tsx`
- Add: `site/src/features/workspace/SignalPanel.tsx`
- Add: `site/src/features/workspace/WorkspaceSummary.tsx`
- Add: `site/src/features/workspace/workspace-session.ts`
- Add: `site/src/features/workspace/*.test.tsx`

### Comparison

- Add: `site/src/features/compare/ComparePage.tsx`
- Add: `site/src/features/compare/ComparisonPlayer.tsx`
- Add: `site/src/features/compare/ComparisonTimeline.tsx`
- Add: `site/src/features/compare/DetectorDifferenceTable.tsx`
- Add: `site/src/features/compare/compare-reports.ts`
- Add: `site/src/features/compare/useSynchronizedPlayers.ts`
- Add: `site/src/features/compare/*.test.tsx`

### Report persistence and report route

- Add: `site/src/services/report-store/report-store.ts`
- Add: `site/src/services/report-store/indexeddb-report-store.ts`
- Add: `site/src/services/report-store/memory-report-store.ts`
- Add: `site/src/services/report-store/indexeddb-report-store.test.ts`
- Add: `site/src/features/report/ReportPage.tsx`
- Add: `site/src/features/report/ReportSummary.tsx`
- Add: `site/src/features/report/CreatorReportView.tsx`
- Add: `site/src/features/report/ResearchReportView.tsx`
- Add: `site/src/features/report/ExportDialog.tsx`
- Add: `site/src/features/report/ShareDialog.tsx`
- Add: `site/src/features/report/report-export.ts`
- Add: `site/src/features/report/*.test.tsx`

### Optional authentication and sanitized sharing

- Add: `site/.env.example`
- Add: `site/src/services/auth/auth-client.ts`
- Add: `site/src/services/auth/supabase-auth-client.ts`
- Add: `site/src/services/auth/unavailable-auth-client.ts`
- Add: `site/src/features/auth/AuthProvider.tsx`
- Add: `site/src/features/auth/SignInDialog.tsx`
- Add: `site/src/features/auth/AuthCallbackPage.tsx`
- Add: `site/src/features/auth/AccountMenu.tsx`
- Add: `site/src/features/auth/*.test.tsx`
- Add: `site/src/services/share/share-client.ts`
- Add: `site/src/services/share/sanitize-report.ts`
- Add: `site/src/services/share/unavailable-share-client.ts`
- Add: `site/src/services/share/supabase-share-client.ts`
- Add: `site/src/services/share/*.test.ts`
- Add: `supabase/migrations/202607290001_public_site_auth_and_reports.sql`
- Add: `supabase/README.md`

### Static information, deployment, and documentation

- Add: `site/src/features/static/PrivacyPage.tsx`
- Add: `site/src/features/static/DocsPage.tsx`
- Add: `site/src/features/static/NotFoundPage.tsx`
- Add: `site/src/app/router.test.tsx`
- Add: `site/src/app/no-remote-runtime-assets.test.ts`
- Add: `.github/workflows/pages.yml`
- Modify: `README.md`
- Modify: `docs/frontend.md`
- Modify: `docs/architecture.md`
- Modify: `docs/decisions/0001-local-first.md`
- Add: `docs/public-site.md`
- Add: `docs/public-site-release-checklist.md`

---

## Task 1: Replace vinext/Next with a tested Vite SPA foundation

**Files:**

- Modify: `site/package.json`
- Modify: `site/package-lock.json`
- Modify: `site/tsconfig.json`
- Modify: `site/vite.config.ts`
- Modify: `site/eslint.config.mjs`
- Add: `site/index.html`
- Add: `site/src/main.tsx`
- Add: `site/src/app/App.tsx`
- Add: `site/src/app/AppProviders.tsx`
- Add: `site/src/app/AppErrorBoundary.tsx`
- Add: `site/src/app/router.tsx`
- Add: `site/src/test/setup.ts`
- Add: `site/vitest.config.ts`
- Delete the obsolete files listed under “Remove obsolete public-site implementation”.

- [ ] **Step 1: Write the routing smoke test**

Create `site/src/app/router.test.tsx` with a memory router test that requires the
four primary product routes and verifies the permanent owner mark:

```tsx
it.each([
  ["/", "See what your video hides"],
  ["/workspace", "Workspace"],
  ["/compare", "Compare videos"],
  ["/report/demo", "Report"],
])("renders %s", async (path, expected) => {
  render(<TestApp initialEntries={[path]} />);
  expect(await screen.findByText(expected, { exact: false })).toBeVisible();
  expect(screen.getByText("what912")).toBeVisible();
});
```

- [ ] **Step 2: Run the test and confirm the old scaffold cannot satisfy it**

Run:

```powershell
cd site
npx vitest run src/app/router.test.tsx
```

Expected: failure because Vitest, the new app shell, and the routes do not yet
exist.

- [ ] **Step 3: Replace the dependency and script set**

Set production dependencies to:

```json
{
  "@supabase/supabase-js": "2.57.4",
  "hash-wasm": "4.12.0",
  "idb": "8.0.3",
  "motion": "12.23.12",
  "react": "19.2.6",
  "react-dom": "19.2.6",
  "react-router-dom": "7.8.2"
}
```

Set development dependencies to Vite, TypeScript, ESLint, Vitest, jsdom,
Testing Library, and React type packages. Replace scripts with:

```json
{
  "dev": "vite",
  "build": "vite build",
  "preview": "vite preview",
  "test": "vitest run",
  "test:watch": "vitest",
  "lint": "eslint . --max-warnings=0",
  "typecheck": "tsc --noEmit",
  "check": "npm run lint && npm run typecheck && npm run test && npm run build",
  "media:prepare": "node scripts/prepare-media.mjs",
  "media:verify": "node scripts/verify-media.mjs"
}
```

Remove Next, vinext, Drizzle, Cloudflare, Tailwind, and worker-only
dependencies.

- [ ] **Step 4: Configure Vite and the SPA shell**

Use `base: "/VideoScope/"`, `@vitejs/plugin-react`, route-level lazy imports,
and a root error boundary. The router factory must accept `initialEntries` for
tests and use `createBrowserRouter` with `basename: "/VideoScope"` in
production.

- [ ] **Step 5: Install the exact lockfile and run checks**

Run:

```powershell
npm install
npm run lint
npm run typecheck
npm test
npm run build
```

Expected: all commands pass and `site/dist/index.html` references assets under
`/VideoScope/`.

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add site
git commit -m "build(site): migrate public app to Vite"
```

## Task 2: Establish the bilingual design system and application shell

**Files:**

- Add: `site/src/styles/tokens.css`
- Add: `site/src/styles/globals.css`
- Add: `site/src/styles/print.css`
- Add: `site/src/i18n/types.ts`
- Add: `site/src/i18n/en.ts`
- Add: `site/src/i18n/zh-CN.ts`
- Add: `site/src/i18n/I18nProvider.tsx`
- Add: `site/src/i18n/dictionary-parity.test.ts`
- Add: `site/src/components/brand/ScopeMark.tsx`
- Add: `site/src/components/brand/CreatorBadge.tsx`
- Add: `site/src/components/layout/Header.tsx`
- Add: `site/src/components/layout/MobileNavigation.tsx`
- Add: `site/src/components/layout/Footer.tsx`
- Add: `site/src/components/layout/PageTransition.tsx`
- Add: `site/src/components/feedback/EmptyState.tsx`
- Add: `site/src/components/feedback/LoadingState.tsx`
- Add: `site/src/components/feedback/ErrorState.tsx`

- [ ] **Step 1: Write locale parity and persistence tests**

Use a recursive key flattener and assert:

```ts
expect(flattenKeys(zhCN)).toEqual(flattenKeys(en));
```

Render the provider, switch to `zh-CN`, remount, and require the saved locale.
Assert that `what912` remains the exact same literal in both locales.

- [ ] **Step 2: Run the tests and confirm they fail**

```powershell
npx vitest run src/i18n/dictionary-parity.test.ts
```

- [ ] **Step 3: Implement typed dictionaries and locale resolution**

Define:

```ts
export type Locale = "en" | "zh-CN";
export type Dictionary = typeof en;

export interface I18nValue {
  locale: Locale;
  t: Dictionary;
  setLocale(locale: Locale): void;
}
```

Resolution order is explicit choice, saved preference, browser language, then
English. Set `document.documentElement.lang` on changes. Do not localize
`what912`.

- [ ] **Step 4: Implement Video Observatory tokens**

Define CSS variables for all approved color, type, spacing, radius, shadow,
focus, z-index, and motion values. Add dark/light themes, monospaced numeric
styles, visible focus, reduced motion, reduced data, responsive breakpoints,
and print styles. Do not load remote fonts.

- [ ] **Step 5: Build the accessible shell**

The header includes brand, Product, Features, Compare, Research, Open Source,
Docs, GitHub, language switch, optional Sign in, Analyze a video, and fixed
`what912`. Mobile navigation must trap focus while open, close with Escape,
and restore focus to its trigger.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src
git commit -m "feat(site): add bilingual observatory design system"
```

## Task 3: Prepare distinct licensed local media

**Files:**

- Add: `site/scripts/prepare-media.mjs`
- Add: `site/scripts/verify-media.mjs`
- Add: `site/public/media/media-sources.json`
- Add: `site/public/media/ATTRIBUTION.md`
- Add generated local video and poster files under `site/public/media/`
- Add: `site/src/data/media-manifest.ts`
- Add: `site/src/components/media/ViewportVideo.tsx`
- Add: `site/src/components/media/ViewportVideo.test.tsx`

- [ ] **Step 1: Write the media behavior and manifest tests**

Require:

```tsx
expect(video).toHaveAttribute("muted");
expect(video).toHaveAttribute("playsinline");
expect(video).not.toHaveAttribute("autoplay");
```

Mock `IntersectionObserver`; the component may play only while intersecting,
must pause outside the viewport, and must remain a poster under reduced motion.
Verify every homepage role has a unique local filename.

- [ ] **Step 2: Run the focused tests and confirm failure**

```powershell
npx vitest run src/components/media/ViewportVideo.test.tsx
```

- [ ] **Steps 3–6: Historical media-source proposal**

The initial media-source proposal was superseded before public publication by
the approved project-authored procedural media design dated 2026-07-31. No
third-party media source or download URL is part of the release plan.

- [ ] **Step 7: Verify the component and full build**

```powershell
npm run check
```

- [ ] **Step 8: Record the review checkpoint**

```powershell
git add site/public/media site/scripts site/src/data site/src/components/media
git commit -m "feat(site): add licensed local observatory media"
```

## Task 4: Define report types, centralized demo data, and deterministic local storage

**Files:**

- Add: `site/src/types/analysis.ts`
- Add: `site/src/types/compare.ts`
- Add: `site/src/types/report.ts`
- Add: `site/src/data/demo-report.ts`
- Add: `site/src/services/report-store/report-store.ts`
- Add: `site/src/services/report-store/indexeddb-report-store.ts`
- Add: `site/src/services/report-store/memory-report-store.ts`
- Add: `site/src/services/report-store/indexeddb-report-store.test.ts`

- [ ] **Step 1: Write the report-store contract tests**

The shared suite must run against the memory repository and fake IndexedDB:

```ts
interface ReportStore {
  put(report: BrowserReport): Promise<void>;
  get(id: string): Promise<BrowserReport | null>;
  list(): Promise<ReportIndexEntry[]>;
  delete(id: string): Promise<void>;
  clear(): Promise<void>;
  usage(): Promise<StorageUsage>;
}
```

Assert newest-first ordering, local ID lookup, replacement by ID, delete-all,
and no original `File` or object URL persistence.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/services/report-store/indexeddb-report-store.test.ts
```

- [ ] **Step 3: Define the stable browser schema**

Include detector version, configuration, executions with
`ok | skipped | failed`, Finding interval, severity, detector-local score,
confidence, evidence, parameters, limitations, warnings, runtime, and
`schema_version: "0.1-browser"`.

Define `source: "real" | "demo"` and require all demo records to use
`source: "demo"` plus a visible label. Real analysis constructors may only
return `source: "real"`.

- [ ] **Step 4: Create one centralized demo report**

Use the approved five example intervals and independent metrics, but omit the
proposed `Overall Score: 82`. The demo summary must use “5 review intervals,”
severity counts, and independent detector/optional-signal values. Mark
Temporal Flicker and browser CPU examples separately from optional subject,
geometry, text, and background demo signals.

- [ ] **Step 5: Implement IndexedDB with a quota-safe record**

Store compact report JSON, reviewed IDs, preferences, and capped thumbnails.
Never store the original file or full-resolution evidence. Provide a memory
fallback when IndexedDB is unavailable and surface a non-fatal warning.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src/types site/src/data/demo-report.ts site/src/services/report-store
git commit -m "feat(site): add typed browser reports and local store"
```

## Task 5: Refactor the four real browser CPU detectors behind a service boundary

**Files:**

- Add all files under `site/src/services/browser-analysis/`
- Remove: `site/app/analyzer.ts`

- [ ] **Step 1: Write pure metric, interval, and detector tests first**

Create numeric fixtures with timestamps, luma, dark ratio, sharpness,
perceptual hashes, differences, and scenes. Cover:

- valid and invalid intervals;
- deterministic merge and Finding order;
- a near-black run;
- a static run contained within one scene;
- scene-relative sharpness drop;
- alternating high-frequency luminance residuals;
- clean motion producing no high Finding;
- scene boundaries resetting freeze and flicker runs;
- empty and very short series.

Use interval assertions such as:

```ts
expect(findings[0].time_range).toEqual({
  start_seconds: 2,
  end_seconds: 4,
});
expect(findings[0].title).toBe("Possible frozen or repeated frames");
```

- [ ] **Step 2: Run and confirm failures**

```powershell
npx vitest run src/services/browser-analysis
```

- [ ] **Step 3: Implement contracts and all configurable thresholds**

Expose:

```ts
export interface BrowserAnalysisService {
  analyzeLocalVideo(
    file: File,
    options: BrowserAnalysisOptions,
    signal: AbortSignal,
    onProgress: (event: AnalysisProgress) => void,
  ): Promise<BrowserReport>;
}
```

Thresholds live only in typed configuration objects. Detectors receive sampled
metrics and scene context and never access React, DOM state, other detectors,
storage, or report components.

- [ ] **Step 4: Implement streaming SHA-256 and deterministic IDs**

Use `hash-wasm`'s incremental SHA-256 with `file.stream().getReader()` so the
whole video is never materialized as a second in-memory `ArrayBuffer`:

```ts
const hash = await createSHA256();
const reader = file.stream().getReader();
for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  if (signal.aborted) throw abortError();
  hash.update(value);
}
return hash.digest("hex");
```

Build Finding IDs from video hash, detector ID/version, rounded interval, and
effective configuration. The analysis ID may remain random.

- [ ] **Step 5: Implement incremental browser sampling**

Use one hidden `HTMLVideoElement`, one downscaled canvas, bounded samples, and
yield between frames. Revoke the analysis object URL in `finally`. Capture
capped JPEG evidence only for chosen timestamps instead of retaining every
full frame as a data URL.

- [ ] **Step 6: Isolate detector failures**

Wrap each detector except cancellation/system errors. Return a failed execution
with sanitized error type/message and continue the remaining detectors.
Finding order is start time, severity rank, detector ID, then deterministic ID.

- [ ] **Step 7: Test the orchestrator using a fake sampler**

Require:

- progress stage order;
- cancellation;
- one failed detector does not erase successful results;
- repeated input/config produces identical Findings;
- no local absolute path in serialized JSON;
- real reports never contain demo IDs or labels.

- [ ] **Step 8: Verify**

```powershell
npm run check
```

- [ ] **Step 9: Record the review checkpoint**

```powershell
git add site/src/services/browser-analysis
git commit -m "feat(site): add browser-local CPU analysis service"
```

## Task 6: Implement upload validation, modes, URL consent, and staged analysis

**Files:**

- Add all files under `site/src/features/upload/`
- Add: `site/src/services/browser-analysis/url-import.ts`

- [ ] **Step 1: Write upload and job-hook tests**

Cover file selection, drag state, unsupported types, 500 MiB limit, no
detectors selected, cancellation, stage ordering, object URL cleanup, and
localized errors. Inject a fake analysis service rather than decoding media in
component tests.

For URL import, test:

```ts
await expect(importDirectMediaUrl("javascript:alert(1)", deps))
  .rejects.toMatchObject({ code: "invalid_url" });
expect(deps.fetch).not.toHaveBeenCalled();
```

Also cover HTTPS CORS failure, missing video content type, streamed size limit,
and explicit consent before `fetch`.

- [ ] **Step 2: Run focused tests and confirm failures**

```powershell
npx vitest run src/features/upload src/services/browser-analysis/url-import.test.ts
```

- [ ] **Step 3: Implement analysis modes**

Use truthful mode definitions:

- Quick Scan: four CPU detectors, conservative sampling cap;
- Deep Analysis: denser browser sampling within the same detector set;
- Research Mode: exposes raw series and parameters;
- Compare Videos: routes to `/compare`;
- Batch Evaluate: disabled browser action linking to desktop CLI docs.

Do not label a CPU mode as AI.

- [ ] **Step 4: Implement `useAnalysisJob`**

Model states:

```ts
type JobState =
  | { status: "idle" }
  | { status: "running"; progress: AnalysisProgress }
  | { status: "completed"; report: BrowserReport }
  | { status: "cancelled" }
  | { status: "failed"; error: PublicAnalysisError };
```

On completion, persist the compact report, retain the active object URL only
for the session, and navigate to `/workspace?report=<id>`.

- [ ] **Step 5: Implement the accessible Upload Lab**

Support input, drag/drop, direct URL, sample video, detector selection,
advanced settings, named progress stages, cancellation, and clear privacy
copy. The URL dialog must state that the source host receives the network
request and may observe the visitor IP.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src/features/upload site/src/services/browser-analysis/url-import.ts
git commit -m "feat(site): add local upload and staged analysis flow"
```

## Task 7: Build the synchronized player, timeline, overlay, issue, and metric components

**Files:**

- Add all files under `site/src/components/diagnostics/`

- [ ] **Step 1: Write geometry and keyboard tests**

Implement and test:

```ts
export function intervalToPercent(
  startSeconds: number,
  endSeconds: number,
  durationSeconds: number,
): { left: number; width: number };
```

Clamp invalid ranges safely. Test keyboard seeking with ArrowLeft/ArrowRight,
Home/End, and Enter/Space selection. Severity must include visible text or a
symbol, never color alone.

- [ ] **Step 2: Run focused tests and confirm failures**

```powershell
npx vitest run src/components/diagnostics
```

- [ ] **Step 3: Implement controlled video state**

`VideoPlayer` receives `currentTime`, `playing`, `playbackRate`,
`selectedFinding`, and callbacks. It must not own report selection. This lets
workspace, homepage demo, compare, and report reuse the same primitives.

- [ ] **Step 4: Implement the branded timeline**

Render detector rows, severity segments, a scan-line playhead, tick marks, and
hover/focus evidence previews. The timeline exposes an accessible slider and
buttons for Findings; it does not rely on pixel color alone.

- [ ] **Step 5: Implement linked issue and metric views**

Selecting an issue updates the overlay, timeline segment, metric cursor, and
detail panel. Evidence buttons emit their timestamp. `IssueDetailPanel`
renders title, interval, severity, detector-local score, confidence,
description, limitations, parameters, and evidence.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src/components/diagnostics
git commit -m "feat(site): add synchronized diagnostic components"
```

## Task 8: Implement the complete workspace route

**Files:**

- Add all files under `site/src/features/workspace/`
- Modify: `site/src/app/router.tsx`

- [ ] **Step 1: Write workspace integration tests**

Render a real typed report with a fake session video URL. Verify:

- clicking a Finding seeks the player;
- clicking evidence seeks to its timestamp;
- the active timeline marker and detail panel change together;
- marking reviewed persists locally;
- detector filtering works;
- no-Finding and detector-error states remain distinct;
- mobile detail opens as a bottom sheet;
- no universal score is rendered.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/features/workspace
```

- [ ] **Step 3: Implement workspace session ownership**

Keep session-only `File` and object URL in a module-level store scoped to the
SPA lifecycle. Persist only the report/index. On refresh without a video URL,
show report evidence and metadata with a truthful “original video is no longer
loaded” state and a reselect action.

- [ ] **Step 4: Implement desktop and mobile layouts**

Desktop: toolbar, collapsible project rail, central player/timeline, right
Finding panel, expandable signal panel. Mobile: player/timeline first, filters
below, bottom sheet detail, drawer project rail. Include playback speed,
frame-step approximation, filters, copy timestamp, export, new analysis, and
clear-local-data actions.

- [ ] **Step 5: Verify**

```powershell
npm run check
```

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add site/src/features/workspace site/src/app/router.tsx
git commit -m "feat(site): add interactive browser workspace"
```

## Task 9: Implement the cinematic homepage and interactive demo narrative

**Files:**

- Add all files under `site/src/features/home/`
- Modify: `site/src/app/router.tsx`

- [ ] **Step 1: Write homepage interaction tests**

Require:

- hero communicates interval-level diagnosis in the first section;
- Analyze a video focuses or scrolls to Upload Lab;
- View demo selects the first centralized demo Finding;
- selecting a demo issue changes active timestamp, overlay, and detail;
- every demo surface includes `INTERACTIVE DEMO`;
- optional AI/OCR topics include `OPTIONAL` or `DEMO`;
- no `Overall Score` or equivalent Chinese claim appears;
- all major media roles are unique;
- `what912` stays visible after switching locale.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/features/home/HomePage.test.tsx
```

- [ ] **Step 3: Implement the hero and product proof window**

Use the local optical hero video behind the headline and the local city video
inside the product window. On initial load, animate only the scan line, staged
Finding markers, and review-interval count. Use reduced-motion posters when
requested.

- [ ] **Step 4: Implement the product narrative**

Build:

- Upload Lab with liquid macro atmosphere;
- sticky diagnostic narrative with the fashion shot;
- metrics spectrum with detector-local values;
- three distinct evidence/editorial images;
- synchronized compare preview with the two landscape videos;
- Upload → Analyze → Inspect → Improve;
- open-source detector protocol and JSON example with a copy button;
- final compact upload CTA.

Use IntersectionObserver to select narrative steps without hijacking scrolling.
Mobile uses tabs/cards and no sticky dependency.

- [ ] **Step 5: Verify accessibility and responsive behavior**

Test at 360, 768, 1280, and 1600 CSS-pixel widths using browser automation.
Confirm no horizontal scroll, all CTA controls work, focus remains visible,
and decorative video has no audio.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src/features/home site/src/app/router.tsx
git commit -m "feat(site): add Video Observatory product homepage"
```

## Task 10: Implement detector-by-detector video comparison

**Files:**

- Add all files under `site/src/features/compare/`
- Modify: `site/src/app/router.tsx`

- [ ] **Step 1: Write comparison math and synchronization tests**

`compareReports(a, b)` must report each detector independently:

```ts
interface DetectorDifference {
  detectorId: DetectorId;
  aEventCount: number | null;
  bEventCount: number | null;
  aDurationSeconds: number | null;
  bDurationSeconds: number | null;
  observation: "a_fewer" | "b_fewer" | "equal" | "unknown";
}
```

Test missing detectors as `unknown`, not zero. Test equal and unequal durations,
normalized timeline mode, swap A/B, sync on/off, and shared seeking.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/features/compare
```

- [ ] **Step 3: Implement two input paths**

Allow two local files or two compatible browser reports. Local files use the
same `BrowserAnalysisService`; do not duplicate detection logic. Retain two
session object URLs only while the page is active.

- [ ] **Step 4: Implement comparison UI**

Provide side-by-side players, synchronized controls, frame-step approximation,
swap, aligned detector timelines, detector difference table, evidence pairs,
and neutral observation copy. Do not declare one video universally better.

- [ ] **Step 5: Verify**

```powershell
npm run check
```

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add site/src/features/compare site/src/app/router.tsx
git commit -m "feat(site): add local detector comparison"
```

## Task 11: Implement local report, Creator/Research views, print, and JSON export

**Files:**

- Add all files under `site/src/features/report/`
- Modify: `site/src/styles/print.css`
- Modify: `site/src/app/router.tsx`

- [ ] **Step 1: Write report route and export tests**

Cover:

- local report lookup;
- explicit demo report lookup;
- missing report;
- Creator/Research switching;
- detector error display;
- HTML-sensitive strings rendered as text;
- UTF-8 JSON export;
- print button calls `window.print`;
- report never says “PDF export”; it says “Print / Save as PDF”;
- no absolute path or original media blob in export.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/features/report
```

- [ ] **Step 3: Implement report views**

Creator View emphasizes observable intervals, severity, limitations, and
review order. Research View adds detector IDs/versions, parameters,
configuration, confidence, raw summaries, runtime, warnings, and schema
version. Both consume the same typed report.

- [ ] **Step 4: Implement print and JSON export**

Use Blob download with `application/json;charset=utf-8`. Print styles hide
navigation and controls, expand Finding details, preserve evidence aspect
ratios, and avoid splitting a Finding across pages when possible.

- [ ] **Step 5: Verify**

```powershell
npm run check
```

- [ ] **Step 6: Record the review checkpoint**

```powershell
git add site/src/features/report site/src/styles/print.css site/src/app/router.tsx
git commit -m "feat(site): add share-ready local report views"
```

## Task 12: Add optional Supabase registration and login without blocking anonymous use

**Files:**

- Add: `site/.env.example`
- Add all files under `site/src/services/auth/`
- Add all files under `site/src/features/auth/`
- Add: `site/src/types/auth.ts`
- Add: `supabase/migrations/202607290001_public_site_auth_and_reports.sql`
- Add: `supabase/README.md`
- Modify: `site/src/app/AppProviders.tsx`
- Modify: `site/src/app/router.tsx`
- Modify: `site/src/components/layout/Header.tsx`

- [ ] **Step 1: Write the auth contract tests with a fake client**

Define:

```ts
export interface AuthClient {
  getSession(): Promise<AuthSession | null>;
  onSessionChange(callback: (session: AuthSession | null) => void): () => void;
  signInWithMagicLink(email: string, redirectTo: string): Promise<void>;
  signInWithGitHub(redirectTo: string): Promise<void>;
  completeCallback(url: URL): Promise<void>;
  signOut(): Promise<void>;
}
```

Test unavailable configuration, anonymous startup, restored session, magic
link request, GitHub redirect, callback completion, sign-out, localized error,
and that anonymous Analyze remains enabled in every state.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/features/auth
```

- [ ] **Step 3: Implement the unavailable adapter**

If either `VITE_SUPABASE_URL` or `VITE_SUPABASE_ANON_KEY` is missing, return
`UnavailableAuthClient`. The sign-in dialog must explain configuration is not
available; no Supabase module initialization or network call occurs.

- [ ] **Step 4: Implement the Supabase adapter**

Use only the URL and anonymous key. Support email magic link and GitHub OAuth.
Compute the callback with base-aware code:

```ts
const callbackUrl = new URL(
  `${import.meta.env.BASE_URL}auth/callback`,
  window.location.origin,
).toString();
```

This keeps development and GitHub Pages on the same path logic. Do not use or
document a service-role key in the browser.

- [ ] **Step 5: Add RLS migration and setup guide**

Create user-owned `profiles` and `report_index` tables. Enable RLS and require:

```sql
using (auth.uid() = user_id)
with check (auth.uid() = user_id)
```

Document exact Supabase redirect URLs:

```text
http://localhost:5173/VideoScope/auth/callback
https://what912.github.io/VideoScope/auth/callback
```

Document the GitHub OAuth app callback shown by Supabase. Account deletion is
an instruction/contact flow unless a privileged server function is later
deployed; do not expose privileged deletion from the static client.

- [ ] **Step 6: Verify offline behavior**

```powershell
Remove-Item Env:VITE_SUPABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:VITE_SUPABASE_ANON_KEY -ErrorAction SilentlyContinue
npm run check
```

Expected: the full site and anonymous analysis build and test successfully.

- [ ] **Step 7: Manually verify real providers after configuration**

This requires the user's Supabase project and GitHub OAuth configuration.
Verify email magic link, GitHub login, refresh/session restoration, sign-out,
and anonymous analysis in a signed-out private window. Record as unverified
until these real credentials exist.

- [ ] **Step 8: Record the review checkpoint**

```powershell
git add site supabase
git commit -m "feat(site): add optional Supabase authentication"
```

## Task 13: Add explicit sanitized sharing while keeping it disabled by default

**Files:**

- Add all files under `site/src/services/share/`
- Add or modify: `site/src/features/report/ShareDialog.tsx`
- Modify: `supabase/migrations/202607290001_public_site_auth_and_reports.sql`
- Modify: `supabase/README.md`

- [ ] **Step 1: Write sanitization and consent tests**

`sanitizeReportForShare` must remove:

- original filename unless the user supplies a report title;
- prompt unless separately opted in;
- absolute paths;
- object/data URLs;
- runtime cache fields;
- evidence not explicitly selected.

Test that clicking “Create share link” without the final consent checkbox does
not call the share client.

- [ ] **Step 2: Run and confirm failure**

```powershell
npx vitest run src/services/share src/features/report/ShareDialog.test.tsx
```

- [ ] **Step 3: Implement unavailable-by-default sharing**

Require `VITE_SUPABASE_SHARE_ENABLED=true`, a configured auth client, and an
authenticated session. Otherwise show “Not configured” with no network call.
The consent dialog lists the exact report fields and selected evidence that
will leave the device.

- [ ] **Step 4: Implement the sanitized JSON share adapter**

Insert only the sanitized report into a `shared_reports` table with a random
public ID, owner ID, created time, optional expiry, and revoked time. Do not
upload the original video. Keep evidence-image upload disabled until storage
policies receive a separate security review.

- [ ] **Step 5: Add RLS policies**

Owners can create, read, and revoke their own records. Anonymous readers can
select only non-revoked, non-expired records by public ID through the intended
view/RPC. No listing endpoint exposes all public IDs.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src/services/share site/src/features/report/ShareDialog.tsx supabase
git commit -m "feat(site): add consent-based sanitized report sharing"
```

## Task 14: Finish privacy, docs, error states, accessibility, and performance

**Files:**

- Add: `site/src/features/static/PrivacyPage.tsx`
- Add: `site/src/features/static/DocsPage.tsx`
- Add: `site/src/features/static/NotFoundPage.tsx`
- Add: `site/src/app/no-remote-runtime-assets.test.ts`
- Modify relevant feature/component CSS and tests
- Modify: `site/src/app/router.tsx`

- [ ] **Step 1: Add static-page and security tests**

The production source/build test must reject:

```ts
const forbidden = [
  "fonts.googleapis.com",
  "fonts.gstatic.com",
  "third-party-media-host.invalid",
  "http://",
];
```

Allow only documented HTTPS links in anchor tags and optional Supabase origins
from runtime configuration. Assert no `dangerouslySetInnerHTML` in product
source and no raw error stack rendering.

- [ ] **Step 2: Add complete error and empty-state coverage**

Implement the design-spec states for missing file, unsupported media, decode
failure, duration unavailable, file too large, canvas unavailable, memory
pressure/sample cap, CORS failure, cancellation, detector failure, no
Findings, missing/revoked report, unavailable auth, and offline optional
services.

- [ ] **Step 3: Implement Privacy and Docs routes**

Privacy must explain local-only video handling, IndexedDB contents, direct URL
network disclosure, optional auth data, optional sanitized share data, and
delete-all controls. Docs must distinguish browser preview from desktop
FFmpeg, Benchmark, AI, OCR, and Web API capabilities.

- [ ] **Step 4: Add accessibility validation**

Use `@axe-core/react` or `vitest-axe` in focused tests for Header, Upload Lab,
Workspace, Compare, Report, and dialogs. Manually verify keyboard-only
operation, focus restoration, timeline labels, severity text, evidence alt
text, and reduced motion.

- [ ] **Step 5: Enforce performance behavior**

Code-split workspace, compare, report, and auth routes. Preload only the hero
poster/metadata. Lazy-load below-fold media, pause it offscreen, cap thumbnails,
and ensure no duplicate decoded-frame arrays. Run a production bundle-size
report and set measured budgets in `verify-media.mjs`.

- [ ] **Step 6: Verify**

```powershell
npm run check
```

- [ ] **Step 7: Record the review checkpoint**

```powershell
git add site/src
git commit -m "feat(site): complete public-site privacy and accessibility"
```

## Task 15: Add GitHub Pages routing and deployment

**Files:**

- Add: `site/public/404.html`
- Add: `site/public/robots.txt`
- Add: `site/public/site.webmanifest`
- Add: `.github/workflows/pages.yml`
- Modify: `site/index.html`
- Modify: `site/vite.config.ts`
- Modify: `site/src/main.tsx`
- Add or modify: `site/src/app/router.test.tsx`

- [ ] **Step 1: Write the base-path and redirect tests**

Test:

- generated asset URLs start with `/VideoScope/`;
- the 404 shim stores the requested route and redirects to the app base;
- `main.tsx` restores the route before router creation;
- `/VideoScope/report/demo` resolves after restoration;
- query and hash survive round-trip;
- redirect inputs cannot escape the same origin/base.

- [ ] **Step 2: Implement the GitHub Pages 404 shim**

Use a same-origin session-storage payload rather than interpolating untrusted
HTML. On boot, validate the stored pathname begins with `/VideoScope/`, restore
it through `history.replaceState`, then delete the payload.

- [ ] **Step 3: Add the Pages workflow**

Trigger on pushes to `main` and manual dispatch. Configure:

```yaml
permissions:
  contents: read
  pages: write
  id-token: write
```

Run Node 22, `npm ci`, `npm run lint`, `npm run typecheck`, `npm test`,
`npm run media:verify`, and `npm run build` in `site/`; upload `site/dist`
using `actions/upload-pages-artifact`; deploy with `actions/deploy-pages`.
Do not run the Python release workflow, publish packages, or download AI
models.

- [ ] **Step 4: Verify the production artifact locally**

```powershell
npm run check
npm run media:verify
npx vite preview --host 127.0.0.1
```

In a signed-out browser, open:

```text
http://127.0.0.1:4173/VideoScope/
http://127.0.0.1:4173/VideoScope/workspace
http://127.0.0.1:4173/VideoScope/compare
http://127.0.0.1:4173/VideoScope/report/demo
```

- [ ] **Step 5: Record the review checkpoint**

```powershell
git add .github/workflows/pages.yml site
git commit -m "ci(site): deploy public app to GitHub Pages"
```

## Task 16: Update repository documentation and perform final acceptance

**Files:**

- Modify: `README.md`
- Modify: `docs/frontend.md`
- Modify: `docs/architecture.md`
- Modify: `docs/decisions/0001-local-first.md`
- Add: `docs/public-site.md`
- Add: `docs/public-site-release-checklist.md`
- Add screenshots: `docs/assets/public-home.png`
- Add screenshots: `docs/assets/public-workspace.png`
- Add screenshots: `docs/assets/public-compare.png`
- Add screenshots: `docs/assets/public-report.png`

- [ ] **Step 1: Update documentation truthfully**

Document:

- public URL and source repository;
- anonymous local browser analysis;
- four browser CPU detectors;
- browser versus desktop differences;
- English/Chinese switch;
- `what912` creator mark;
- local development and tests;
- media preparation/license audit;
- optional Supabase configuration;
- report/share privacy;
- GitHub Pages deployment;
- known codec/CORS/calibration limitations.

Do not claim an overall score, PDF generator, AI browser analysis, model
accuracy, or successful production auth until verified.

- [ ] **Step 2: Run complete automated validation**

```powershell
cd site
npm ci
npm run check
npm run media:verify
cd ..
.\.venv\Scripts\python.exe scripts\validate.py
python -m build
```

Expected:

- site lint, strict TypeScript, Vitest, asset audit, and production build pass;
- repository Ruff, format, mypy, and pytest pass;
- Python wheel/sdist still build;
- no model download or network call occurs during base validation.

- [ ] **Step 3: Run fixture-based browser acceptance**

Generate fixtures locally:

```powershell
.\.venv\Scripts\python.exe scripts\generate_test_videos.py --force
```

In Firefox and Chromium, analyze `clean_motion.mp4`,
`black_segment.mp4`, `freeze_segment.mp4`, `blur_segment.mp4`, and
`flicker_segment.mp4`. Record actual Finding intervals without changing the
fixture manifest. Confirm:

- clean motion has no high-severity Finding;
- each positive fixture exposes its expected detector interval within the
  documented tolerance;
- selecting a Finding seeks player, timeline, overlay, detail, evidence, and
  metric cursor together;
- cancelling frees the session;
- no detector failure is presented as “no issue.”

- [ ] **Step 4: Run responsive and privacy acceptance**

At desktop, tablet, and mobile widths:

- no horizontal overflow;
- keyboard navigation works;
- reduced motion uses posters;
- language persists and all strings translate;
- `what912` stays literal;
- anonymous analysis works with Supabase variables absent;
- network panel shows no upload during local file analysis;
- JSON contains no absolute path, original media, or demo content.

- [ ] **Step 5: Capture real local screenshots**

Capture the homepage, a real fixture workspace result, compare, and report.
Save them under `docs/assets/`. Do not fabricate a detector result or use the
marketing demo screenshot as evidence of real analysis.

- [ ] **Step 6: Complete external configuration gates**

Before production auth is marked passed:

1. create/configure the Supabase project;
2. apply the checked-in migration;
3. set allowed GitHub Pages callback URLs;
4. configure email magic link and GitHub OAuth;
5. add GitHub Actions variables;
6. verify row-level policies with two separate test accounts;
7. verify share revocation if sharing is enabled.

If these are not available, ship anonymous analysis with sign-in/share shown
as unavailable and list auth/share under “not yet configured.”

- [ ] **Step 7: Publish only after explicit user authorization**

With the public `what912/VideoScope` repository created and Pages enabled:

```powershell
git remote add origin https://github.com/what912/VideoScope.git
git push -u origin main
```

Wait for the Pages workflow, then verify in a signed-out browser:

```text
https://what912.github.io/VideoScope/
https://what912.github.io/VideoScope/workspace
https://what912.github.io/VideoScope/compare
https://what912.github.io/VideoScope/report/demo
```

Do not create a GitHub Release or publish PyPI as part of this site deployment.

- [ ] **Step 8: Record the final review checkpoint**

```powershell
git add README.md docs
git commit -m "docs: document public VideoScope product site"
```

## Final Self-Review Checklist

- [ ] Every acceptance criterion in the approved design spec maps to a task or
      an explicitly named external configuration gate.
- [ ] No task introduces a universal or overall quality score.
- [ ] No real report can import centralized demo Findings.
- [ ] Every external-media role has a distinct local file and license record.
- [ ] Browser-local analysis remains usable without auth, network, GPU, or
      model downloads.
- [ ] Optional auth has an unavailable adapter and fake-client tests.
- [ ] Sharing is explicit, sanitized, revocable, and disabled unless configured.
- [ ] Direct URL import shows a network/privacy warning before the request.
- [ ] Detector failures remain visible and do not erase successful results.
- [ ] All thresholds live in typed configuration.
- [ ] All translated keys exist in both dictionaries.
- [ ] `what912` remains a non-translated literal.
- [ ] GitHub Pages base paths and direct refresh are tested.
- [ ] `rg -n "TODO|TBD|placeholder|implement later|dangerouslySetInnerHTML" site/src docs/public-site.md`
      returns no unresolved implementation placeholders or unsafe HTML use.
- [ ] `rg -n "Overall Score|综合评分|总质量分" site/src` returns no product UI claim.
- [ ] Base automated validation performs no remote request.
- [ ] Actual test/build outputs and external blockers are reported without
      embellishment.
