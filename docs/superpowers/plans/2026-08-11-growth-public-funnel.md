# Result-Led Public Growth Funnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public GitHub Pages site and README explain, prove, and start the Video Rescue → Publish Ready journey within one screen.

**Architecture:** New lazy-loaded result-led routes consume the validated case manifest from the case-foundation plan. The homepage promotes one real generated comparison and one primary action; support pages route creators to Rescue/download and developers to stable schemas without changing local processing or requiring signup.

**Tech Stack:** React 19, TypeScript, React Router 8, Vite, CSS variables, existing i18n provider, Vitest, Testing Library.

## Global Constraints

- Public route base remains `/VideoScope/`; hard refresh of every route must work on GitHub Pages.
- The primary message is “Rescue a problematic video. Export a verified, publish-ready copy.”
- Chinese primary copy is “视频打不开、格式不兼容、音画异常或观看效果较差？拖入视频，先诊断，再生成一个经过验证、可以继续发布的新副本。”
- Attribute every page with the fixed, untranslated string `Created by what912`.
- Do not claim cloud processing, universal restoration, guaranteed upload acceptance, or an uncalibrated overall score.
- The public site can run its existing lightweight browser CPU scan; complete Rescue/Publish processing uses the paired loopback connector.
- Anonymous visitors can view cases, download instructions, and begin analysis; optional sign-in cannot block trying the product.
- Use only checked-in project-authored assets from the validated manifests; no CDN, remote font, remote image, analytics, or video hotlink.
- All interactive controls require keyboard focus, visible focus state, labels, reduced-motion behavior, and non-color status text.
- English and Simplified Chinese dictionaries must remain structurally identical.

---

## File Map

- Create `site/src/features/growth/growth-copy.ts`: bilingual route copy typed from one English source.
- Create `site/src/features/growth/growth.css`: shared result-led page layouts.
- Create `site/src/features/growth/RescueLandingPage.tsx`: zero-beginner journey and connector status.
- Create `site/src/features/growth/ExamplesPage.tsx`: filterable validated case library.
- Create `site/src/features/growth/CaseStudyPage.tsx`: same-range evidence, exact actions, limitations, reproduction.
- Create `site/src/features/growth/DownloadPage.tsx`: official release, hash, FFmpeg, publisher-warning, uninstall guidance.
- Create `site/src/features/growth/DevelopersPage.tsx`: CLI/schema/benchmark/plugin contribution paths.
- Create `site/src/features/growth/RoadmapPage.tsx`: shipped/validating/not-promised states.
- Create `site/src/features/growth/CommunityPage.tsx`: support, case authorization, contribution routes.
- Create focused tests beside each route.
- Modify `site/src/app/router.tsx`, `site/src/components/layout/Header.tsx`, and `site/src/components/layout/Footer.tsx`.
- Modify `site/src/features/home/HomePage.tsx`, `Hero.tsx`, `HomeUploadLab.tsx`, `FinalCta.tsx`, `home.css`, and home tests.
- Modify `site/src/i18n/en.ts`, `zh-CN.ts`, and dictionary parity tests.
- Modify `site/index.html` and create `site/public/404.html`, `site/public/sitemap.xml`, `site/public/robots.txt`.
- Create `scripts/generate_launch_media.py`, `tests/scripts/test_generate_launch_media.py`, and `docs/growth/launch-media-script.md`.
- Modify `README.md` and `site/scripts/public-release-docs.test.mjs`.
- Create `site/scripts/sync-case-readme.mjs` and `site/scripts/sync-case-readme.test.mjs` so README case proof is generated from the canonical manifest.
- Replace `site/public/og.png` with the generated 1280×640 project-authored comparison asset.

### Task 1: Add the result-led route skeleton and bilingual copy

**Files:**
- Create: `site/src/features/growth/growth-copy.ts`
- Create: `site/src/features/growth/growth.css`
- Create: route component files listed above
- Create: `site/src/features/growth/GrowthRoutes.test.tsx`
- Modify: `site/src/app/router.tsx`
- Modify: `site/src/components/layout/Header.tsx`
- Modify: `site/src/components/layout/Footer.tsx`
- Modify: `site/src/i18n/en.ts`
- Modify: `site/src/i18n/zh-CN.ts`

**Interfaces:**
- Consumes: `caseStudyManifest`, `findCaseStudy`, `useI18n`, and connector route `/connect`.
- Produces: routes `/rescue`, `/examples`, `/examples/:slug`, `/download`, `/developers`, `/roadmap`, and `/community`.

- [ ] **Step 1: Write failing route and attribution tests**

```tsx
it.each([
  "/rescue", "/examples", "/examples/timeline-rescue", "/download",
  "/developers", "/roadmap", "/community",
])("renders %s without authentication", async (path) => {
  render(<TestApp initialEntries={[path]} />);
  expect(await screen.findByRole("main")).toBeInTheDocument();
  expect(screen.getByText("Created by what912")).toBeInTheDocument();
  expect(screen.queryByText(/sign in to continue/iu)).not.toBeInTheDocument();
});

it("keeps attribution unchanged after locale switch", async () => {
  render(<TestApp initialEntries={["/rescue"]} />);
  await userEvent.selectOptions(screen.getByLabelText(/language|语言/iu), "zh-CN");
  expect(screen.getByText("Created by what912")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run the tests and confirm missing routes**

Run: `cd site; npm test -- src/features/growth/GrowthRoutes.test.tsx`

Expected: FAIL with the current not-found page.

- [ ] **Step 3: Add lazy route imports**

```tsx
const RescueLandingPage = lazy(async () => {
  const module = await import("../features/growth/RescueLandingPage");
  return { default: module.RescueLandingPage };
});
const ExamplesPage = lazy(async () => {
  const module = await import("../features/growth/ExamplesPage");
  return { default: module.ExamplesPage };
});
const CaseStudyPage = lazy(async () => {
  const module = await import("../features/growth/CaseStudyPage");
  return { default: module.CaseStudyPage };
});
const DownloadPage = lazy(async () => {
  const module = await import("../features/growth/DownloadPage");
  return { default: module.DownloadPage };
});
const DevelopersPage = lazy(async () => {
  const module = await import("../features/growth/DevelopersPage");
  return { default: module.DevelopersPage };
});
const RoadmapPage = lazy(async () => {
  const module = await import("../features/growth/RoadmapPage");
  return { default: module.RoadmapPage };
});
const CommunityPage = lazy(async () => {
  const module = await import("../features/growth/CommunityPage");
  return { default: module.CommunityPage };
});
```

Register the exact paths below the existing shell and keep the catch-all last.

- [ ] **Step 4: Define typed bilingual copy**

```ts
export const growthCopy = {
  en: {
    positioning: "Rescue a problematic video. Export a verified, publish-ready copy.",
    sourcePreserved: "Your source stays unchanged.",
    localBoundary: "Full processing runs in the paired connector on this computer.",
  },
  "zh-CN": {
    positioning: "抢救有问题的视频，导出经过验证的可发布新副本。",
    sourcePreserved: "源文件始终保持不变。",
    localBoundary: "完整处理在这台电脑已配对的本地连接器中运行。",
  },
} as const satisfies Record<Locale, GrowthCopy>;
```

Put stable product names, URLs, and `Created by what912` in constants outside translated fields.

- [ ] **Step 5: Update navigation hierarchy**

Desktop and mobile primary navigation order becomes Product (`/`), Rescue (`/rescue`), Examples, Download, Developers, Roadmap, Community, GitHub. Keep Compare and other modes reachable through the product/workspace, not as competing homepage primaries.

- [ ] **Step 6: Run route, parity, and accessibility tests**

Run: `cd site; npm test -- src/features/growth/GrowthRoutes.test.tsx src/i18n/dictionary-parity.test.ts`

Expected: PASS for both locales and every route.

- [ ] **Step 7: Commit the route foundation**

```powershell
git add site/src/app site/src/components/layout site/src/features/growth site/src/i18n
git commit -m "feat: add result-led public growth routes"
```

### Task 2: Rebuild the first screen around one verified outcome

**Files:**
- Modify: `site/src/features/home/HomePage.tsx`
- Modify: `site/src/features/home/Hero.tsx`
- Modify: `site/src/features/home/HomeUploadLab.tsx`
- Modify: `site/src/features/home/FinalCta.tsx`
- Modify: `site/src/features/home/home.css`
- Modify: `site/src/features/home/HomePage.test.tsx`
- Modify: `site/src/features/home/HomeUploadLab.test.tsx`
- Create: `site/src/features/home/FeaturedCaseComparison.tsx`
- Create: `site/src/features/home/FeaturedCaseComparison.test.tsx`

**Interfaces:**
- Consumes: `featuredCaseStudies[0]` and its manifest-bound media.
- Produces: one homepage primary link to `/rescue`, secondary links to `/examples` and `/developers`, and an immediate browser-scan fallback.

- [ ] **Step 1: Write failing content-order and interaction tests**

```tsx
it("shows verified proof before feature detail", () => {
  renderHome();
  const proof = screen.getByTestId("featured-case-comparison");
  const modes = screen.getByTestId("home-upload-lab");
  expect(proof.compareDocumentPosition(modes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("uses one creator-facing primary action", () => {
  renderHome();
  const primary = screen.getByRole("link", { name: /rescue.*publish|抢救.*发布/iu });
  expect(primary).toHaveAttribute("href", "/rescue");
});

it("synchronizes the before and after video positions", async () => {
  render(<FeaturedCaseComparison item={featuredCaseStudies[0]} />);
  fireEvent.timeUpdate(screen.getByLabelText(/before|处理前/iu), { target: { currentTime: 4.2 } });
  expect(screen.getByLabelText(/after|处理后/iu).currentTime).toBeCloseTo(4.2, 1);
});
```

- [ ] **Step 2: Run the tests and confirm current marketing-first order fails**

Run: `cd site; npm test -- src/features/home/HomePage.test.tsx src/features/home/FeaturedCaseComparison.test.tsx`

Expected: FAIL because the featured case component and `/rescue` primary do not exist.

- [ ] **Step 3: Implement same-range featured comparison**

```tsx
export function FeaturedCaseComparison({ item }: { item: CaseStudy }) {
  const before = useRef<HTMLVideoElement>(null);
  const after = useRef<HTMLVideoElement>(null);
  const sync = (source: HTMLVideoElement, target: HTMLVideoElement | null) => {
    if (target && Math.abs(target.currentTime - source.currentTime) > 0.08) {
      target.currentTime = source.currentTime;
    }
  };
  return <section data-testid="featured-case-comparison">{/* two labelled videos, range, actions, verification, limitations */}</section>;
}
```

Use a shared play/pause control and an explicit “project-authored demonstration” label. Do not autoplay with sound. Reduced-motion mode disables automatic scanning decoration.

- [ ] **Step 4: Enforce the homepage section order**

Render exactly:

```text
Hero positioning + primary action
Featured same-range before/after proof
Three-step Rescue → review → Publish Ready flow
Local/privacy/source-preserved boundary
Three validated cases
Windows download
Developer entry
Created by what912
Optional GitHub Star link
```

Move detailed detector metrics and A/D/B/C descriptions below the creator journey or link them from Developers/Product.

- [ ] **Step 5: Keep immediate browser analysis usable**

The `/rescue` primary explains that full processing needs the local connector. Keep a secondary “Run a quick browser check” action that scrolls to the existing `HomeUploadLab`; do not present it as full Rescue.

- [ ] **Step 6: Run focused home tests**

Run: `cd site; npm test -- src/features/home`

Expected: PASS in English and Chinese, with keyboard controls and no duplicate primary CTA.

- [ ] **Step 7: Commit the result-led homepage**

```powershell
git add site/src/features/home site/src/i18n
git commit -m "feat: lead homepage with verified rescue outcome"
```

### Task 3: Implement the case library and evidence pages

**Files:**
- Modify: `site/src/features/growth/ExamplesPage.tsx`
- Modify: `site/src/features/growth/CaseStudyPage.tsx`
- Create: `site/src/features/growth/CaseComparison.tsx`
- Create: `site/src/features/growth/CaseFilters.tsx`
- Test: `site/src/features/growth/ExamplesPage.test.tsx`
- Test: `site/src/features/growth/CaseStudyPage.test.tsx`

**Interfaces:**
- Consumes: validated `CaseStudy` records only.
- Produces: provenance/status filters, stable case URLs, same-range playback, and reproduction copy action.

- [ ] **Step 1: Write failing evidence-integrity tests**

```tsx
it("renders exact actions, limitations, and verification separately", async () => {
  renderRoute("/examples/measured-viewing-improvement");
  expect(await screen.findByRole("heading", { name: /actions|执行动作/iu })).toBeVisible();
  expect(screen.getByRole("heading", { name: /limitations|限制/iu })).toBeVisible();
  expect(screen.getByText(/project-authored|项目原创/iu)).toBeVisible();
});

it("returns the normal not-found page for an unknown case slug", async () => {
  renderRoute("/examples/not-a-case");
  expect(await screen.findByRole("heading", { name: /not found|未找到/iu })).toBeVisible();
});
```

- [ ] **Step 2: Run the tests and confirm missing evidence UI**

Run: `cd site; npm test -- src/features/growth/ExamplesPage.test.tsx src/features/growth/CaseStudyPage.test.tsx`

Expected: FAIL.

- [ ] **Step 3: Implement filterable case cards**

Use URL search parameter `provenance` with values `all`, `project-authored`, `user-authorized`, `synthetic-regression`. Each card shows title, symptom, outcome status text, provenance, comparison duration, and limitations count. Never show synthetic regression under a “user results” label.

- [ ] **Step 4: Implement the case detail hierarchy**

```tsx
<CaseComparison item={item} />
<CaseFacts versions={item.versions} verification={item.verification} />
<ActionList actions={item.actions} />
<LocalizedList heading={copy.unresolved} items={item.unresolved} />
<LocalizedList heading={copy.limitations} items={item.limitations} />
<ReproductionSteps steps={item.reproduction} />
```

The comparison component clamps seeking to the declared range and keeps both players within 100 ms of each other. Copying reproduction commands copies only the manifest strings.

- [ ] **Step 5: Run case tests**

Run: `cd site; npm test -- src/features/growth/ExamplesPage.test.tsx src/features/growth/CaseStudyPage.test.tsx`

Expected: PASS for all three cases and unknown slugs.

- [ ] **Step 6: Commit the public evidence library**

```powershell
git add site/src/features/growth
git commit -m "feat: publish reproducible case evidence library"
```

### Task 4: Complete creator and developer support pages

**Files:**
- Modify: `site/src/features/growth/RescueLandingPage.tsx`
- Modify: `site/src/features/growth/DownloadPage.tsx`
- Modify: `site/src/features/growth/DevelopersPage.tsx`
- Modify: `site/src/features/growth/RoadmapPage.tsx`
- Modify: `site/src/features/growth/CommunityPage.tsx`
- Create: `site/src/features/growth/SupportPages.test.tsx`
- Modify: `site/src/config/connector-install.ts`

**Interfaces:**
- Consumes: official repository/release constants and existing connector status client.
- Produces: zero-beginner steps, exact boundary text, three sub-ten-minute contribution entry points.

- [ ] **Step 1: Write failing link and boundary tests**

```tsx
it("never invents a release asset when none is configured", () => {
  renderRoute("/download", { releaseAsset: null });
  expect(screen.getByText(/not yet available|尚未提供/iu)).toBeVisible();
  expect(screen.queryByRole("link", { name: /download windows|下载 Windows/iu })).not.toBeInTheDocument();
});

it("shows source preserved and verification limits on rescue", () => {
  renderRoute("/rescue");
  expect(screen.getByText(/source.*unchanged|源文件.*不变/iu)).toBeVisible();
  expect(screen.getByText(/not.*guarantee|不保证/iu)).toBeVisible();
});
```

- [ ] **Step 2: Run the test and confirm support pages lack the contract**

Run: `cd site; npm test -- src/features/growth/SupportPages.test.tsx`

Expected: FAIL.

- [ ] **Step 3: Implement `/rescue` as a zero-beginner decision page**

Ask one question with four symptom choices, map them to existing Rescue symptom hints, then show five ordered steps: install connector, start/pair, choose local video, review evidence/plan, confirm Rescue then Publish Ready. Provide “Check connector” and `/connect` buttons. Symptom choices must not auto-confirm a plan.

- [ ] **Step 4: Implement `/download` without false availability**

Render the configured v0.8 release asset only when `connector-install.ts` includes URL, filename, and SHA-256. Include unsigned-publisher explanation, FFmpeg check, loopback-only statement, uninstall steps, and source fallback. All URLs must use `https://github.com/what912/VideoScope`.

- [ ] **Step 5: Implement developer, roadmap, and community pages**

Developer quick contributions are exactly: improve a detector test, improve a translation, submit a sanitized reproduction/case metadata record. Roadmap groups capabilities into `Shipped`, `Validating`, and `Not promised`; the last group includes universal restoration, identity recognition, and guaranteed platform acceptance. Community links to Discussions, issues, security policy, contribution guide, and the case authorization form without requiring Star or signup for product use.

- [ ] **Step 6: Run support-page tests**

Run: `cd site; npm test -- src/features/growth/SupportPages.test.tsx`

Expected: PASS with exact valid URLs and no false download link.

- [ ] **Step 7: Commit support pages**

```powershell
git add site/src/features/growth site/src/config/connector-install.ts
git commit -m "docs: add creator and developer growth paths"
```

### Task 5: Produce the 30-second proof and 3-minute zero-beginner guide

**Files:**
- Create: `scripts/generate_launch_media.py`
- Create: `tests/scripts/test_generate_launch_media.py`
- Create: `docs/growth/launch-media-script.md`
- Create: `docs/growth/captions/rescue-30s.en.srt`
- Create: `docs/growth/captions/rescue-30s.zh-CN.srt`
- Create: `docs/growth/captions/windows-3min.en.srt`
- Create: `docs/growth/captions/windows-3min.zh-CN.srt`

**Interfaces:**
- Consumes: validated case manifest/media plus reviewed local screenshots from the current public site and connector.
- Produces: local-only `runs/growth-media/videoscope-rescue-30s.mp4` and `runs/growth-media/videoscope-windows-3min.mp4` with matching SHA-256 summaries.

- [ ] **Step 1: Write failing timeline and command tests**

```python
def test_launch_timelines_have_exact_duration_and_sources() -> None:
    short, tutorial = build_launch_timelines(load_case_manifest())
    assert short.duration_seconds == 30
    assert tutorial.duration_seconds == 180
    assert {segment.case_slug for segment in short.case_segments} == {
        "timeline-rescue",
        "measured-viewing-improvement",
        "no-crop-vertical-publish",
    }


def test_render_uses_ffmpeg_argument_arrays(fake_runner, tmp_path: Path) -> None:
    render_launch_media(valid_timeline(), tmp_path, runner=fake_runner)
    assert fake_runner.calls
    assert all(
        call.shell is False and isinstance(call.argv, list)
        for call in fake_runner.calls
    )
```

- [ ] **Step 2: Run tests and confirm the generator is missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/scripts/test_generate_launch_media.py -q`

Expected: FAIL.

- [ ] **Step 3: Define the reviewed 30-second timeline**

Use exact seconds:

```text
00–03  VideoScope + “Rescue a problematic video”
03–09  timeline-rescue same-range before/after
09–15  measured improvement same-range before/after + limitation label
15–21  no-crop vertical Publish Ready comparison
21–26  Select → diagnose → review → confirm → verify
26–30  GitHub Pages URL, Windows download, Local-first, Created by what912
```

No segment may show a success state not present in the case manifest.

- [ ] **Step 4: Define the reviewed 3-minute tutorial timeline**

Use exact sections:

```text
00–15  problem and local-first boundary
15–45  official Windows download, checksum, unsigned-publisher explanation
45–75  install, FFmpeg/ffprobe status, start and pair connector
75–105 choose problem and local video; source remains unchanged
105–135 review damage evidence, same-range preview, limitations, exact Rescue plan
135–160 confirm Rescue and play the verified result
160–175 continue into Publish Ready, choose profile, review and confirm
175–180 download, delete local job, GitHub/examples links, Created by what912
```

The guide must show a visible `needs_review is not completion` callout and a no-cloud-upload statement.

- [ ] **Step 5: Implement deterministic composition**

The generator reads only allowlisted case assets and explicit screenshot paths, builds FFmpeg filter graphs as argument values, maps local bilingual SRT tracks, uses H.264/AAC MP4 at 1280×720 and 30 fps, removes metadata, and writes atomically below `runs/growth-media`. Missing reviewed screenshots cause a clear failure and stop rendering without substituting unrelated media.

- [ ] **Step 6: Validate captions and render locally**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/scripts/test_generate_launch_media.py -q`

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe scripts/generate_launch_media.py --output runs/growth-media --force`

Run: `ffprobe -v error -show_streams -show_format -of json runs/growth-media/videoscope-rescue-30s.mp4`

Run: `ffprobe -v error -show_streams -show_format -of json runs/growth-media/videoscope-windows-3min.mp4`

Expected: exact 30/180-second outputs within one frame, no external URL, two local subtitle files per video, and checksums printed. Generated videos remain uncommitted until a separate asset-publication review.

- [ ] **Step 7: Commit source scripts and captions, not rendered videos**

```powershell
git add scripts/generate_launch_media.py tests/scripts/test_generate_launch_media.py docs/growth/launch-media-script.md docs/growth/captions
git commit -m "docs: add reproducible launch media production"
```

### Task 6: Align README, social metadata, and GitHub Pages fallbacks

**Files:**
- Modify: `README.md`
- Modify: `site/index.html`
- Create: `site/public/404.html`
- Create: `site/public/sitemap.xml`
- Create: `site/public/robots.txt`
- Replace: `site/public/og.png`
- Modify: `site/scripts/public-release-docs.test.mjs`
- Create: `site/scripts/sync-case-readme.mjs`
- Create: `site/scripts/sync-case-readme.test.mjs`
- Modify: `site/scripts/build-safety.mjs`
- Modify: `site/scripts/build-safety.test.mjs`

**Interfaces:**
- Consumes: the featured case and official release configuration.
- Produces: README first fold, Open Graph metadata, crawlable routes, and SPA refresh recovery.

- [ ] **Step 1: Write failing release-document tests**

```js
it("keeps the README first screen focused on Rescue then Publish Ready", async () => {
  const readme = await readFile(readmePath, "utf8");
  const firstScreen = readme.split("\n").slice(0, 45).join("\n");
  expect(firstScreen).toContain("Rescue a problematic video");
  expect(firstScreen).toContain("Created by what912");
  expect(firstScreen).toContain("/VideoScope/examples");
  expect(firstScreen).not.toMatch(/100%|guaranteed|万能修复/iu);
});

it("uses a 1280 by 640 project-authored social preview", async () => {
  expect(await imageSize(ogPath)).toEqual({ width: 1280, height: 640 });
});

it("renders README case links from completed featured manifest records", async () => {
  const rendered = renderCaseReadme(validManifest());
  expect(rendered).toContain("/VideoScope/examples/timeline-rescue");
  expect(rendered).not.toContain("needs-review-case");
});
```

- [ ] **Step 2: Run the tests and confirm the existing first fold fails**

Run: `cd site; npm test -- scripts/public-release-docs.test.mjs scripts/build-safety.test.mjs`

Expected: FAIL until the README and metadata are updated.

- [ ] **Step 3: Rewrite the README first fold**

Use this exact order:

```markdown
# VideoScope
**Rescue a problematic video. Export a verified, publish-ready copy.**

[project-authored same-range comparison]

[Download for Windows] [Open Web App] [View Examples]

Local-first · Source preserved · CPU available · Optional BYOK AI
Created by what912
```

Follow with three-minute setup, real case evidence, supported/not-supported conditions, complete A/D/B/C workflows, privacy/security, developer architecture, benchmark, roadmap, and contribution guidance.

Wrap the case-proof block in `<!-- VIDEOSCOPE_CASES_START -->` and `<!-- VIDEOSCOPE_CASES_END -->`. `sync-case-readme.mjs` reads `site/src/data/case-studies.json`, renders only `featured && completed` records in stable manifest order, and refuses missing markers or an empty featured set. Its `--check` mode compares generated text without writing; `--write` performs an atomic README replacement. This keeps README, public pages, launch media, and social preview bound to the same manifest.

```js
export function renderCaseReadme(manifest) {
  return manifest.cases
    .filter((item) => item.featured && item.verification.status === "completed")
    .map((item) => `- [${item.title.en}](https://what912.github.io/VideoScope/examples/${item.slug})`)
    .join("\n");
}
```

- [ ] **Step 4: Add static metadata and route fallback**

`index.html` includes canonical URL, bilingual-neutral description, local `og.png`, and no remote script. `404.html` preserves `location.pathname + search + hash`, redirects to `/VideoScope/?route=...`, and the app restores the internal route once; tests must reject open redirects and external origins.

- [ ] **Step 5: Generate the social preview from project assets**

Use the existing deterministic media tooling to compose VideoScope wordmark, a same-range split comparison, `Local-first`, and `Created by what912`. Do not use third-party logos or claims of repaired detail not visible in the case.

- [ ] **Step 6: Run the complete site gate**

Run: `cd site; npm run check`

Run: `cd site; node scripts/sync-case-readme.mjs --check`

Expected: lint, typecheck, tests, media preparation, production build, runtime URL audit, bundle budget, and both media allowlists pass.

- [ ] **Step 7: Commit the public packaging surface**

```powershell
git add README.md site/index.html site/public site/scripts
git commit -m "docs: align public launch surface with rescue outcomes"
```

### Task 7: Run the public-funnel review gate

**Files:**
- Review: all files changed by Tasks 1–5.

**Interfaces:**
- Produces: a deployable but not yet deployed GitHub Pages artifact.

- [ ] **Step 1: Run the production preview locally**

Run: `cd site; npm run build`

Run: `cd site; npm run preview -- --host 127.0.0.1`

Expected: local preview binds loopback only. Visit `/VideoScope/`, every growth route, and a case detail by direct URL.

- [ ] **Step 2: Perform bilingual keyboard review**

In both locales, Tab through header, comparison controls, filters, CTAs, download instructions, and footer. Confirm visible focus, no scroll trap, no horizontal overflow at 360 px, and unchanged `Created by what912`.

- [ ] **Step 3: Audit remote references**

Run: `rg -n "https?://" site/src site/public site/dist`

Expected: only documented GitHub repository/release/community destinations; no CDN, analytics, remote font, remote media, or upload endpoint.

- [ ] **Step 4: Run repository validation**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts\validate.py
```

Expected: every gate passes. Do not deploy or push during this task.
