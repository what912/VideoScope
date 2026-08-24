# Local Outcome Handoff and Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a completed Rescue into a confirmed Publish Ready job, a trustworthy same-range result comparison, and an explicitly reviewed local share/case package without uploading video.

**Architecture:** The loopback API transfers verified Rescue output to Publish Ready through a pinned descriptor and bounded disk copy, not through browser memory or a user path. The React workbench tracks actual result playback/download locally, renders a privacy-minimal share card, and uses a two-step plan/confirm case-package exporter whose ZIP stays on the user’s computer.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, stdlib `zipfile`, FFmpeg, React 19, TypeScript, Canvas API, Vitest, pytest.

## Global Constraints

- Only a Rescue job with status exactly `completed` can start the one-click Publish Ready handoff.
- `needs_review`, `partial`, `failed`, and `cancelled` are not successful handoff or Star states.
- The handoff source must be an allowlisted, hash-bound Rescue artifact opened through `open_public_artifact`; never accept a user-supplied server path.
- Copy the pinned descriptor in bounded chunks to a fresh Publish job; do not read the full video into Python or browser memory.
- Publish Ready still requires the existing plan preview and exact digest confirmation.
- A Star invitation appears only after `completed` plus a user playback or download event; it is dismissible and never blocks output access.
- A default share card contains no video frame, source filename, absolute path, prompt, transcript, subtitle text, API key, provider request, or private evidence.
- A user-selected thumbnail is optional, must be previewed, and stays local until the user manually shares the downloaded file.
- Case package creation is two-step: prepare private preview, then exact digest confirmation; no GitHub or cloud upload occurs.
- All generated names are stable generic names and all public paths are output-relative.

---

## File Map

- Modify `src/videoscope/web/models.py`: Rescue-to-Publish and case-package request/response models.
- Modify `src/videoscope/web/publish_jobs.py`: pinned-descriptor import method.
- Modify `src/videoscope/web/app.py`: local handoff and case-package endpoints.
- Create `src/videoscope/community/__init__.py`, `models.py`, and `case_package.py`: sanitized package plan/confirm/export domain.
- Create `tests/web/test_rescue_publish_handoff.py` and `tests/community/test_case_package.py`.
- Modify `web/src/types.ts`, `web/src/api.ts`, and their tests.
- Create `web/src/components/PublishHandoffPanel.tsx` and test.
- Modify `web/src/components/RescueResult.tsx`, `RescueView.tsx`, `PublishReadyView.tsx`, and `App.tsx`.
- Create `web/src/components/OutcomeComparison.tsx`, `StarInvitation.tsx`, and tests.
- Create `web/src/outcomeEngagement.ts` and test.
- Create `web/src/outcomeShare.ts`, `components/OutcomeShareDialog.tsx`, and tests.
- Create `web/src/components/CasePackageDialog.tsx` and test.
- Modify `web/src/rescueI18n.ts`, its parity test, and `web/src/styles.css`.
- Rebuild `src/videoscope/web/static` from the verified `web` production build.

### Task 1: Add a descriptor-safe Rescue → Publish handoff API

**Files:**
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/publish_jobs.py`
- Modify: `src/videoscope/web/app.py`
- Test: `tests/web/test_rescue_publish_handoff.py`

**Interfaces:**
- Consumes: `RescueJobManager.open_public_artifact(job_id, relative_path) -> PinnedRescueArtifact`.
- Produces: `PublishJobManager.import_pinned_source(...) -> PublishJobResponse` and `POST /api/rescue/jobs/{job_id}/publish`.

- [ ] **Step 1: Write failing API security and lifecycle tests**

```python
@pytest.mark.parametrize("status", ["needs_review", "partial", "failed", "cancelled"])
def test_handoff_rejects_every_noncompleted_rescue_status(
    client, completed_rescue, status
):
    completed_rescue.finish_for_test(status)
    response = client.post(
        f"/api/rescue/jobs/{completed_rescue.job_id}/publish",
        json={"artifact_role": "faithful", "profile_id": "compatible_mp4"},
    )
    assert response.status_code == 409


def test_handoff_streams_only_the_pinned_verified_artifact(client, completed_rescue):
    response = client.post(
        f"/api/rescue/jobs/{completed_rescue.job_id}/publish",
        json={"artifact_role": "faithful", "profile_id": "compatible_mp4"},
    )
    assert response.status_code == 202
    publish = response.json()
    assert publish["status"] in {"queued", "inspecting"}
    assert completed_rescue.read_entire_artifact_calls == 0
    assert completed_rescue.last_opened_name == "faithful-rescue.mp4"


def test_handoff_rejects_unpublished_or_unknown_roles(client, completed_rescue):
    response = client.post(
        f"/api/rescue/jobs/{completed_rescue.job_id}/publish",
        json={"artifact_role": "../../private", "profile_id": "compatible_mp4"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run the focused tests and confirm the endpoint is missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/web/test_rescue_publish_handoff.py -q`

Expected: FAIL with HTTP 404.

- [ ] **Step 3: Add exact request model**

```python
class RescuePublishHandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_role: Literal["faithful", "improved"] = "faithful"
    profile_id: PublishProfileId = PublishProfileId.COMPATIBLE_MP4
```

- [ ] **Step 4: Add bounded pinned-source import**

```python
def import_pinned_source(
    self,
    *,
    descriptor: int,
    size_bytes: int,
    profile_id: PublishProfileId,
    original_filename: str = "rescued-video.mp4",
) -> PublishJobResponse:
    record = self.reserve_job(
        original_filename=original_filename, profile_id=profile_id
    )
    staging = record.input_path.with_suffix(f"{record.input_path.suffix}.import")
    copied = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with staging.open("xb") as destination:
            while block := os.read(descriptor, 1024 * 1024):
                destination.write(block)
                copied += len(block)
            destination.flush()
            os.fsync(destination.fileno())
        if copied != size_bytes:
            raise PublishJobStateError(
                "Verified Rescue artifact size changed during handoff"
            )
        staging.replace(record.input_path)
        record.update_upload_size(copied)
        return self.submit_prepare(record.job_id)
    except BaseException:
        self.discard_reserved(record.job_id)
        staging.unlink(missing_ok=True)
        raise
```

The caller owns and closes the Rescue descriptor in `finally`. The copy method never accepts a filesystem path.

- [ ] **Step 5: Add the handoff endpoint**

Resolve the requested role against `record.verification.artifacts`, require exact terminal `completed`, call `open_public_artifact`, import it, then close the descriptor. Convert unknown job to 404, lifecycle/artifact mismatch to 409, and unexpected errors to sanitized 500 responses without paths.

- [ ] **Step 6: Run API tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/web/test_rescue_publish_handoff.py tests/web/test_rescue_api.py tests/web/test_publish_api.py -q`

Expected: PASS, including descriptor closure on success and every failure branch.

- [ ] **Step 7: Commit the local handoff API**

```powershell
git add src/videoscope/web tests/web/test_rescue_publish_handoff.py
git commit -m "feat: hand verified Rescue output to Publish Ready"
```

### Task 2: Connect the handoff in the local React workbench

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/api.test.ts`
- Create: `web/src/components/PublishHandoffPanel.tsx`
- Create: `web/src/components/PublishHandoffPanel.test.tsx`
- Modify: `web/src/components/RescueResult.tsx`
- Modify: `web/src/components/RescueView.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/rescueI18n.ts`
- Modify: `web/src/rescueI18n.test.ts`

**Interfaces:**
- Produces: `createPublishJobFromRescue(rescueJobId, artifactRole, profileId) -> Promise<PublishJobResponse>` and callback `onPublishReady(jobId: string): void`.
- Consumes: the endpoint from Task 1 and existing `PublishReadyView(initialJobId)` restoration.

- [ ] **Step 1: Write failing client and component tests**

```ts
it("posts only the role and versioned profile", async () => {
  await createPublishJobFromRescue("a".repeat(32), "faithful", "compatible_mp4", fetcher);
  expect(fetcher).toHaveBeenCalledWith(
    `/api/rescue/jobs/${"a".repeat(32)}/publish`,
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ artifact_role: "faithful", profile_id: "compatible_mp4" }),
    }),
  );
});

it("does not render the handoff for needs_review", () => {
  render(<RescueResult {...props({ status: "needs_review" })} />);
  expect(screen.queryByRole("button", { name: /publish ready|发布就绪/iu })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests and confirm the client is missing**

Run: `cd web; npm test -- src/api.test.ts src/components/PublishHandoffPanel.test.tsx`

Expected: FAIL.

- [ ] **Step 3: Add the typed client**

```ts
export type RescueHandoffArtifactRole = "faithful" | "improved";

export function createPublishJobFromRescue(
  rescueJobId: string,
  artifactRole: RescueHandoffArtifactRole,
  profileId: PublishProfileId,
  fetcher?: typeof fetch,
): Promise<PublishJobResponse> {
  return requestJson(`/rescue/jobs/${encodeURIComponent(rescueJobId)}/publish`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ artifact_role: artifactRole, profile_id: profileId }),
  }, fetcher);
}
```

- [ ] **Step 4: Implement the handoff panel**

The panel appears only for `job.status === "completed"`. It lists only artifact roles actually present in the verified report, loads the existing three Publish profiles, starts a job after explicit button activation, and surfaces sanitized errors. Default role is faithful; default profile is `compatible_mp4`.

- [ ] **Step 5: Switch modes using the returned job ID**

```tsx
const startPublishFromRescue = (jobId: string) => {
  rememberPublishJob(jobId);
  switchMode("publish");
};

<RescueView onPublishReady={startPublishFromRescue} ... />
```

`PublishReadyView` restores the new job and still requires its existing preview and digest confirmation.

- [ ] **Step 6: Run the workbench tests**

Run: `cd web; npm test -- src/api.test.ts src/components/PublishHandoffPanel.test.tsx src/components/RescueView.test.tsx src/App.test.tsx`

Expected: PASS in both locales; no handoff for non-completed states.

- [ ] **Step 7: Commit the continuous journey UI**

```powershell
git add web/src
git commit -m "feat: continue completed Rescue into Publish Ready"
```

### Task 3: Add trustworthy comparison engagement and terminal-only Star invitation

**Files:**
- Create: `web/src/outcomeEngagement.ts`
- Create: `web/src/outcomeEngagement.test.ts`
- Create: `web/src/components/OutcomeComparison.tsx`
- Create: `web/src/components/OutcomeComparison.test.tsx`
- Create: `web/src/components/StarInvitation.tsx`
- Create: `web/src/components/StarInvitation.test.tsx`
- Modify: `web/src/components/RescueResult.tsx`
- Modify: `web/src/components/PublishResult.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**
- Produces: `deriveOutcomeEngagement(status, state)`, `OutcomeComparison`, and `StarInvitation`.
- Consumes: explicit video `play` events and artifact-link click events.

- [ ] **Step 1: Write the failing state matrix**

```ts
it.each([
  ["completed", true, false, true],
  ["completed", false, true, true],
  ["completed", false, false, false],
  ["needs_review", true, true, false],
  ["partial", true, true, false],
  ["failed", true, true, false],
  ["cancelled", true, true, false],
])("derives Star eligibility", (status, played, downloaded, expected) => {
  expect(deriveOutcomeEngagement(status, { played, downloaded, dismissed: false }).starEligible).toBe(expected);
});
```

- [ ] **Step 2: Run the tests and confirm the state module is missing**

Run: `cd web; npm test -- src/outcomeEngagement.test.ts src/components/StarInvitation.test.tsx`

Expected: FAIL.

- [ ] **Step 3: Implement the pure state contract**

```ts
export interface OutcomeEngagementState {
  played: boolean;
  downloaded: boolean;
  dismissed: boolean;
}

export function deriveOutcomeEngagement(
  status: RescueJobStatus | PublishJobStatus,
  state: OutcomeEngagementState,
) {
  return {
    starEligible: status === "completed" && (state.played || state.downloaded) && !state.dismissed,
  } as const;
}
```

Persist dismissal and engagement per job in `sessionStorage`, not analytics or a server.

- [ ] **Step 4: Implement synchronized result comparison**

`OutcomeComparison` accepts `beforeUrl`, `afterUrl`, optional `improvedUrl`, and `onPlayed`. It provides one transport, same timestamp labels, a split/side-by-side switch, verification status text, and explicit “same preview range” copy. It must not claim a full-video comparison when only previews are available.

- [ ] **Step 5: Implement the respectful invitation**

```tsx
<aside aria-label={copy.starTitle} className="star-invitation">
  <p>{copy.starQuestion}</p>
  <a href="https://github.com/what912/VideoScope">{copy.starAction}</a>
  <button type="button" onClick={onDismiss}>{copy.dismiss}</button>
</aside>
```

The invitation contains no modal, countdown, reward, telemetry, or repeated reappearance for the same job.

- [ ] **Step 6: Run component tests**

Run: `cd web; npm test -- src/outcomeEngagement.test.ts src/components/OutcomeComparison.test.tsx src/components/StarInvitation.test.tsx src/components/RescueView.test.tsx src/components/PublishReadyView.test.tsx`

Expected: PASS for the entire terminal-state matrix.

- [ ] **Step 7: Commit comparison and invitation behavior**

```powershell
git add web/src
git commit -m "feat: add trustworthy local outcome comparison"
```

### Task 4: Generate a privacy-minimal share card locally

**Files:**
- Create: `web/src/outcomeShare.ts`
- Create: `web/src/outcomeShare.test.ts`
- Create: `web/src/components/OutcomeShareDialog.tsx`
- Create: `web/src/components/OutcomeShareDialog.test.tsx`
- Modify: `web/src/components/RescueResult.tsx`
- Modify: `web/src/components/PublishResult.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**
- Produces: `buildOutcomeShareModel(input): OutcomeShareModel` and `renderOutcomeCard(model, canvas, thumbnail?): Promise<Blob>`.
- Consumes: public verification checks, action kinds, limitations, and optional user-selected thumbnail.

- [ ] **Step 1: Write failing sanitization tests**

```ts
it("omits private fields and media by default", () => {
  const model = buildOutcomeShareModel(privateFixture);
  const json = JSON.stringify(model);
  expect(json).not.toContain(privateFixture.sourceFilename);
  expect(json).not.toContain(privateFixture.absolutePath);
  expect(json).not.toContain(privateFixture.prompt);
  expect(json).not.toContain(privateFixture.apiKey);
  expect(model.thumbnail).toBeNull();
  expect(model.attribution).toBe("Created by what912");
});
```

- [ ] **Step 2: Run tests and confirm the share model is missing**

Run: `cd web; npm test -- src/outcomeShare.test.ts src/components/OutcomeShareDialog.test.tsx`

Expected: FAIL.

- [ ] **Step 3: Implement an allowlist-only model**

```ts
export interface OutcomeShareModel {
  schemaVersion: 1;
  workflow: "Video Rescue" | "Publish Ready";
  outcome: "completed";
  actionLabels: string[];
  verificationLabels: string[];
  limitationLabels: string[];
  sourcePreserved: true;
  projectUrl: "https://github.com/what912/VideoScope";
  attribution: "Created by what912";
  thumbnail: null;
}
```

Build this object field-by-field; never spread a report or job object.

- [ ] **Step 4: Render and preview the card**

Canvas size is 1200×630. Render neutral background, VideoScope wordmark, workflow/outcome text, at most three actions, at most three checks, source-preserved line, project URL, and fixed attribution. If the user explicitly selects a local image, decode and draw it only after displaying the exact final card preview.

- [ ] **Step 5: Download locally**

Create the PNG with `canvas.toBlob`, use `URL.createObjectURL`, trigger a generic `videoscope-result-card.png` download, then revoke the URL. Do not upload or call the Web Share API automatically.

- [ ] **Step 6: Run share tests**

Run: `cd web; npm test -- src/outcomeShare.test.ts src/components/OutcomeShareDialog.test.tsx`

Expected: PASS, including HTML-like text escaping in the accessible preview and no forbidden fields.

- [ ] **Step 7: Commit the local share card**

```powershell
git add web/src
git commit -m "feat: generate privacy-minimal outcome cards"
```

### Task 5: Add two-step local case-package export

**Files:**
- Create: `src/videoscope/community/__init__.py`
- Create: `src/videoscope/community/models.py`
- Create: `src/videoscope/community/case_package.py`
- Test: `tests/community/test_case_package.py`
- Modify: `src/videoscope/web/models.py`
- Modify: `src/videoscope/web/app.py`
- Modify: `web/src/types.ts`
- Modify: `web/src/api.ts`
- Create: `web/src/components/CasePackageDialog.tsx`
- Create: `web/src/components/CasePackageDialog.test.tsx`

**Interfaces:**
- Produces: `CasePackagePlan`, `CasePackageConfirmation`, `CasePackageExporter.prepare(...)`, `confirm(...)`, and loopback API under `/api/community/case-packages`.
- Consumes: one completed verified Rescue or Publish artifact, a bounded range, bilingual user-authored summary, and explicit include-video choice.

- [ ] **Step 1: Write failing domain tests**

```python
def test_prepare_defaults_to_metadata_only_and_strips_private_fields(tmp_path: Path):
    plan = exporter.prepare(completed_artifact, request(include_video=False))
    assert plan.contents == ("case-metadata.json", "README.txt")
    assert plan.confirmation_required is True
    assert not plan.output_path.exists()
    assert "Users" not in plan.model_dump_json()


def test_confirmation_must_match_exact_digest(tmp_path: Path):
    plan = exporter.prepare(completed_artifact, request(include_video=True))
    with pytest.raises(CasePackageConfirmationError):
        exporter.confirm(plan, CasePackageConfirmation(plan_digest="0" * 64))


def test_video_clip_uses_declared_range_and_argument_array(fake_runner):
    plan = exporter.prepare(
        completed_artifact, request(start=3.0, end=7.0, include_video=True)
    )
    exporter.confirm(plan, CasePackageConfirmation(plan_digest=plan.plan_digest))
    assert fake_runner.last_argv[:2] == ["ffmpeg", "-hide_banner"]
    assert fake_runner.shell is False
    assert "3.0" in fake_runner.last_argv and "4.0" in fake_runner.last_argv
```

- [ ] **Step 2: Run tests and confirm the package domain is missing**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/community/test_case_package.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement strict models**

```python
class CasePackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workflow: Literal["rescue", "publish"]
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    artifact_role: Literal["faithful", "improved", "publish_ready"]
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    include_video: bool = False
    title_en: str = Field(min_length=1, max_length=120)
    title_zh_cn: str = Field(min_length=1, max_length=120)
    summary_en: str = Field(min_length=1, max_length=600)
    summary_zh_cn: str = Field(min_length=1, max_length=600)
    authorization: Literal["project-authored", "user-authorized"]
```

Validate `end_seconds > start_seconds`, maximum public clip length 25 seconds, and source duration bounds.

- [ ] **Step 4: Implement prepare/confirm and ZIP contents**

Prepare creates a private plan with digest and exact content list. Confirm revalidates the source descriptor/hash, optionally renders `authorized-clip.mp4`, writes `case-metadata.json`, `README.txt`, and `SHA256SUMS`, then creates `videoscope-case-package.zip` atomically. The metadata file uses an explicit public-field allowlist and generic artifact names.

- [ ] **Step 5: Add loopback endpoints**

```text
POST /api/community/case-packages
GET  /api/community/case-packages/{package_id}/plan
POST /api/community/case-packages/{package_id}/confirm
GET  /api/community/case-packages/{package_id}/artifact
DELETE /api/community/case-packages/{package_id}
```

All endpoints require loopback. Preparation never writes the final ZIP. Delete removes private preview and final package. No endpoint sends data to GitHub.

- [ ] **Step 6: Add the review-first dialog**

The dialog collects range, bilingual summary, authorization, and optional include-video. After preparation it lists every ZIP member and shows the local clip preview when selected. The confirm checkbox states that the user will manually review and upload only what they authorize. After download, show a link to the Case Submission Issue Form; do not attach files automatically.

- [ ] **Step 7: Run domain, API, and UI tests**

Run: `C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest tests/community/test_case_package.py tests/web -q`

Run: `cd web; npm test -- src/components/CasePackageDialog.test.tsx src/api.test.ts`

Expected: PASS, including traversal, absolute-path, stale-digest, changed-source, oversized-range, and cancellation tests.

- [ ] **Step 8: Commit case-package export**

```powershell
git add src/videoscope/community src/videoscope/web tests/community tests/web web/src
git commit -m "feat: export reviewed local case packages"
```

### Task 6: Package and verify the complete local outcome flow

**Files:**
- Modify: `src/videoscope/web/static/*` through the existing frontend build-copy command.
- Modify: `docs/video-rescue-guide.md`
- Modify: `docs/publish-ready.md`
- Modify: `docs/safe-sharing.md`

**Interfaces:**
- Produces: a distributable loopback workbench containing the exact tested `web` build.

- [ ] **Step 1: Run all frontend checks**

Run: `cd web; npm run lint`

Run: `cd web; npm run typecheck`

Run: `cd web; npm test`

Run: `cd web; npm run build`

Expected: every command passes with no TypeScript `any` additions and no console errors.

- [ ] **Step 2: Copy only the production build using the repository command**

Run the existing documented build/copy command from `web/package.json` or the packaging script; do not manually copy hashed filenames. Confirm `src/videoscope/web/static/index.html` references only files present in its `assets` directory.

- [ ] **Step 3: Run the real local journey**

Start the loopback connector, process a generated Rescue fixture to `completed`, use the handoff, review and confirm Publish Ready, play and download the output, generate a default share card, prepare a metadata-only case package, inspect it, confirm it, and delete both local jobs. Confirm no network request beyond the fixed public GitHub link.

- [ ] **Step 4: Update guides with exact boundaries**

Document the handoff, completed-only rule, preview/confirmation, local share-card fields, case-package contents, manual GitHub upload, deletion, and the fact that `needs_review` is not completion.

- [ ] **Step 5: Run repository validation**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe' scripts\validate.py
```

Expected: all Python, site, web, packaging, and isolated Rescue gates pass.

- [ ] **Step 6: Commit packaging and docs**

```powershell
git add src/videoscope/web/static docs
git commit -m "docs: explain verified rescue to publish outcomes"
```
