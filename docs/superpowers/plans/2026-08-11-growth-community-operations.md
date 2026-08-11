# Community and Zero-Cost Growth Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create truthful community entry points, privacy-preserving growth measurements, and a gated 90-day launch rhythm that can be operated at zero project-owner cloud cost.

**Architecture:** GitHub issue forms and checked-in playbooks collect structured feedback without hosting a new service. A manually invoked Python snapshot tool queries GitHub through `gh`, writes local JSON only, and has fully mocked offline tests; channel execution remains a human action behind readiness and authorization gates.

**Tech Stack:** GitHub Issue Forms, GitHub Discussions, GitHub CLI, Python 3.11+, stdlib JSON/subprocess, pytest, Markdown.

## Global Constraints

- Stars are an outcome metric, not a guaranteed deliverable or release gate.
- Never buy, exchange, automate, incentivize, or require Stars; do not ask coordinated groups to upvote or comment.
- Keep project-owner cloud compute, storage, database, analytics, ad, and model spend at zero.
- Do not add telemetry, tracking pixels, cookies, analytics SDKs, URL shorteners, or per-user identifiers.
- Growth snapshots contain aggregate public repository/release/traffic counts only and run solely when explicitly invoked by a maintainer.
- User case submission is optional, manual, and independent of product results; video is never uploaded automatically.
- Security reports must route to `SECURITY.md`, not a public issue form.
- Every public claim must link to a reproducible case, current release artifact, current documentation, or actual measured aggregate.
- The 90-day numbers are directional goals, never advertised as achieved before measurement.
- Repository settings, labels, Discussions, Topics, push, Release, and deployment require separate explicit authorization.

---

## File Map

- Create `.github/ISSUE_TEMPLATE/installation-help.yml`: zero-beginner setup support.
- Create `.github/ISSUE_TEMPLATE/case-study.yml`: authorization-aware case metadata submission.
- Create `.github/ISSUE_TEMPLATE/reproduction.yml`: sanitized reproducible failure report.
- Modify `.github/ISSUE_TEMPLATE/config.yml`: security and Discussions contact links.
- Modify `.github/ISSUE_TEMPLATE/bug.yml`, false-positive/negative forms: bilingual privacy reminders and reproduction fields.
- Modify `pyproject.toml`: add `PyYAML>=6` to the development extra only for repository YAML validation.
- Modify `CONTRIBUTING.md`: quick contributions, case review, maintainer response contract.
- Create `scripts/collect_growth_snapshot.py` and `tests/scripts/test_collect_growth_snapshot.py`.
- Create `docs/growth/measurement-schema.md`, `90-day-calendar.md`, `launch-checklist.md`, `channel-copy.md`, and `weekly-review.md`.
- Modify `docs/release-checklist.md` and `CHANGELOG.md` only when a release version is selected.

### Task 1: Add structured community and case-submission forms

**Files:**
- Create: `.github/ISSUE_TEMPLATE/installation-help.yml`
- Create: `.github/ISSUE_TEMPLATE/case-study.yml`
- Create: `.github/ISSUE_TEMPLATE/reproduction.yml`
- Modify: `.github/ISSUE_TEMPLATE/config.yml`
- Modify: `.github/ISSUE_TEMPLATE/bug.yml`
- Modify: `.github/ISSUE_TEMPLATE/false-positive.yml`
- Modify: `.github/ISSUE_TEMPLATE/false-negative.yml`
- Modify: `CONTRIBUTING.md`
- Modify: `pyproject.toml`
- Test: `tests/test_repository_community_files.py`

**Interfaces:**
- Produces: labels `installation`, `needs reproduction`, and `case study` in form metadata; human-readable required consent fields.
- Consumes: GitHub Issue Forms YAML schema and existing security/contribution documents.

- [ ] **Step 1: Write failing repository-form tests**

```python
def test_case_form_requires_authorization_and_forbids_automatic_upload(repo_root: Path):
    form = yaml.safe_load(
        (repo_root / ".github/ISSUE_TEMPLATE/case-study.yml").read_text("utf-8")
    )
    ids = {item.get("id") for item in form["body"]}
    assert {"provenance", "authorization", "privacy_review", "reproduction"} <= ids
    text = json.dumps(form, ensure_ascii=False)
    assert "VideoScope does not upload your video" in text
    assert "VideoScope 不会上传你的视频" in text


def test_security_contact_never_opens_a_public_issue(repo_root: Path):
    config = yaml.safe_load(
        (repo_root / ".github/ISSUE_TEMPLATE/config.yml").read_text("utf-8")
    )
    security = next(
        item for item in config["contact_links"] if item["name"] == "Security report"
    )
    assert security["url"].endswith("/security/policy")
```

Add `PyYAML>=6` under `[project.optional-dependencies].dev`; do not add it to base, AI, OCR, ASR, or Web extras.

- [ ] **Step 2: Run the tests and confirm forms are missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/test_repository_community_files.py -q`

Expected: FAIL because the three forms do not exist.

- [ ] **Step 3: Add the installation form**

Required fields: locale, Windows version, installer version, connector status (`not installed | not started | waiting for pairing | paired | degraded`), FFmpeg/ffprobe status, exact step, sanitized error, and confirmation that no API key/private path/video is attached. Include a link to `docs/windows-install.md`.

- [ ] **Step 4: Add the case form**

```yaml
name: Authorized case study / 授权案例
description: Submit sanitized metadata for a reviewed VideoScope result; video is never uploaded automatically.
title: "[Case] "
labels: ["case study", "needs reproduction"]
body:
  - type: dropdown
    id: provenance
    attributes:
      label: Provenance / 来源
      options: ["project-authored", "user-authorized"]
    validations: { required: true }
  - type: checkboxes
    id: authorization
    attributes:
      label: Authorization / 授权
      options:
        - label: I own or am authorized to publish every submitted item. / 我拥有或获准公开所有提交内容。
          required: true
```

Also require visible symptom, exact VideoScope version, actions, verification status, limitations, reproduction, and local package checksum. File attachment remains a separate manual GitHub action.

- [ ] **Step 5: Add the sanitized reproduction form and improve existing forms**

Require minimal reproduction steps, version, platform, FFmpeg version, expected/observed behavior, and a checkbox excluding videos, keys, personal paths, and private frames unless intentionally authorized. Do not request email addresses.

- [ ] **Step 6: Add contribution response expectations**

`CONTRIBUTING.md` documents the three sub-ten-minute entries, expected maintainer triage labels, case review checklist, and that submitting a case neither requires nor earns a Star, reward, or faster fix.

- [ ] **Step 7: Run form tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/test_repository_community_files.py -q`

Expected: PASS and every YAML file parses.

- [ ] **Step 8: Commit community forms**

```powershell
git add .github/ISSUE_TEMPLATE CONTRIBUTING.md pyproject.toml tests/test_repository_community_files.py
git commit -m "docs: add privacy-safe community intake"
```

### Task 2: Build an explicit, zero-telemetry growth snapshot tool

**Files:**
- Create: `scripts/collect_growth_snapshot.py`
- Create: `tests/scripts/test_collect_growth_snapshot.py`
- Create: `docs/growth/measurement-schema.md`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `collect_snapshot(repo: str, runner: GhRunner, now: datetime) -> GrowthSnapshot` and CLI `python scripts/collect_growth_snapshot.py --repo what912/VideoScope --output runs/growth`.
- Consumes: authenticated `gh api` JSON responses only when a maintainer invokes the command.

- [ ] **Step 1: Write failing offline tests with injected responses**

```python
def test_snapshot_uses_only_aggregate_public_counts(fake_gh: FakeGhRunner) -> None:
    snapshot = collect_snapshot("what912/VideoScope", runner=fake_gh, now=FIXED_NOW)
    assert snapshot.repository.stars == 7
    assert snapshot.release.downloads == 12
    assert snapshot.traffic.unique_visitors == 4
    serialized = snapshot.model_dump_json()
    assert "visitor_id" not in serialized
    assert "ip" not in serialized
    assert "email" not in serialized


def test_gh_calls_are_argument_arrays(fake_gh: FakeGhRunner) -> None:
    collect_snapshot("what912/VideoScope", runner=fake_gh, now=FIXED_NOW)
    assert fake_gh.calls == [
        ["gh", "api", "repos/what912/VideoScope"],
        ["gh", "api", "repos/what912/VideoScope/releases"],
        ["gh", "api", "repos/what912/VideoScope/traffic/views"],
        ["gh", "api", "repos/what912/VideoScope/traffic/popular/referrers"],
    ]
```

- [ ] **Step 2: Run tests and confirm the collector is missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/scripts/test_collect_growth_snapshot.py -q`

Expected: FAIL.

- [ ] **Step 3: Define the aggregate schema**

```python
@dataclass(frozen=True, slots=True)
class GrowthSnapshot:
    schema_version: Literal[1]
    collected_at: str
    repository: RepositoryCounts
    release: ReleaseCounts
    traffic: TrafficCounts
    community: CommunityCounts
    caveats: tuple[str, ...]
```

Required caveats: GitHub traffic windows are limited; clone counts may include automation; Stars do not prove successful processing; case/user-result counts require opt-in maintainer records.

- [ ] **Step 4: Implement the safe runner and atomic local write**

```python
def run_gh(argv: Sequence[str]) -> Mapping[str, object] | list[object]:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise GrowthSnapshotError(sanitize_diagnostic(completed.stderr[-2000:]))
    return json.loads(completed.stdout)
```

Write `runs/growth/YYYY-MM-DD.json` through a sibling temporary file and `Path.replace`. The `runs/` directory remains ignored; publishing a snapshot requires manual review and a separate commit.

- [ ] **Step 5: Document field definitions and interpretation**

Define visibility, trial, outcome, recommendation, and community stages. State that confirmed successful processing and authorized cases are opt-in manual counts, not inferred from traffic. Do not calculate a global “success rate” when denominator quality is unknown.

- [ ] **Step 6: Run the offline collector tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/scripts/test_collect_growth_snapshot.py -q`

Expected: PASS without network.

- [ ] **Step 7: Commit the measurement tooling**

```powershell
git add scripts/collect_growth_snapshot.py tests/scripts/test_collect_growth_snapshot.py docs/growth/measurement-schema.md .gitignore
git commit -m "feat: collect opt-in aggregate growth snapshots"
```

### Task 3: Write the 90-day content and response playbook

**Files:**
- Create: `docs/growth/90-day-calendar.md`
- Create: `docs/growth/channel-copy.md`
- Create: `docs/growth/weekly-review.md`
- Test: `tests/test_growth_playbook.py`

**Interfaces:**
- Produces: 12 weekly objectives, six Chinese problem-led topic templates, five international launch assets, and weekly stop/go review rules.
- Consumes: only claims supported by the three case records and current docs.

- [ ] **Step 1: Write failing playbook completeness tests**

```python
def test_calendar_has_twelve_weeks_and_required_gates(repo_root: Path):
    text = (repo_root / "docs/growth/90-day-calendar.md").read_text("utf-8")
    assert len(re.findall(r"^## Week \d+", text, flags=re.MULTILINE)) == 12
    assert "three testers blocked at one step" in text
    assert "pause promotion" in text


def test_channel_copy_contains_no_prohibited_claims(repo_root: Path):
    text = (repo_root / "docs/growth/channel-copy.md").read_text("utf-8")
    assert not re.search(
        r"100%|guaranteed|万能修复|真实准确率", text, flags=re.IGNORECASE
    )
```

- [ ] **Step 2: Run tests and confirm playbooks are missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/test_growth_playbook.py -q`

Expected: FAIL.

- [ ] **Step 3: Write the exact 12-week rhythm**

```text
Weeks 1–2: public funnel, three authored cases, 30-second demo, 3-minute guide
Weeks 3–4: 10–20 zero-beginner tests; pause if three block at the same step
Weeks 5–6: six Chinese problem-led posts and at least one full case review
Weeks 7–8: onboarding/FFmpeg/error/output-location feedback release
Weeks 9–10: English demo, architecture note, Show HN readiness, one channel at a time
Weeks 11–12: two authorized cases, benchmark/engineering review, contributor thanks, transparent retrospective
```

Every week records one product deliverable, one evidence artifact, one support obligation, one metric review, and one stop condition.

- [ ] **Step 4: Write channel-specific copy templates**

Chinese templates cover upload incompatibility, timeline/container issues, measurable dark/flicker/noise/audio problems, unrecoverable lost information, local privacy, and a full success/failure postmortem. English templates cover a 30-second same-range demo, 3-minute Windows setup, GitHub Pages + loopback zero-cost architecture, deterministic detectors/benchmark, and one evidence-led case review.

Each template includes: symptom, exact process, same-range evidence, limitations, free download, examples, source, and one non-coercive GitHub link. Adapt each platform’s format; do not paste identical promotional text everywhere.

- [ ] **Step 5: Write the weekly review form**

Capture aggregate exposure, release downloads, support requests, opt-in completed outcomes, authorized cases, Stars, useful Issues/Discussions, external contributors, blockers, and next experiment. Mark clone counts as noisy and never count them as users.

- [ ] **Step 6: Run playbook tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/test_growth_playbook.py -q`

Expected: PASS with no prohibited claims or missing weeks.

- [ ] **Step 7: Commit the 90-day playbook**

```powershell
git add docs/growth tests/test_growth_playbook.py
git commit -m "docs: define evidence-led ninety-day growth rhythm"
```

### Task 4: Add launch gates and external-mutation runbook

**Files:**
- Create: `docs/growth/launch-checklist.md`
- Modify: `docs/release-checklist.md`
- Test: `tests/test_growth_launch_checklist.py`

**Interfaces:**
- Produces: a pass/manual/blocker checklist and exact commands that are never run without user authorization.
- Consumes: outputs from the first three plans and current GitHub repository state.

- [ ] **Step 1: Write failing checklist tests**

```python
def test_launch_checklist_covers_truth_and_zero_cost(repo_root: Path):
    text = (repo_root / "docs/growth/launch-checklist.md").read_text("utf-8")
    for phrase in (
        "three reproducible project-authored cases",
        "no remote tracking",
        "Windows install/start/pair/process/uninstall",
        "needs_review is not completion",
        "explicit authorization before external mutation",
    ):
        assert phrase in text
```

- [ ] **Step 2: Run tests and confirm the checklist is missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/test_growth_launch_checklist.py -q`

Expected: FAIL.

- [ ] **Step 3: Write pass/manual/blocker sections**

Pass requires: all automated checks, three reproducible cases, valid release links/hashes, bilingual routes, local full journey, exact attribution, no tracking, no private paths, no false success, and clean wheel/site/installer audits. Manual checks include media quality, unsigned Windows warning wording, keyboard/mobile review, case authorization, and channel-specific copy. Blockers include any missing installer asset, broken pairing, failed current-release generation, mismatched comparison range, or unreviewed privacy output.

- [ ] **Step 4: Document but do not execute repository mutations**

After explicit authorization, the maintainer may run:

```powershell
gh label create "installation" --color "1D76DB" --description "Windows install, connector, pairing, FFmpeg"
gh label create "needs reproduction" --color "FBCA04" --description "A sanitized reproduction is required"
gh label create "case study" --color "0E8A16" --description "Authorized, reviewed case metadata"
gh repo edit what912/VideoScope --enable-discussions
gh repo edit what912/VideoScope --add-topic video-repair --add-topic video-quality --add-topic ffmpeg --add-topic local-first --add-topic privacy-tools --add-topic creator-tools --add-topic computer-vision --add-topic video-analysis --add-topic video-processing --add-topic ai-video
```

Discussion categories `Help`, `Ideas`, `Show and Tell`, and `Benchmark` are configured through GitHub’s UI or API only after authorization. Social preview upload, branch push, PR, merge, Release, Pages deployment, Show HN, Product Hunt, Reddit, Bilibili, Xiaohongshu, Douyin, and Zhihu publication are separate manual approvals.

- [ ] **Step 5: Add release-checklist references**

Link the growth checklist from `docs/release-checklist.md`; do not make directional Star targets a release blocker.

- [ ] **Step 6: Run checklist tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/test_growth_launch_checklist.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the launch gates**

```powershell
git add docs/growth/launch-checklist.md docs/release-checklist.md tests/test_growth_launch_checklist.py
git commit -m "docs: gate ethical zero-cost product launch"
```

### Task 5: Run the community-operations review gate

**Files:**
- Review: `.github/ISSUE_TEMPLATE/*`
- Review: `scripts/collect_growth_snapshot.py`
- Review: `docs/growth/*`

**Interfaces:**
- Produces: a local, reviewable operations package; it performs no external mutation.

- [ ] **Step 1: Run all offline growth tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/scripts/test_collect_growth_snapshot.py tests/test_repository_community_files.py tests/test_growth_playbook.py tests/test_growth_launch_checklist.py -q`

Expected: PASS without network.

- [ ] **Step 2: Validate issue forms locally**

Parse every `.github/ISSUE_TEMPLATE/*.yml`; inspect rendered field order against GitHub Issue Forms schema; verify required privacy/authorization checkboxes cannot be bypassed by empty values.

- [ ] **Step 3: Run one explicitly networked snapshot only with approval**

Run after authorization: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe scripts/collect_growth_snapshot.py --repo what912/VideoScope --output runs/growth`

Expected: one local aggregate JSON file. Review it for no identifiers or secrets and keep it uncommitted unless the user separately approves publishing the snapshot.

- [ ] **Step 4: Run repository validation**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts\validate.py
```

Expected: every offline/CPU gate passes.

- [ ] **Step 5: Present the external action packet**

Report proposed labels, Topics, Discussion categories, Social Preview, branch, PR, release/deployment, and each channel publication separately. Include current baseline and caveats. Wait for explicit authorization before any such action.
