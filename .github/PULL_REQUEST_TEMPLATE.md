## Summary

Describe the scoped change and the user-visible outcome.

## Product and architecture check

- [ ] I read `AGENTS.md` and the relevant product/architecture documents.
- [ ] This change stays within the requested scope and preserves local-first defaults.
- [ ] Findings describe observable signals and include limitations.
- [ ] No base install or test downloads an AI model or requires network/GPU.

## Verification

- [ ] `python scripts/validate.py`
- [ ] `python scripts/generate_test_videos.py --force` (when FFmpeg/video behavior changes)
- [ ] `python -m build` (when packaging changes)
- [ ] I added or updated focused tests.
- [ ] Paths containing spaces and non-ASCII characters remain supported.

Commands and summarized results:

```text
paste results here
```

## Privacy and distribution

- [ ] I did not add private videos, prompts, reports, credentials, personal paths, generated fixtures, `runs/`, or local caches.
- [ ] External commands use argument arrays and do not use `shell=True`.
- [ ] New dependencies or redistributed assets include appropriate license notes.

## Benchmark impact

State detector-specific metric changes, or “not applicable.” Synthetic fixture
results must not be described as real-world accuracy.
