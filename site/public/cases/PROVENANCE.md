# Public case media provenance

These public comparison cases are authored demonstrations generated locally by
the VideoScope project. They are licensed as project-authored work under
Apache-2.0 and are not real-user footage or evidence about real-user results.

Regenerate the cases with:

```text
python scripts/generate_growth_cases.py --force
```

VideoScope version: `0.8.0`.

Generation and verification require locally installed FFmpeg and ffprobe. The
case manifest records the exact hashes, source declarations, and reproducible
metadata for each public asset. This provenance file is a build-time audit
record and is intentionally excluded from the deployed public site.
