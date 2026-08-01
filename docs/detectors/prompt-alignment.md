# Prompt alignment detector

`prompt_alignment` is an optional, local OpenCLIP-based detector. It compares
the user-supplied prompt with multiple sampled frames from each scene. It does
not upload the video, prompt, frames, or embeddings to an external API.

## Install and run

The CPU v0.1 installation remains unchanged. Install the optional provider only
when this detector is needed:

```text
python -m pip install -e ".[ai]"
videoscope analyze example.mp4 \
  --prompt "A red car driving through snow" \
  --enable-ai \
  --ai-device auto \
  --output runs/prompt-alignment
```

If the selected OpenCLIP weights are not already cached, VideoScope asks before
downloading in an interactive terminal. Non-interactive use must add
`--allow-model-download`. Installing the `ai` extra or passing a prompt alone
does not enable the detector or authorize a model download.

Without a prompt, an enabled `prompt_alignment` execution is recorded as
`skipped`. Provider, device, model, cache, or inference failures are recorded as
`detector_error`; completed CPU detector results remain in the report.

## Modes

The default `descriptive` mode creates no Finding. It records the scene-level
similarity curve and the lowest-scoring scene under
`runtime.detector_diagnostics.prompt_alignment`.

`threshold` mode is opt-in and requires the user to provide
`similarity_threshold`. VideoScope deliberately has no universal default
threshold:

```json
{
  "detector_configurations": {
    "prompt_alignment": {
      "mode": "threshold",
      "similarity_threshold": 0.2,
      "representative_frames_per_scene": 3
    }
  }
}
```

Scenes whose mean cosine similarity is below that user-provided threshold
produce a Finding titled `Low prompt-frame similarity`. Consecutive
below-threshold scenes are merged by default. Evidence includes the prompt,
scene interval, per-scene mean values, the minimum value, and the
lowest-scoring sampled frame.

## Score meaning

The raw scene metrics are cosine similarities in `[-1, 1]`. A threshold-mode
Finding maps its merged mean similarity into a detector-local anomaly score in
`[0, 1]`; this score is not a general video quality score and must not be
combined with unrelated detector scores as if they shared one calibration.

OpenCLIP similarity is not a complete semantic verification of a prompt.
Complex actions, negation, counts, and spatial relationships can be represented
unreliably. The detector therefore never reports that prompt compliance or a
prompt violation has been confirmed.

## Testing

Base CI uses `FakeEmbeddingProvider`, never downloads weights, never contacts a
model registry, and does not require torch or a GPU. The real integration test
is opt-in and only uses already cached weights:

```text
set VIDEOSCOPE_RUN_OPENCLIP_TESTS=1
python -m pytest tests/ai/test_openclip_optional.py
```
