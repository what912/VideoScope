# Visual semantic drift detector

`visual_semantic_drift` is an optional local visual-consistency heuristic. It
compares visual embeddings only between sampled frames that belong to the same
detected scene. It is not face recognition, person re-identification, or a
database of character identities.

## Install and run

The CPU v0.1 installation and detector profile remain unchanged. Install and
enable the optional AI profile explicitly:

```text
python -m pip install -e ".[ai]"
videoscope analyze example.mp4 \
  --enable-ai \
  --detector visual_semantic_drift \
  --ai-device auto \
  --output runs/visual-drift
```

The default provider is the local DINOv2 visual backbone. VideoScope first
checks the torch hub repository and checkpoint caches. If either resource is
missing, interactive analysis asks for permission before torch hub may fetch
it. Non-interactive analysis must pass `--allow-model-download`. Base tests
never exercise this download path.

## Providers

The detector supports the registered DINOv2 and OpenCLIP image providers. A
configuration can select OpenCLIP when prompt alignment should share the same
image embeddings:

```json
{
  "detector_configurations": {
    "visual_semantic_drift": {
      "provider_id": "openclip",
      "long_gap_seconds": 1.5,
      "scene_boundary_guard_seconds": 0.25,
      "minimum_distance_threshold": 0.15,
      "baseline_mad_multiplier": 3.0,
      "merge_gap_seconds": 0.5
    }
  }
}
```

The provider selection fills the matching built-in model and preprocessing
identifiers unless the configuration overrides them. When
`prompt_alignment` and `visual_semantic_drift` request the same provider,
model, preprocessing version, video hash, and frame timestamp, the shared
`ModelRuntimeManager` returns the cached embedding instead of encoding that
frame twice.

## Algorithm

For every scene, the detector:

1. removes samples inside the configured boundary guard;
2. obtains normalized image embeddings through the shared runtime;
3. computes cosine distance for adjacent frames and deterministic longer-gap
   frame pairs;
4. builds a scene-local baseline from the median distance and median absolute
   deviation (MAD);
5. flags distances above the larger of the robust scene threshold and the
   configured minimum distance threshold; and
6. merges nearby candidates without crossing the scene boundary.

Very short scenes and scenes with too few baseline comparisons are skipped.
No comparison is made across a cut.

A Finding is titled `Abrupt visual semantic drift`. Its evidence contains the
sample immediately before and after the peak comparison. Finding metadata and
report diagnostics record the provider, model, scene baseline, threshold, peak
distance, comparison type, and the path-free distance time series.

The score is a detector-local summary of how far the peak exceeds the scene
threshold. It is not a calibrated probability or a general quality score.

## Limitations

Rapid camera movement, large occlusion, reasonable deformation, and large
lighting changes can all produce high embedding distance without a generation
error. Sampling may miss short events, and incorrect scene boundaries can
change the baseline. The method detects feature-space inconsistency only; it
must not be interpreted as confirmation that a character identity changed or
that a person was replaced.

## Offline tests

Base CI uses `FakeEmbeddingProvider` with controlled vectors. It covers a
stable sequence, a middle jump, cut isolation, provider sharing, cache hits,
short-scene skipping, and the invariant that shared frames are encoded only
once. The real DINOv2 integration test is opt-in and requires an already cached
model:

```text
set VIDEOSCOPE_RUN_DINOV2_TESTS=1
python -m pytest tests/ai/test_dinov2_optional.py
```
