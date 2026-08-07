# Suspicious text privacy proposals

`suspicious_text` is an optional Safe Sharing scanner. It uses the shared local
OCR runtime to propose visible regions that may contain a phone number, email,
address, account identifier, verification code, local path, or URL. It does not
decide that recognized text belongs to a person and does not perform identity
recognition.

## Privacy boundary

The public risk description contains only a broad category such as
`email-like` or `address-like`. Raw OCR text is retained only in the private
review document under `private_evidence`; it is excluded from public evidence
and share-package summaries. Frame references remain output-relative.

The scanner normalizes Unicode with NFKC before conservative pattern matching.
`zh-CN` enables Chinese labels and address forms; `en` enables English labels
and street-address forms. Email, URL, path, and explicit numeric phone patterns
remain language-neutral. Other locale values are rejected rather than silently
guessing.

The scanner rejects isolated low-confidence OCR output. Stable low-confidence
email or phone observations may still be proposed when they repeat across the
configured number of adjacent sampled frames. Association stays inside one
scene and uses rectangle overlap, normalized-text similarity, and time
continuity. Same-frame observations for the same box are aggregated before
tracking so duplicated OCR output cannot produce duplicate risk identities.
Every threshold is part of `SuspiciousTextConfig` and is recorded by the later
orchestration layer as effective configuration.

## Optional installation and fallback

The base VideoScope installation does not install PaddleOCR, PaddlePaddle, or
model weights. Base tests use `FakeOCRProvider`, stay offline, and never access
a GPU. Users who explicitly choose local OCR can install the optional extra:

```text
python -m pip install "genvideoscope[ocr]"
```

Model loading and any permitted first-time model download remain controlled by
`ModelRuntimeManager`; this scanner never downloads a model itself. When an OCR
runtime is not configured, scanner execution is `skipped` with the
`manual_visual_region` fallback. A runtime/provider failure is reported as a
scanner error and does not remove metadata, manual-region, or other successful
privacy risks.

## Limitations

- OCR errors can create false positives, including a wrong category.
- Stylized text, handwriting, motion, blur, occlusion, low resolution, and
  unsupported languages can cause missed text.
- Pattern matching is intentionally conservative and will not recognize every
  possible private identifier.
- URLs and account-like strings may be harmless; proposals are observations,
  not confirmed privacy violations.
- Sampled-frame tracking can miss short appearances between frames.
- Human review is required. Users should add or edit a manual visual region
  whenever OCR is unavailable, uncertain, or incomplete.
