# Optional shared model runtime

VideoScope ships a provider-neutral AI runtime plus optional local OpenCLIP
and DINOv2 providers. CPU analysis remains the complete default path.

## Installation boundaries

The base install does not depend on torch, OpenCLIP, DINOv2, PaddleOCR, FastAPI
or Uvicorn:

```text
python -m pip install .
```

Reserved optional dependency groups are:

```text
python -m pip install ".[ai]"
python -m pip install ".[ocr]"
python -m pip install ".[web]"
python -m pip install ".[all]"
```

Installing an extra does not download model weights or enable a detector.
Provider-specific GPU wheels and weight licenses may require additional
installation decisions.

## CLI inspection

These commands list and inspect lazy provider registrations:

```text
videoscope models list
videoscope models doctor
```

They do not import heavy runtimes, probe CUDA, contact a model registry or
download weights. `models doctor` checks only the lightweight protocol runtime,
local cache writability, installed-package metadata and download policy.

`--allow-model-download` represents explicit permission for a provider:

```text
videoscope models doctor --allow-model-download
```

The doctor itself still loads no model and downloads nothing. Non-interactive
analysis without this flag rejects a first-time model download.

## Provider contract

Providers implement `ModelProvider`; embedding models additionally implement
`EmbeddingProvider`, while OCR models implement `OCRProvider`. Provider
factories receive the Manager-resolved device and precision. Factories must
not import their heavy framework until the Manager actually requests the model.

Detectors do not own providers. An orchestrator constructs one
`ModelRuntimeManager` and injects it into:

```text
AnalysisContext.shared_cache["model_runtime"]
```

Two detectors requesting the same provider/model/device/precision receive the
same loaded instance. Identical frame requests are served from the shared
embedding cache.

## Cache and privacy

The memory tier is an LRU constrained by `memory_budget_bytes`. The disk tier
uses atomic NumPy archives under the platform cache directory by default.
Cache keys cover video SHA-256, exact frame timestamp, provider, model and
preprocessing version.

Embedding archives do not include source filenames or paths. They remain
sensitive derived content and should receive the same access-control care as
reports and evidence frames. No cache content is uploaded by VideoScope.

## Runtime records

Each embedding or OCR operation records:

- provider and model ID;
- device and precision;
- configured batch size;
- requested and newly encoded item counts;
- provider inference time;
- cache hits, misses and hit rate;
- success or sanitized failure type.

When a Manager is injected into `AnalysisPipeline`, these records are copied
into the report runtime metadata. They are operational measurements, not model
quality scores.

## Testing

`FakeEmbeddingProvider` produces deterministic embeddings from local bytes and
UTF-8 text. It has failure injection, local-cache availability and lifecycle
counters. Base tests use only this provider and never access a network, model
registry, GPU or real model package.

The real `OpenCLIPEmbeddingProvider` imports `open_clip` and torch only when the
shared Manager first requests it. It uses the model-specific validation image
transform and tokenizer, returns L2-normalized NumPy embeddings, and records
both the OpenCLIP model name and pretrained-weight identifier. See
`docs/detectors/prompt-alignment.md` for its detector semantics.

`DINOv2EmbeddingProvider` is image-only. It imports torch and torchvision only
when first requested, checks both the torch hub repository cache and checkpoint
cache before declaring local availability, batches local images, and returns
L2-normalized NumPy embeddings. Missing repository code or weights require the
same explicit download permission as every other provider. See
`docs/detectors/visual-semantic-drift.md` for its detector semantics.

`PaddleOCRProvider` performs timestamped local detection and recognition for
Chinese and English. It is registered lazily under the `ocr` extra, uses the
Manager for singleton sharing, batching, device and download policy, and
normalizes pixel rectangles to unit-square boxes. Base CI uses
`FakeOCRProvider`; it does not install Paddle or download OCR models. See
`docs/detectors/text-stability.md`.

If prompt alignment and visual semantic drift use OpenCLIP with identical
model and preprocessing settings, they share the same loaded singleton and
frame cache. Text embeddings remain provider operations and are not stored in
the frame cache.
