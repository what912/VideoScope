# Safe Sharing CPU workflow

Safe Sharing is an opt-in, local CPU workflow that helps a person prepare a
separate sharing copy of a video. It removes selected metadata, proposes
reviewable visual regions, applies only decisions the user accepted, and then
runs independent checks on the new copy. The source video is read-only.

Safe Sharing does **not** identify people, guarantee anonymity, or certify that
a video is safe for every audience. Face, QR/barcode, and optional OCR results
are heuristic proposals. Scanner failure is never treated as proof that no risk
exists: the final result becomes `needs_review` whenever a required check cannot
be completed.

## Artifact boundary

Each output directory has two physical roots:

- `privacy-review-private/` contains the risk map, decisions, plan, extracted
  review frames, and optional preview. It can contain sensitive evidence and
  must not be shared.
- `share-package/` contains only allowlisted public artifacts. A successful run
  produces `share-safe.mp4`, `changes.json`, `privacy-summary.json`,
  `verification.json`, `technical-report.json`, and `manifest.json`.

The native executor can only stage video and `changes.json` in a pending
directory strictly below `privacy-review-private/`; it has no direct publishing
mode. The pipeline independently verifies the candidate and adds every report.
Publication rejects anything except the exact six allowlisted files bound to
the same plan and candidate digest, then performs one atomic directory swap.
Verification or report-generation failure therefore leaves `share-package/`
empty and removes the pending package.

Only the `completed` outcome may atomically publish or authorize downloads from
`share-package/`. The `needs_review`, `partial`, `failed`, and `cancelled`
outcomes retain no candidate or public report in that directory; users must
revise the private review state and run a new confirmation lifecycle.

Public JSON contains output-relative artifact names. It never contains the
source path, workspace path, raw OCR text, private metadata values, or private
evidence. Delete the entire output directory when the private review material is
no longer needed.

## Command-line workflow

The lifecycle is deliberately split into scan, review, exact confirmation, and
verification. No content-changing full execution occurs before the exact plan
digest is supplied.

### 1. Scan locally

```powershell
videoscope privacy input.mp4 --output safe-run --audience public --scan-only
```

Review `safe-run/privacy-review-private/risk-map.json`. If an optional scanner
was unavailable, perform the stated manual review rather than assuming the
video is clear.

### 2. Record human decisions and prepare the plan

Create a UTF-8 JSON decision file. Timestamps require a timezone, and each
`risk_id` must come from the current private risk map.

```json
{
  "reviews": [
    {
      "risk_id": "privacy_risk_REPLACE_WITH_CURRENT_64_HEX_ID",
      "decision": "redact",
      "style": "blur",
      "edited_box": null,
      "reviewed_at": "2026-08-03T12:00:00+08:00"
    }
  ]
}
```

```powershell
videoscope privacy input.mp4 --output safe-run --review-file review.json
```

The command writes the private immutable plan and prints its SHA-256 digest. It
does not execute the plan. For a private preview, add `--preview-only`; the
stable preview location is
`privacy-review-private/preview/privacy-preview.mp4`.

### 3. Confirm the exact plan

After checking the current private plan and preview, rerun with the exact digest
printed in step 2:

```powershell
videoscope privacy input.mp4 --output safe-run `
  --confirm-digest REPLACE_WITH_EXACT_64_HEX_PLAN_DIGEST
```

A preparation can be consumed only once. A changed source, configuration, risk
decision, box, interval, style, or digest invalidates confirmation. A normal
completion uses exit code `0`; input/configuration errors use `2`; media errors
use `3`; internal or failed-verification errors use `4`; and a created copy that
still needs human review uses `5`. Cancellation uses `130`.

## Configuration

`--config` accepts a UTF-8 JSON `SafeSharingConfig`. CLI `--audience`,
`--enable-ocr`, and `--keep-workspace` explicitly override the corresponding
file values. Built-in audiences are `public`, `work_client`, `school`, `family`,
and `external_ai`.

```json
{
  "audience": "public",
  "sample_fps": 2.0,
  "thumbnail_max_size": 640,
  "scanner_configurations": {},
  "enable_ocr": false,
  "keep_workspace": false,
  "effective_config": {}
}
```

OCR remains optional and local. Safe Sharing never downloads an OCR model
implicitly. If OCR is disabled or unavailable, visible text requires manual
review and verification remains conservative.

## Safety properties and limitations

- External programs are invoked with argument arrays and `shell=False`.
- Source content is hashed before planning and rechecked during execution.
- Resumable sampling workspaces are stored only as private-root-relative
  identities. Absolute paths, parent traversal, symlinks, junctions, and other
  reparse-point escapes are rejected before restoration or cleanup.
- Exact confirmation is claimed with an exclusive filesystem marker and then
  persisted before execution. The marker is shared across pipeline instances
  and processes; contention or a crash after claiming remains fail-closed.
- Confirmation and discard also share one atomic lifecycle-transition gate
  outside the removable private root. A stale process cannot delete an active
  claim or pending package, and confirmation revalidates the persisted plan
  after acquiring the gate before it can execute.
- Extracted frames are sampled once for scanning and removed after a terminal
  result unless `--keep-workspace` is explicit.
- Scanner exceptions are isolated and sanitized; `KeyboardInterrupt` and
  `SystemExit` are not hidden.
- Public artifacts are allowlisted and rescanned for private fields, absolute
  paths, local file URIs, and GPS-like values.
- A passing local check means only that the implemented check passed under the
  recorded version and configuration. Human review remains required for the
  intended audience and context.
