# GenVideoScope v0.2.0 release checklist

Target:

- repository: `GenVideoScope`
- PyPI distribution: `genvideoscope`
- Python package: `videoscope`
- CLI: `videoscope`
- candidate version: `0.2.0rc1`
- candidate tag: `v0.2.0-rc1`

Do not publish while any item marked **blocking** is unresolved.

## Source and identity

- [ ] `pyproject.toml` name is `genvideoscope`.
- [ ] package import and CLI remain `videoscope`.
- [ ] `videoscope --version` prints `VideoScope 0.2.0rc1`.
- [ ] GitHub repository name and links use `GenVideoScope`.
- [ ] PyPI name availability and project ownership are checked manually.
- [ ] tag points to the exact reviewed commit.

## Security and privacy

- [ ] secret and personal-path scans are clean after excluding deliberate test
  strings.
- [ ] no production `shell=True`, `os.system`, or user-built shell command.
- [ ] upload size, prompt size, and configuration size are bounded.
- [ ] ffprobe, not extension or MIME, validates uploaded video content.
- [ ] artifact, evidence, and upload paths have traversal tests.
- [ ] default Web bind, Host, Origin, and CORS behavior are local-only.
- [ ] HTML uses autoescaping and rejects unsafe local artifact paths.
- [ ] reports contain no absolute workspace path.
- [ ] maintainers manually review what filename, prompt, SHA-256, timestamps,
  evidence frames, and bundled video reveal before sharing a report.

## Dependency boundaries

- [ ] base wheel installs no AI, OCR, or Web runtime.
- [ ] `ai`, `ocr`, `web`, and `all` remain explicit extras.
- [ ] optional imports are lazy.
- [ ] no model weights are included or downloaded by base tests.
- [ ] the exact resolved transitive licenses are reviewed.
- [ ] `NOTICE` and `docs/third-party-licenses.md` match declarations.
- [ ] FFmpeg remains an external executable and is not bundled.

## API and compatibility

- [ ] report schema remains `0.1`, or migration notes are added before changing
  it.
- [ ] `AnalysisConfig` rejects unknown fields.
- [ ] CLI exit codes remain documented and tested.
- [ ] Detector protocol, requirements, configuration model, Finding shape, and
  deterministic ordering remain tested.
- [ ] optional detector/provider failure preserves completed CPU results.

## Performance and cleanup

- [ ] input hashing and upload use bounded chunks.
- [ ] FFmpeg extracts sampled frames without loading the video into memory.
- [ ] pipeline samples frames once and shares the resulting context.
- [ ] matching AI detectors share provider instances and embedding cache keys.
- [ ] original video is not bundled unless explicitly requested.
- [ ] workspaces, upload staging, cancelled jobs, and expired jobs are cleaned.
- [ ] long-video memory and disk behavior is manually profiled on a realistic
  authorized input.

## Documentation and examples

- [ ] README first screen has value, install, analyze, screenshot, CPU detectors,
  and local-first statement.
- [ ] source, wheel, extras, Web, Benchmark, and API commands match the candidate.
- [ ] CPU heuristics and AI/OCR heuristics are clearly distinguished.
- [ ] limitations do not claim unmeasured accuracy.
- [ ] examples import or parse under supported Python versions.
- [ ] PowerShell and POSIX shell examples are manually checked on their target
  platforms.

## Required automated verification

Run from the repository root:

```powershell
python scripts/generate_test_videos.py --force
python scripts/verify.py
python -m build
python scripts/audit_distribution.py dist
python scripts/smoke_test.py `
  --dist dist `
  --video tests/fixtures/generated/black_segment.mp4

cd web
npm ci
npm test
npm run build
cd ..
```

Clean-profile checks:

```powershell
python -m pip install dist/genvideoscope-0.2.0rc1-py3-none-any.whl
python -m videoscope --version
python -m videoscope doctor

python -m pip install "dist/genvideoscope-0.2.0rc1-py3-none-any.whl[web]"
python -c "from videoscope.web.app import create_app; print(create_app().title)"

python -m pip install "dist/genvideoscope-0.2.0rc1-py3-none-any.whl[ai]"
python -m videoscope models list
```

The AI check installs code dependencies only. Do not run a provider or authorize
a model download during the base release audit.

## Human release gate

- [ ] Review [release-audit.md](../release-audit.md).
- [ ] Confirm no unpublished private media is present in the Git index.
- [ ] Review the complete staged diff.
- [ ] Confirm CI passes on Windows and Linux, Python 3.11 and 3.12.
- [ ] Confirm the candidate on a clean macOS environment if macOS is advertised.
- [ ] Enable GitHub private vulnerability reporting.
- [ ] Create the commit and local candidate tag only after all blockers close.

Suggested commands, intentionally not executed by the audit:

```powershell
git add .
git commit -m "release: prepare GenVideoScope v0.2.0"
git tag v0.2.0-rc1
```

Publishing commands belong to a later, separately authorized release operation.
