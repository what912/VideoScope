# Third-party dependency and license inventory

Audit date: 2026-07-29

This inventory covers every direct dependency declared by `pyproject.toml` and
`web/package.json`. Package metadata and upstream license files remain
authoritative. Transitive dependencies are selected by installers; distributors
must review the exact resolved environment they ship.

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
| `httpx2` | in-process API test client | BSD-3-Clause |
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

## Dashboard packages

Resolved versions below come from `web/package-lock.json` on the audit date.

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
include `node_modules`.

## External FFmpeg boundary

FFmpeg and ffprobe are user-installed external executables. VideoScope does not
copy or redistribute their binaries. FFmpeg licensing depends on the options
used to build a particular binary, including whether GPL components were
enabled. Review `ffmpeg -L` and the distributor's accompanying notices for the
exact build.

## Release procedure

Before publishing:

1. Build in a clean environment.
2. Record `python -m pip list` for each shipped installation profile.
3. Run `npm ci` and retain `web/package-lock.json`.
4. Review transitive licenses and model terms for the resolved versions.
5. Re-run the distribution audit to confirm no third-party binary, model, test
   video, cache, or personal path was bundled accidentally.
