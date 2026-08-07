# Local Web API

VideoScope provides an optional FastAPI service for local automation and the
local React dashboard. It has no user accounts, cloud storage, external
database, or remote upload integration. API jobs invoke the same
`AnalysisPipeline` used by the CLI.

## Install and start

```text
python -m pip install -e ".[web]"
videoscope serve
```

The default bind address is `127.0.0.1:8765`, matching the fixed loopback
connector address used by the public VideoScope site. Port `0` can still be
requested explicitly when public-site pairing is not needed:

```text
videoscope serve --port 0
```

OpenAPI JSON and the self-contained API reference are local:

- `http://127.0.0.1:PORT/openapi.json`
- `http://127.0.0.1:PORT/docs`

The production dashboard is served from `/` when the front-end has been built
and synced into the Python package. The API reference remains at `/docs`.

The `/docs` page does not load Swagger, fonts, scripts, stylesheets or other
resources from a CDN. Tooling can import `/openapi.json` for a full interactive
experience without changing the server's offline behavior.

Binding to a LAN or public interface is never implicit. A non-loopback address
requires both `--host` and `--allow-network`. VideoScope does not provide
authentication, TLS, or multi-user isolation, so operators should not expose
this development API to untrusted networks.

In the default loopback mode, the application also rejects untrusted `Host`
headers and browser `Origin` headers that are not loopback HTTP(S) origins.
This reduces DNS-rebinding and cross-site request risks; it is not a substitute
for authentication after `--allow-network` broadens the trust boundary.

## Private Advanced AI review for C

After a useful-content job reaches `awaiting_review`, the loopback dashboard may
use these endpoints:

- `POST /api/content/jobs/{job_id}/ai/prepare` — run explicitly configured local
  providers and return a grounded private suggestion batch;
- `GET /api/content/jobs/{job_id}/ai/suggestions` — restore the current batch;
- `PUT /api/content/jobs/{job_id}/ai/review` — save exact decisions;
- `POST /api/content/jobs/{job_id}/ai/apply` — apply accepted chapter/highlight
  ranges through optimistic C revision control.

These routes reject non-loopback clients. Heavy preparation is bounded by the
configured heavy-job semaphore. Batches are bound to the current C revision;
editing the storyboard makes the batch stale. Private AI artifacts are not
served through public content-artifact routes. Applying suggestions neither
renders media nor bypasses private previews or exact-plan confirmation.

This API has no public multi-user authentication or isolation and must not be
exposed as a public AI service. A future service requires separate authentication,
quotas, upload retention, abuse prevention, encrypted storage, deletion
guarantees, privacy terms and operational monitoring.

## Create a job

`POST /api/jobs` uses `multipart/form-data`:

- `video`: required file upload;
- `prompt`: optional text;
- `config`: optional UTF-8 JSON matching `AnalysisConfig`.

Example:

```text
curl -X POST http://127.0.0.1:8765/api/jobs \
  -F "video=@example.mp4;type=video/mp4" \
  -F "prompt=A red car driving through snow" \
  -F 'config={"sample_fps":2.0,"enabled_detectors":["near_black"]}'
```

The response is `202 Accepted` with a random 32-character job ID and links.
The uploaded filename is never used as a filesystem path. Extension and MIME
type checks only generate preliminary warnings; `ffprobe` remains the
authoritative media validation step.

Any `output_directory`, `keep_workspace`, or `bundle_video` values in uploaded
configuration cannot move artifacts outside the job. The server forces a
private job output directory, disables retained workspaces, and does not bundle
another source-video copy.

## Lifecycle and progress

Jobs use these states:

```text
queued -> probing -> sampling -> detecting -> rendering -> completed
                                                        -> failed
                                                        -> cancelled
```

Read state with `GET /api/jobs/{job_id}`. Stream ordered progress with:

```text
GET /api/jobs/{job_id}/events
Accept: text/event-stream
```

Every SSE message has an integer `id`, event type `status`, and JSON data.
Clients can reconnect with `Last-Event-ID` or the `after` query parameter.
The stream closes after delivery of the terminal event.

`DELETE /api/jobs/{job_id}` requests cooperative cancellation for an active
job. Calling it again after the job becomes terminal removes its retained
directory and record. Cancellation is checked between pipeline stages and is
also exposed to detectors through `AnalysisContext`.

## Results

- `GET /api/jobs/{job_id}/report` returns `report.json` after completion.
- `GET /api/jobs/{job_id}/video` streams the retained local source video while
  the job exists, enabling timestamp review without a second upload.
- `GET /api/jobs/{job_id}/artifacts/{path}` returns a report-local artifact,
  such as `report.html` or `evidence/frame.jpg`.

Artifact lookup is restricted to the completed job's artifact root. Absolute
paths, parent traversal, missing files, uploads, temporary workspaces, and
artifacts from unfinished jobs are not served.

## Service endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Service health and active job count |
| GET | `/api/detectors` | Detector manifests and requirements |
| POST | `/api/jobs` | Stream an upload and queue analysis |
| GET | `/api/jobs/{job_id}` | Read job state |
| GET | `/api/jobs/{job_id}/events` | Stream ordered SSE progress |
| GET | `/api/jobs/{job_id}/report` | Read completed JSON report |
| GET | `/api/jobs/{job_id}/video` | Stream the retained source video |
| GET | `/api/jobs/{job_id}/artifacts/{path}` | Read one safe artifact |
| DELETE | `/api/jobs/{job_id}` | Cancel or delete a job |

## Resource and retention policy

Defaults:

- upload limit: 1024 MiB;
- CPU concurrency: 2;
- optional model concurrency: 1;
- terminal job retention: 24 hours;
- job directory: the platform VideoScope application-data directory.

Relevant server options:

```text
videoscope serve \
  --max-upload-mib 2048 \
  --cpu-concurrency 4 \
  --heavy-ai-concurrency 1 \
  --job-ttl-hours 12
```

CPU and optional-model jobs use separate worker pools. This prevents the
default single heavy-model limit from unnecessarily serializing CPU-only jobs.
Model-backed API jobs are enabled only when their detector IDs appear in the
uploaded configuration. Model downloads are non-interactive and disabled.

A background retention loop removes expired terminal jobs. Startup and
explicit cleanup also remove expired orphan directories whose names match the
random job-ID format.

## Security and privacy

- no wildcard CORS middleware is installed;
- default requests require a trusted loopback Host and loopback browser Origin;
- uploads and reports remain local;
- upload size is enforced while copying bounded chunks;
- job and artifact paths are containment-checked after resolution;
- errors are sanitized and do not expose job or input absolute paths;
- the service has no accounts, external database, cloud upload, analytics, or
  remote fonts/scripts;
- uploaded source files are deleted with the job.

The API is intended for a single trusted user on one machine. Loopback binding,
Host checking, and Origin checking are defense in depth, not account
authentication.

## Long Video to Useful Content API

The content API is the local Web adapter for the same confirmation-gated
pipeline used by `videoscope content`. Uploads, transcripts, structural maps,
storyboards and join previews stay in the application-data job directory.
Nothing is sent to a remote service.

Create a job with `POST /api/content/jobs` using multipart fields:

- `video`: required local video;
- `goal`: `faithful_clean`, `chaptered_full`, or `selected_clips`;
- `config_json`: optional JSON containing only `ContentConfig` fields;
- `transcript`: optional local UTF-8 `.srt` or `.vtt` file.

The lifecycle is:

```text
queued -> probing -> mapping -> planning -> awaiting_review
       -> previewing -> ready_to_confirm -> rendering -> verifying
       -> completed | partial | needs_review | failed | cancelled
```

Read the path-free structural map from
`GET /api/content/jobs/{job_id}/map`. Submit exact source-time range edits with
`PUT /api/content/jobs/{job_id}/storyboard`. Every edit includes the current
`expected_revision`; concurrent or stale revisions return `409 Conflict`.
The server assigns deterministic range IDs from the uploaded video hash, range
kind and exact source interval.

After review, `POST /api/content/jobs/{job_id}/previews` creates bounded private
join previews. Then read the immutable plan from
`GET /api/content/jobs/{job_id}/plan`. Execution requires all three values from
that current review: `plan_digest`, `revision`, and the exact ordered
`accepted_action_ids`. A stale digest, partial action set, or replay is rejected.
The UI enumerates a path-safe private preview manifest through
`GET /api/content/jobs/{job_id}/previews`; media bytes remain protected by the
loopback-only preview allowlist.

Private previews are served only through the loopback-only `/previews/{path}`
route and its exact per-job allowlist. The `/artifacts/{path}` route serves only
verified files declared by the confirmed plan, and only after a successful or
partial terminal outcome. It never serves the upload, transcript, evidence,
draft storyboard, private previews, or pending render tree.

Useful-content jobs share the application-wide CPU concurrency budget. Ordered
SSE progress supports `Last-Event-ID` and `after` reconnects. A first `DELETE`
requests cooperative cancellation; deleting a terminal job removes its local
directory. Versioned path-free state supports browser refresh and terminal-job
recovery after restart. An interrupted nonterminal job fails closed and must be
started again because transient media handles are never serialized.

## Publish Ready API

Publish Ready reuses the same loopback-only service and shared safe job storage.
It is a local processing workflow, not a cloud upload or remote transcoding
service. The source upload remains read-only and the pipeline writes a separate
`publish-ready.mp4` only after the client confirms the exact prepared plan.

Available profiles are returned by `GET /api/publish/profiles`:

- `compatible_mp4` preserves the source dimensions;
- `social_vertical_9_16` uses a 1080×1920 canvas;
- `social_horizontal_16_9` uses a 1920×1080 canvas.

The social profiles use scale-and-pad and retain the complete source frame; they
do not crop. Start a job with multipart `video` and `profile_id` fields:

```text
POST /api/publish/jobs
```

The preparation stages inspect the source and create a deterministic plan and a
short local preview. Poll `GET /api/publish/jobs/{job_id}` or subscribe to
`GET /api/publish/jobs/{job_id}/events`. When the state is
`awaiting_confirmation`, read `GET /api/publish/jobs/{job_id}/plan`, review its
actions and preview, then send the unchanged `plan_digest`:

```json
POST /api/publish/jobs/{job_id}/confirm
{"plan_digest":"<digest from the prepared plan>"}
```

A missing, stale, or mismatched digest is rejected. Confirmation can be accepted
only once. The lifecycle is:

```text
created -> inspecting -> planning -> awaiting_confirmation
        -> processing -> verifying -> completed | needs_review | failed | cancelled
```

The exact `preview/publish-preview.mp4` is available after preparation so the
user can review the plan. Other artifacts remain gated until a terminal success
or review state. Completed output includes `publish-ready.mp4`, `cover.jpg`,
`changes.json`, and `technical-report.json`; the technical report records whether
verification is `passed`, `needs_review`, or `failed`. Verification is a
profile-specific technical check, not a claim about artistic quality or permanent
platform compatibility.

`DELETE /api/publish/jobs/{job_id}` requests cancellation while the job is
active and deletes a retained terminal job on a later call. Artifact paths are
resolved inside the job root, use output-relative paths, and reject traversal.
FFmpeg and ffprobe must be installed on the host; no model, GPU, external API, or
network upload is used by Publish Ready.

## Dashboard development

See `docs/frontend.md` for the two-terminal Vite workflow, mock report, tests,
production build, and screenshot procedure.
