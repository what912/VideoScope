# Safe Sharing Local Web API

The optional Web API exposes the same `SafeSharingPipeline` used by the CLI.
It does not implement a second scanner, planner, renderer, or verifier. Install
the Web extra and start the loopback-only service:

```powershell
python -m pip install -e ".[web]"
videoscope serve
```

The default host is `127.0.0.1`. Cross-site browser origins are rejected unless
the operator explicitly changes the local server policy. The API does not add a
permissive CORS policy and does not upload media to an external service.

## Lifecycle

```text
queued -> inspecting -> scanning -> awaiting_review -> planning
       -> previewing -> awaiting_confirmation -> processing -> verifying
       -> completed | needs_review | partial | failed | cancelled
```

Progress events have monotonically increasing sequence numbers. Clients may
resume the server-sent event stream with `Last-Event-ID` or `?after=N`. A
restart restores review and confirmation waits through the core pipeline's
persisted state. Interrupted scanning or confirmation is failed closed and is
never submitted automatically a second time.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/privacy/profiles` | List the versioned audience profiles. |
| `POST` | `/api/privacy/jobs` | Stream a video upload and queue a local scan. |
| `GET` | `/api/privacy/jobs/{job_id}` | Read path-free job state. |
| `GET` | `/api/privacy/jobs/{job_id}/events` | Stream ordered SSE events. |
| `GET` | `/api/privacy/jobs/{job_id}/risk-map` | Read the private review map. |
| `PUT` | `/api/privacy/jobs/{job_id}/review` | Store explicit review decisions. |
| `POST` | `/api/privacy/jobs/{job_id}/prepare` | Build the plan and private preview. |
| `GET` | `/api/privacy/jobs/{job_id}/plan` | Read the exact confirmable plan. |
| `POST` | `/api/privacy/jobs/{job_id}/confirm` | Submit the exact plan digest once. |
| `GET` | `/api/privacy/jobs/{job_id}/artifacts/{path}` | Read an allowlisted public artifact. |
| `GET` | `/api/privacy/jobs/{job_id}/private-artifacts/{path}` | Read allowlisted review evidence or the exact preview. |
| `DELETE` | `/api/privacy/jobs/{job_id}` | Cancel active work or delete terminal local data. |

`POST /api/privacy/jobs` is `multipart/form-data` with `video`, `profile_id`,
and optional `enable_ocr=false`. Extensions and MIME types are hints only; the
core pipeline performs the authoritative `ffprobe` validation. Uploads are
written to a temporary sibling and atomically renamed only after the configured
size limit and non-empty checks pass.

Review request bodies use the privacy schema's `PrivacyReviewDecision` list:

```json
{
  "reviews": [
    {
      "risk_id": "privacy_risk_<64 lowercase hex characters>",
      "decision": "redact",
      "style": "blur",
      "edited_box": null,
      "reviewed_at": "2026-08-03T12:00:00+08:00"
    }
  ],
  "manual_visual_regions": [
    {
      "start_seconds": 2.0,
      "end_seconds": 3.0,
      "box": {
        "x_min": 0.25,
        "y_min": 0.2,
        "x_max": 0.75,
        "y_max": 0.8
      },
      "style": "blur"
    }
  ],
  "manual_audio_intervals": [
    {
      "start_seconds": 4.0,
      "end_seconds": 5.5,
      "style": "mute"
    }
  ]
}
```

Both manual lists are optional and default to empty. They are validated against
the probed source duration. Normalized visual boxes must remain inside the frame,
audio intervals must have positive duration, and duplicate deterministic manual
regions are rejected. The server creates manual risk IDs and plan actions from
the source hash and reviewed coordinates; clients do not invent IDs or perform
privacy detection.

After preparation, read the plan and confirm exactly its lowercase SHA-256
digest:

```json
{"plan_digest": "<64 lowercase hex characters>"}
```

A mismatch, duplicate confirmation, or operation in the wrong state returns
`409`. The presence of findings or a `needs_review` result is a workflow result,
not an HTTP transport failure.

## Storage and authorization boundaries

Each job receives a random 32-hex identifier below the platform application
data directory. User filenames never select a directory name. Public and
private material are separate:

```text
<job>/artifacts/privacy-review-private/  # never a public package
<job>/artifacts/share-package/           # confirmed output only
```

The public artifact route accepts only the fixed Safe Sharing package names and
only for `completed` jobs. A `needs_review`, `partial`, `failed`, or `cancelled`
job has no authorized public package and must be reviewed and rerun. The route
cannot traverse into the input or private tree. The private route is not a
filesystem browser: it
allows only `evidence/...` and the exact `preview/privacy-preview.mp4` identity.
It cannot serve the risk-map JSON, review decisions, plan, confirmation claim,
pipeline state, samples, caches, or source input. Private artifact responses and
the private risk-map response include `Cache-Control: no-store`.

All artifact paths are relative, slash-normalized, and containment checked.
Symlinks, Windows junctions/reparse points, absolute paths, `..`, and malformed
segments are rejected. Cleanup unlinks a link-like job entry without following
its target. Error responses omit local absolute paths and do not echo private
OCR evidence.

The upload limit, CPU concurrency, retention period, and cleanup interval use
the existing `WebServerConfig`. Analysis, Publish Ready, and Safe Sharing share
the same application-wide CPU limiter. Safe Sharing does not require FastAPI in
the base installation; these routes are available only with the `web` extra.
