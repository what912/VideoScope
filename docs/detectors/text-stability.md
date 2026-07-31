# Temporal text stability detector

`text_stability` is an optional, local OCR-based heuristic. It follows
recognized text regions through sampled frames and reports temporal patterns
that may indicate unstable generated lettering. It never sends frames to a
remote OCR API.

## Install and run

PaddleOCR and its inference runtime are intentionally excluded from the base
installation:

```text
python -m pip install -e ".[ocr]"
videoscope analyze example.mp4 \
  --enable-ocr \
  --detector text_stability \
  --output runs/text-stability
```

Chinese (`ch`) and English (`en`) configurations are supported. The default is
Chinese. To select English:

```json
{
  "enabled_detectors": ["text_stability"],
  "detector_configurations": {
    "text_stability": {
      "language": "en"
    }
  }
}
```

The shared runtime checks the local PaddleX model cache first. If model files
are missing, non-interactive analysis refuses a download unless
`--allow-model-download` is supplied. Installing the `ocr` extra does not
enable the detector or silently download model weights.

## Provider contract

`OCRProvider.detect_and_recognize(images)` accepts timestamped local frame
paths and returns recognized text, OCR confidence, a normalized bounding box,
and the source frame timestamp.

`PaddleOCRProvider` imports PaddleOCR only when the
`ModelRuntimeManager` loads it. The Manager owns provider lifecycle, singleton
sharing, batching, device policy, explicit model-download permission and run
records. Detectors do not instantiate PaddleOCR directly.

## Tracking and diagnostics

Each detected scene is processed independently. Observations are matched using
time continuity, bounding-box intersection-over-union, and normalized Unicode
text similarity based on edit distance.

The detector looks for repeated text changes in one region, a short deformed
recognition between stable observations, a brief disappearance, and a short
interior text flash. One ordinary subtitle replacement is not enough to
produce a Finding. Tracks never cross a scene cut. Low-confidence isolated
results are ignored, and monotonic displacement is treated as likely rolling
text.

A Finding is titled `Potential temporal text instability`. Evidence metadata
contains OCR text, confidence, normalized boxes and edit distance. The offline
report draws those boxes over evidence thumbnails. Path-free diagnostics are
stored under `runtime.detector_diagnostics.text_stability`.

## Limitations

OCR errors can themselves create apparent instability. Stylized, curved,
small, occluded or motion-blurred text can be recognized inconsistently even
when the rendered video is correct. Sampling can miss short events, and
unusual but intentional subtitle timing may still require human review.

The detector score and confidence are detector-local heuristic summaries, not
calibrated probabilities or a global quality score.

## Tests

Base tests use `FakeOCRProvider`; they require no network, GPU, Paddle package
or model weights. The fixture factory also creates `stable_text.mp4` and
`changing_text.mp4` from program-generated images.

The real integration test is opt-in and only uses models already in the local
cache:

```text
set VIDEOSCOPE_RUN_PADDLEOCR_TESTS=1
python -m pytest tests/ai/test_paddleocr_optional.py
```
