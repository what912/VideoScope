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

The default bind address is `127.0.0.1`. Port `0` asks the operating system to
choose an available port; Uvicorn prints the selected address. A fixed port can
be selected explicitly:

```text
videoscope serve --port 8765
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

## Dashboard development

See `docs/frontend.md` for the two-terminal Vite workflow, mock report, tests,
production build, and screenshot procedure.
