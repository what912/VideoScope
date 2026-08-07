# Advanced AI implementation plan

Base: `840393d` (`codex/long-video-useful-content-cpu-mvp`)

Branch: `codex/advanced-ai-useful-content`

## Task 1: Freeze contracts and roadmap

- add the Advanced AI product, architecture and roadmap sections;
- define privacy, provenance, grounding and failure-degradation rules;
- bump the development version to `0.7.0.dev0` only after contracts pass.

## Task 2: Add strict intelligence models

- implement versioned transcript, evidence, suggestion, review and report models;
- implement canonical JSON, deterministic IDs, safe relative paths and atomic
  writers;
- test Unicode, invalid ranges, unknown fields, non-finite values and roundtrip.

## Task 3: Extend provider protocols and shared runtime

- add ASR and structured content-intelligence protocols;
- add lazy registrations and run records;
- add bounded transcript/suggestion caching without path-bearing keys;
- preserve existing embedding/OCR behavior and lifecycle sharing.

## Task 4: Add offline fake providers

- deterministic ASR and semantic fake providers;
- malformed response, failure, cancellation and cache-hit controls;
- prove two consumers share one loaded provider and cached response.

## Task 5: Add optional local ASR

- lazy `faster-whisper` provider in an `asr` extra;
- local-cache health check and explicit model-download policy;
- batched/streaming normalized segments and recorded inference metadata;
- optional integration tests skipped unless weights are already available.

## Task 6: Add optional local semantic provider

- loopback-only Ollama provider with strict timeout and response-size bounds;
- deterministic generation options and explicit model identity;
- strict JSON request/response contracts and actionable health diagnostics;
- no implicit model pull or remote endpoint support.

## Task 7: Build grounding and review services

- assemble bounded evidence from ContentMap and transcript;
- validate source ranges and transcript cue references;
- normalize, sort and hash suggestions;
- record accepted, rejected and edited decisions without mutating originals.

## Task 8: Bridge accepted suggestions into C

- chapter suggestions become editable chapter user ranges;
- highlight suggestions become selected-clip keep ranges;
- summaries and titles remain text artifacts;
- require the existing preview, exact confirmation and independent verification.

## Task 9: Add Advanced AI pipeline and CLI

- prepare AI review from a local video and optional transcript;
- expose provider/device/download choices and CPU fallback;
- write private review artifacts and a redacted technical report;
- return stable exit codes for input, provider and grounding failures.

## Task 10: Add local Web API

- create bounded AI jobs using the same core pipeline;
- add status, cancellation, review decisions and artifact containment;
- keep heavy-model concurrency bounded to one by default;
- keep private AI artifacts inaccessible through public artifact routes.

## Task 11: Add bilingual Web review experience

- add Local AI assist to Useful Content;
- show transcript, chapters, highlights, summaries and titles;
- support accept/reject/edit and apply-to-storyboard actions;
- show provider/model disclosures, limitations and fallback status.

## Task 12: Add evaluation and fixtures

- deterministic transcript/content-map fixtures;
- grounding precision and coverage metrics without a global quality score;
- a human-evaluation template and held-out dataset instructions;
- no fabricated benchmark or real-video claim.

## Task 13: Test native end-to-end workflows

- generate fixtures with FFmpeg;
- run trusted-transcript and Fake AI workflows twice for determinism;
- render accepted clips through C and verify exact source maps;
- verify cancellation, cleanup and Unicode paths.

## Task 14: Documentation and open-source experience

- README quick start for CPU, AI assist, privacy and Web;
- provider setup/troubleshooting and model-license disclosures;
- screenshots/demo data marked accurately;
- contributor guide for new AI providers and evaluation datasets.

## Task 15: Release and distribution audit

- run unified Python and frontend verification;
- build wheel/sdist and audit contents;
- clean-environment base, web, asr and ai smoke tests without weights;
- scan secrets, paths, shell usage, remote resources and package boundaries.

## Task 16: GitHub and public deployment

- merge the reviewed release branch, push the exact commit and run CI;
- complete repository metadata, topics, templates, security and launch assets;
- deploy the static product site;
- deploy a public processing backend only when authentication, quotas, retention,
  abuse controls, hosting ownership and privacy terms are demonstrably active;
- otherwise publish the fully functional local Web/CLI path without falsely
  claiming that GitHub Pages performs server-side video processing.

