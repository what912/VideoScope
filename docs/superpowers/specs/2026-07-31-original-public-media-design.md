# Original public media generation design

Date: 2026-07-31
Status: approved for implementation
Scope: public VideoScope website media only

## 1. Purpose

The public site currently uses nine Mixkit-derived MP4/WebP assets. Publishing
those files in a public Git repository creates avoidable redistribution and
provenance risk. VideoScope will replace them with project-authored procedural
media while preserving the cinematic, varied, high-quality visual experience
of the public site.

The implementation must satisfy two independent release gates:

1. every published media asset has complete, auditable project-owned
   provenance; and
2. the generated media is visually suitable for a professional public product,
   not a collection of test patterns or placeholder graphics.

This design does not change the analysis algorithms, report schema, browser
privacy boundary, authentication boundary, or product claims.

## 2. Chosen approach

The repository will commit deterministic generation code and a declarative
scene manifest, but will not commit generated MP4/WebP files. GitHub Actions
will generate the assets with FFmpeg before the site test and build steps. The
same command will be used for local development and release verification.

This approach was selected over:

- CSS/SVG-only decoration, which minimizes artifacts but does not provide the
  requested variety and cinematic video presence; and
- committing generated media files, which leaves opaque binaries in public Git
  history and makes provenance review harder.

Generated media may be present in a local working directory and in the deployed
GitHub Pages artifact. It must be ignored by Git and recreated from source.

## 3. Media roles and visual direction

The existing nine roles remain stable so application components do not need a
media-specific rewrite. Each role must have a visibly distinct composition,
motion language, palette, and silhouette.

| Role | Original visual concept | Primary motion |
| --- | --- | --- |
| `hero` | Optical observatory aperture with cyan scan light and refracted rings | Slow orbital focus and scanning sweep |
| `product-proof` | Abstract night observation grid with luminous depth markers | Forward parallax and measured signal pulses |
| `upload-lab` | Layered fluid spectrum with restrained cyan/violet interference | Smooth laminar flow and upload-like convergence |
| `diagnosis` | Diagnostic mesh with a localized anomaly and tracking reticle | Grid deformation, lock-on, and evidence pulse |
| `compare-a` | Cool topographic field with stable layered contours | Horizontal drift with low-amplitude parallax |
| `compare-b` | Warm dawn spectrum with different geometry and rhythm | Rising light band and atmospheric expansion |
| `evidence-a` | Cyan optical caustic still | Static evidence composition |
| `evidence-b` | Violet structural lattice still | Static evidence composition |
| `evidence-c` | Amber diagnostic contour still | Static evidence composition |

No scene may contain people, recognizable property, trademarks, external
photography, copied artwork, remote fonts, or third-party visual assets. The
visual language must remain consistent with the existing Video Observatory
design system without making all roles look like variants of one background.

## 4. Quality standard

Video scenes will be generated from a native 1280x720 working canvas at 24 fps for
six to eight seconds, then encoded as browser-compatible H.264/yuv420p with a
high-quality constant-rate-factor setting. Posters will be extracted from a
deliberately selected composition frame and encoded as WebP.

Quality requirements:

- clean antialiasing through full-resolution rendering and controlled
  downsampling where useful;
- no visible block artifacts, banding severe enough to distract, broken loops,
  single-frame flashes, or accidental clipping;
- purposeful foreground, middle-ground, and background separation;
- controlled motion using transform-like operations and restrained noise;
- a unique focal structure for every major role;
- legible overlays and sufficient contrast when used behind product UI;
- stable poster framing across desktop, tablet, and mobile crops;
- no placeholder labels, test-card appearance, watermarks, or generated text;
- reduced-motion behavior continues to use posters rather than forcing video
  playback.

The generated files must remain within explicit performance budgets. Initial
targets are at most 4 MiB per MP4 and 350 KiB per WebP unless measurements show
that a smaller approved budget is practical. Quality must not be weakened merely
to pass an arbitrary size assertion; any budget change requires recorded size
and browser-loading evidence.

## 5. Generation architecture

### 5.1 Declarative manifest

`site/public/media/media-sources.json` will become a generated-media manifest.
It will contain no remote URL, download date, or third-party provider. Each
entry will declare at least:

- stable role;
- unique output filenames;
- scene identifier;
- generator version;
- duration and poster timestamp where applicable;
- working resolution and frame rate;
- license/provenance value indicating project-authored generation.

The manifest remains the single inventory consumed by generation and
verification. Application-facing filenames in
`site/src/data/media-manifest.ts` remain stable unless a tested migration is
necessary.

### 5.2 Generator

`site/scripts/prepare-media.mjs` will be converted from a downloader/optimizer
into an offline deterministic generator. It will invoke FFmpeg only through an
argument array with `shell: false`, retain bounded stderr, enforce timeouts, and
use a verified staging directory followed by atomic replacement.

Generation must:

- use only FFmpeg sources and filters whose parameters are checked into the
  repository;
- remove metadata and use deterministic encoding settings;
- generate every required asset in one invocation;
- never access the network;
- fail clearly when FFmpeg or a required encoder/filter is unavailable;
- clean temporary and staging directories independently on success or failure;
- avoid absolute personal paths in committed manifests, logs, or output
  metadata.

The generator will produce a project-authored provenance file in place of the
current Mixkit attribution. That document will name the generator version,
source manifest, repository license, and output roles. It will not imply that a
third party licensed or endorsed the media.

### 5.3 Git and build lifecycle

Generated MP4/WebP files will be explicitly ignored. The manifest, generator,
tests, and provenance document remain tracked.

The Pages workflow will:

1. check out the repository;
2. install Node dependencies;
3. ensure a supported FFmpeg is available;
4. generate the original media offline from repository inputs;
5. run lint, strict type checking, tests, and the site build;
6. verify generated media and built-runtime URLs;
7. upload only the validated `site/dist` Pages artifact.

No third-party media download or fallback URL is permitted. A generation or
verification failure blocks deployment.

## 6. Validation and TDD strategy

Implementation begins by replacing the existing Mixkit-oriented assertions
with failing tests for the new contract. Required automated coverage includes:

- manifests reject `sourcePage`, `downloadUrl`, third-party provider fields,
  unknown scene identifiers, unsafe filenames, duplicate roles/files,
  non-project provenance, and unsupported generation settings;
- all nine stable roles are present and map to different primary assets;
- the generator performs no `fetch`, HTTP, HTTPS, shell invocation, or user
  path interpolation;
- cleanup attempts every registered temporary target and preserves the primary
  error;
- generated headers identify valid MP4/WebP files;
- duration, dimensions, frame rate, codec/pixel format, file size, and poster
  dimensions satisfy the manifest and budgets;
- production source and built output contain no remote runtime media reference;
- the public site still resolves each role under the GitHub Pages base path;
- reduced-motion and missing-video behavior continue to use local posters;
- a clean checkout can generate, test, and build without any media download.

The repository-wide `python scripts/validate.py` and the site checks remain
release gates.

## 7. Visual review gate

Automated validity is not sufficient. Before publication, the implementation
must generate a local visual-review contact sheet or equivalent page and review
the live site in the real Home, Workspace, Compare, and Report contexts.

The review records:

- every role is visually distinct;
- video loops and poster transitions are clean;
- overlays remain readable;
- crops work at representative desktop and mobile widths;
- motion is restrained and meaningful;
- `prefers-reduced-motion` produces a stable experience;
- no artifact suggests third-party footage, a brand, a person, or private data.

Any failed visual item blocks publication and requires iteration. Screenshots
used as release evidence must be generated from this project and inspected for
private paths, browser-account data, and unrelated tabs or windows.

## 8. Migration and Git history

All tracked Mixkit-derived MP4/WebP files will be removed in the implementation
commit. Their URLs and license claims will be removed from tracked source and
documentation. The public remote currently contains only a bootstrap commit, so
the safe publication path is to create the first complete public snapshot from
the cleaned implementation rather than publish the local development history
that contains the removed binaries.

The local development history will not be destructively rewritten. Publication
must use an auditable snapshot/branch procedure that excludes unreachable or
historical third-party objects from the public repository. The final remote
tree and object reachability must be checked before Pages is enabled.

## 9. Error handling and rollback

- Missing FFmpeg or an unavailable required encoder/filter produces an explicit
  build failure with an installation/action message.
- Partial media is never copied into the final directory.
- Failed generation does not fall back to old media, remote URLs, or reduced
  unreviewed placeholders.
- Pages deployment occurs only after media verification and site tests pass.
- The bootstrap remote remains recoverable until the validated snapshot is
  uploaded; no force-push is required for the selected publication path.

## 10. Non-goals

This work does not:

- add AI-generated imagery or external generation services;
- change CPU detector behavior or introduce a global quality score;
- publish user videos or evidence;
- enable Supabase authentication or sharing;
- add analytics, remote fonts, CDNs, or tracking;
- promise identical encoded bytes across arbitrary FFmpeg versions without a
  pinned build environment; semantic inputs and validation remain deterministic,
  while the Pages runner provides the controlled public build environment.

## 11. Release acceptance

Publication is allowed only after all of the following are true:

- the tracked tree contains no Mixkit-derived media or Mixkit URL;
- the public snapshot contains no historical third-party media objects;
- a clean generation and production build succeeds;
- Python and site validation pass without weakened tests;
- media technical and visual review gates pass;
- the GitHub Pages workflow succeeds from the public repository;
- the signed-out production URL works under `/VideoScope/`;
- remaining manual limitations, including optional auth being unconfigured, are
  accurately documented.
