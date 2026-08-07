# VideoScope Advanced AI design

Status: approved by the maintainer request to complete the Advanced AI phase

Target version: `0.7.0.dev0`

## 1. Product outcome

Advanced AI makes the existing A/D/B/C workflows easier to use; it does not
replace their deterministic safety gates. The first release turns an authorized
local video into reviewable semantic material:

1. a timestamped local transcript when no trusted transcript exists;
2. semantic chapter suggestions with source ranges and evidence;
3. highlight candidates with a plain-language reason and limitations;
4. editable summary and title suggestions grounded in transcript excerpts;
5. an optional bridge that applies only user-accepted suggestions to the
   existing Long Video to Useful Content planner.

The user receives usable content, not merely an opaque score. AI never renders,
deletes, publishes, shares, or overwrites media by itself.

## 2. Non-goals

- no face recognition, identity database, person re-identification, or identity
  claim;
- no uncalibrated overall quality, virality, importance, or truth score;
- no silent upload, telemetry, remote inference, or model download;
- no AI-generated replacement frames, speech, people, or factual events;
- no automatic acceptance of a title, summary, chapter, or clip;
- no weakening of Check, Publish Ready, Safe Sharing, Video Rescue, or Content
  verification;
- no claim that a model summary is complete or factually verified.

## 3. Trust model

The CPU content map remains the evidence backbone. AI output is a proposal with
provenance, not a fact. Every proposal records:

- provider, model, device, precision, preprocessing and prompt-contract version;
- source video hash and optional transcript hash;
- one or more source time ranges;
- supporting transcript cue IDs or frame timestamps;
- provider confidence when available, explicitly labelled uncalibrated;
- a user-facing explanation and limitations;
- deterministic proposal identity after canonical normalization.

AI proposals are private review artifacts. Public content packages contain only
accepted, independently verified results and a redacted provenance summary.

## 4. Provider architecture

The shared model runtime remains the only owner of provider lifecycle. It gains
two optional capabilities:

- `ASRProvider.transcribe`: local audio to normalized timed speech segments;
- `ContentIntelligenceProvider.suggest`: transcript/content-map evidence to a
  strict structured suggestion batch.

Provider registration is lazy. Importing `videoscope`, running base tests, or
using CPU workflows must not import model packages, probe a GPU, start a local
model server, or access the network.

Initial providers:

- `FakeASRProvider` and `FakeContentIntelligenceProvider` for offline tests;
- optional `FasterWhisperASRProvider` for local ASR with pre-existing weights or
  explicit download authorization;
- optional `OllamaContentIntelligenceProvider` restricted to loopback by
  default, with strict JSON output validation and an explicit local-model ID.

Ollama is treated as an external local process. Non-loopback endpoints are
rejected unless a future separately reviewed remote-provider contract exists.

## 5. Data domains

`videoscope.intelligence` owns a versioned schema separate from v0.1 reports:

- `AISourceEvidence`
- `AITranscriptSegment`
- `AISuggestion`
- `AISuggestionBatch`
- `AIReviewDecision`
- `AIReviewManifest`
- `AIExecutionRecord`
- `AIContentReport`

Suggestions use explicit kinds: `chapter`, `highlight`, `summary`, and `title`.
Only chapter and highlight suggestions carry executable source ranges. Summary
and title suggestions are text proposals and never alter media directly.

Canonical JSON is UTF-8, sorted, finite, path-safe, and atomically written.
Suggestion IDs are hashes of schema version, source/transcript hashes, provider
identity, kind, normalized source ranges, evidence IDs, and normalized content.

## 6. Workflow

```text
authorized local video
  -> deterministic C content map
  -> trusted timed transcript OR optional local ASR
  -> strict AI suggestion request
  -> schema validation and grounding audit
  -> private AI review report
  -> user accepts/rejects/edits suggestions
  -> accepted ranges converted to ordinary C user ranges
  -> existing preview + exact confirmation + native render
  -> independent verification + source map
```

Grounding audit rejects source ranges outside the video, unknown transcript cue
IDs, missing evidence, unsupported kinds, invalid JSON, absolute paths, and
model text that exceeds configured bounds. A provider failure creates a visible
failed execution record and returns the CPU preparation unchanged.

## 7. User experience

CLI and local Web expose one Advanced AI entry inside Useful Content:

- choose `Local AI assist` explicitly;
- choose transcript source: trusted file or local ASR;
- inspect model/download/device disclosures before execution;
- view chapter/highlight evidence next to the video timeline;
- accept, reject, or edit each proposal;
- copy summaries/titles or apply accepted ranges to the C storyboard;
- confirm the ordinary C plan before any media is rendered.

The interface never labels AI suggestions as verified truth. It shows model
identity, evidence coverage, limitations, and a clear CPU fallback.

## 8. Privacy and downloads

- default processing remains local and offline;
- raw video, frames, audio, transcript, and prompts are sensitive;
- real model weights are never included in the wheel or repository;
- missing weights require `--allow-model-download` in non-interactive use;
- Ollama calls are loopback-only and require `--enable-ai`;
- no provider may log transcript text, user paths, or raw model prompts by
  default;
- caches use content hashes and bounded retention, with explicit clear support;
- public artifacts exclude raw prompts, rejected suggestions, transcript text,
  and private thumbnails unless the user explicitly exports them.

## 9. Determinism

Model inference may vary across devices or provider versions, so the system does
not pretend inference itself is bit-deterministic. It instead guarantees that a
captured provider response plus identical source evidence produces identical
validated suggestions, IDs, sorting, review manifests, and downstream C plans.
Provider generation parameters default to deterministic settings where the
provider supports them and are always recorded.

## 10. Evaluation

Engineering fixtures prove contracts, failure isolation, grounding and exact
downstream mapping. They do not prove real-video quality. A real evaluation set
must be authorized and separated into development and held-out review groups.

Human rubrics cover:

- transcript timing and text usefulness;
- chapter boundary usefulness;
- highlight evidence relevance;
- summary faithfulness and omission risk;
- title faithfulness and misleading-language risk;
- accepted-range render fidelity and source mapping;
- time saved compared with manual review.

No accuracy or time-saving claim may be published without the dataset, rubric,
configuration and measured results.

## 11. Release gates

- base install and base CI remain offline, CPU-only and model-free;
- Fake providers cover success, malformed output, cancellation and failure;
- optional provider tests do not download weights in default CI;
- AI failure preserves complete CPU outputs;
- accepted suggestions cannot bypass preview, confirmation or verification;
- private/public artifact separation is tested;
- clean-wheel smoke covers base and optional extras;
- exact-commit CI, license review, real-media human review and public hosting
  security review remain mandatory before a public processing service.

