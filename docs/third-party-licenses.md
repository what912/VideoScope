# Third-party dependency and license inventory

Audit date: 2026-08-25

This inventory covers direct declarations plus the frozen Windows Python 3.12
base + `web` runtime closure and both complete npm lock graphs. Package metadata
and upstream license files remain authoritative. The automated gate detects
identity and reviewed-license drift; it is not legal advice or a legal
certification. Human review remains an open release gate.

## Python base installation

| Distribution | Purpose | Declared license |
| --- | --- | --- |
| `typer` | CLI | MIT |
| `rich` | terminal rendering | MIT |
| `pydantic` | schemas and validation | MIT |
| `numpy` | numeric arrays | BSD-3-Clause plus bundled 0BSD/MIT/Zlib/CC0 components |
| `pillow` | image loading | MIT-CMU |
| `opencv-python-headless` | CPU image metrics | Apache-2.0 |
| `scenedetect-headless` | scene-boundary adapter | BSD-3-Clause |
| `jinja2` | offline HTML templates | BSD-3-Clause |
| `platformdirs` | local cache/data paths | MIT |

The base group contains no Torch, OpenCLIP, PaddleOCR, PaddlePaddle, FastAPI,
Uvicorn, or Node dependency.

## Python development group

| Distribution | Purpose | Declared license |
| --- | --- | --- |
| `pytest` | tests | MIT |
| `pytest-cov` | coverage integration | MIT |
| `ruff` | lint and formatting | MIT |
| `mypy` | static typing | MIT |
| `build` | wheel and sdist build | MIT |
| `setuptools` | build backend | MIT |
| `wheel` | wheel support | MIT |
| `httpx2` | in-process API test client (the installed distribution is `httpx2`, not `httpx`) | BSD-3-Clause |
| `fastapi` | Web API test runtime | MIT |
| `uvicorn` | local server test runtime | BSD-3-Clause |
| `python-multipart` | upload test runtime | Apache-2.0 |

The Web packages are intentionally repeated in `dev` so the documented
`.[dev]` installation can execute the complete Web API test suite. They remain
absent from the base installation.

## Optional AI group

| Distribution | Purpose | Declared license |
| --- | --- | --- |
| `torch` | tensor and model runtime | BSD-3-Clause |
| `torchvision` | image preprocessing | BSD-3-Clause |
| `open-clip-torch` | OpenCLIP provider | MIT |

DINOv2 is loaded through the user-installed PyTorch runtime. Its repository
declares Apache-2.0, but model checkpoints and model cards must be reviewed for
the exact revision selected by a user. VideoScope bundles neither code fetched
by Torch Hub nor weights.

## Optional OCR group

| Distribution | Purpose | Declared license |
| --- | --- | --- |
| `paddleocr` | OCR pipeline | Apache-2.0 |
| `paddlepaddle` | Paddle runtime | Apache-2.0 |

OCR model files are not bundled. Users must review the exact model terms before
redistribution.

## Optional Web group

| Distribution | Purpose | Declared license |
| --- | --- | --- |
| `fastapi` | local API | MIT |
| `uvicorn` | local ASGI server | BSD-3-Clause |
| `python-multipart` | streamed multipart parsing | Apache-2.0 |

## Frozen Windows Connector Python 3.12 runtime

The exact base + `web` closure below is the Windows runtime identity/license
lock. It is not an artifact-hash lock. The installer first installs
`packaging/windows/requirements-runtime.lock`, then installs this project with
`--no-deps`. Every bundled distribution must include its metadata and non-empty
license material. The Windows policy binds the normalized distribution name,
exact version, reviewed license identifier, and the exact relative path set and
canonical UTF-8 LF SHA-256 of every license-marked metadata file. Modern
`License-Expression` metadata is preferred; the documented legacy `License`
and classifier values use a deterministic, bounded normalization table.

| Distribution | Exact version | Reviewed SPDX-style identifier |
| --- | --- | --- |
| `annotated-doc` | `0.0.4` | `MIT` |
| `annotated-types` | `0.8.0` | `MIT` |
| `anyio` | `4.14.2` | `MIT` |
| `click` | `8.4.2` | `BSD-3-Clause` |
| `colorama` | `0.4.6` | `BSD-3-Clause` |
| `fastapi` | `0.140.13` | `MIT` |
| `h11` | `0.16.0` | `MIT` |
| `idna` | `3.18` | `BSD-3-Clause` |
| `Jinja2` | `3.1.6` | `BSD-3-Clause` |
| `markdown-it-py` | `4.2.0` | `MIT` |
| `MarkupSafe` | `3.0.3` | `BSD-3-Clause` |
| `mdurl` | `0.1.2` | `MIT` |
| `numpy` | `2.4.6` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| `opencv-python-headless` | `5.0.0.93` | `Apache-2.0` |
| `pillow` | `12.3.0` | `MIT-CMU` |
| `platformdirs` | `4.11.0` | `MIT` |
| `pydantic` | `2.13.4` | `MIT` |
| `pydantic-core` | `2.46.4` | `MIT` |
| `Pygments` | `2.20.0` | `BSD-2-Clause` |
| `python-multipart` | `0.0.32` | `Apache-2.0` |
| `rich` | `15.0.0` | `MIT` |
| `scenedetect-headless` | `0.7.1` | `BSD-3-Clause` |
| `shellingham` | `1.5.4` | `ISC` |
| `starlette` | `1.3.1` | `BSD-3-Clause` |
| `tqdm` | `4.70.0` | `MPL-2.0 AND MIT` |
| `typer` | `0.27.0` | `MIT` |
| `typing-extensions` | `4.16.0` | `PSF-2.0` |
| `typing-inspection` | `0.4.2` | `MIT` |
| `uvicorn` | `0.51.0` | `BSD-3-Clause` |

The selected Windows candidate environment was CPython 3.12.1. The lock records
version/license identity only; wheel filenames and artifact hashes are outside
this bounded review.

## Optional ASR group

| Distribution | Purpose | Declared license |
| --- | --- | --- |
| `faster-whisper` | optional local transcription provider | MIT |
| `ctranslate2` | faster-whisper inference runtime | MIT |

Neither package nor any Whisper checkpoint is included in the base + `web`
Windows runtime lock. Checkpoint/model terms are separate and must be reviewed
for any explicitly selected model.

## Dashboard packages

Direct resolved versions below come from `web/package-lock.json` on the audit
date. The complete transitive graph is frozen in the summary after the public
site direct-dependency table. Text-file digests use canonical UTF-8 content
with LF line endings, so an LF/CRLF checkout-only change does not alter the
reviewed identity.

| Package | Resolved version | Role | Declared license |
| --- | --- | --- | --- |
| `react` | 19.2.8 | runtime | MIT |
| `react-dom` | 19.2.8 | runtime | MIT |
| `@testing-library/jest-dom` | 6.9.1 | development | MIT |
| `@testing-library/react` | 16.3.2 | development | MIT |
| `@testing-library/user-event` | 14.6.1 | development | MIT |
| `@types/react` | 19.2.17 | development | MIT |
| `@types/react-dom` | 19.2.3 | development | MIT |
| `@vitejs/plugin-react` | 5.2.0 | development | MIT |
| `jsdom` | 27.4.0 | development | MIT |
| `typescript` | 5.9.3 | development | Apache-2.0 |
| `vite` | 7.3.6 | development | MIT |
| `vitest` | 3.2.7 | development | MIT |

Production wheels include compiled first-party dashboard assets and do not
include `node_modules`. The compiled dashboard redistributes the exact
production closure `react@19.2.8`, `react-dom@19.2.8` and `scheduler@0.27.0`.
All three packages carry the same upstream MIT license file at these versions;
its copyright notice and complete text are frozen in
`THIRD_PARTY_NOTICES.txt`, which is included in the wheel, source archive and
Windows Connector bundle.

## Public browser site packages

Resolved versions and SPDX identifiers below come from `site/package-lock.json`
on the audit date. This table lists direct dependencies; the complete graph is
covered by the frozen summary below.

### Runtime

| Package | Resolved version | Role | Declared license |
| --- | --- | --- | --- |
| `@supabase/supabase-js` | `2.57.4` | optional authentication and sanitized sharing adapter | `MIT` |
| `hash-wasm` | `4.12.0` | incremental local input hashing | `MIT` |
| `idb` | `8.0.3` | browser report persistence | `ISC` |
| `motion` | `12.23.12` | functional interface motion | `MIT` |
| `react` | `19.2.8` | interface runtime | `MIT` |
| `react-dom` | `19.2.8` | browser renderer | `MIT` |
| `react-router` | `8.3.0` | client-side routing | `MIT` |

### Development and build

| Package | Resolved version | Role | Declared license |
| --- | --- | --- | --- |
| `@eslint/js` | `9.39.4` | ESLint JavaScript rules | `MIT` |
| `@testing-library/jest-dom` | `6.9.1` | DOM assertions | `MIT` |
| `@testing-library/react` | `16.3.2` | React component tests | `MIT` |
| `@types/node` | `22.19.19` | Node.js type declarations | `MIT` |
| `@types/react` | `19.2.14` | React type declarations | `MIT` |
| `@types/react-dom` | `19.2.3` | React DOM type declarations | `MIT` |
| `@vitejs/plugin-react` | `6.0.2` | Vite React integration | `MIT` |
| `eslint` | `9.39.4` | lint runner | `MIT` |
| `jsdom` | `27.4.0` | test DOM environment | `MIT` |
| `typescript` | `5.9.3` | static type checker | `Apache-2.0` |
| `typescript-eslint` | `8.56.1` | TypeScript-aware lint rules | `MIT` |
| `vite` | `8.2.0` | development and production build tool | `MIT` |
| `vitest` | `4.1.0` | unit and integration test runner | `MIT` |

The public browser site deploys compiled first-party assets and project-authored
media. It does not deploy `node_modules`. Missing optional Supabase configuration
selects the unavailable adapter and leaves sign-in and sharing disabled.

## Complete npm lock graph freeze

The offline policy audits every non-root `packages` entry, not only the direct
tables above. It rejects lockfile digest, package count, license count, missing
version/license/integrity, and unreviewed-license drift.

| Lockfile | SHA-256 | Non-root packages | Complete license counts |
| --- | --- | ---: | --- |
| `web/package-lock.json` | `261dd00a3b6ab2af8f80fa6478ca9d77e6061588411441ecf368526a851796bd` | 217 | Apache-2.0 5; BlueOak-1.0.0 3; BSD-2-Clause 2; BSD-3-Clause 2; CC-BY-4.0 1; CC0-1.0 1; ISC 7; MIT 194; MIT-0 2 |
| `site/package-lock.json` | `2656b753a635ce794039f91b51f075f45196b1738451b7f62190702f2153537f` | 272 | 0BSD 1; Apache-2.0 19; BlueOak-1.0.0 4; BSD-2-Clause 9; BSD-3-Clause 3; CC0-1.0 1; ISC 10; MIT 210; MIT-0 2; MPL-2.0 12; Python-2.0 1 |

The dashboard graph's CC-BY-4.0 occurrence is `caniuse-lite@1.0.30001806`.
The public-site graph's twelve MPL-2.0 occurrences are `lightningcss@1.33.0`
and its eleven platform packages. These are explicit reviewed occurrences, not
a conclusion that no additional attribution or redistribution action is ever
required.

## External FFmpeg boundary

FFmpeg and ffprobe are user-installed external executables. VideoScope does not
copy or redistribute their binaries. FFmpeg licensing depends on the options
used to build a particular binary, including whether GPL components were
enabled. Review `ffmpeg -L` and the distributor's accompanying notices for the
exact build.

## Release procedure

Before publishing:

1. Build in a clean environment.
2. Audit `packaging/windows/requirements-runtime.lock` and the exact bundle
   metadata/license material.
3. Retain and audit both unchanged npm lockfiles against the committed policy.
4. Complete human review of transitive licenses and model terms for any profile
   being redistributed.
5. Re-run the distribution audit to confirm no third-party binary, model, test
   video, cache, or personal path was bundled accidentally.
