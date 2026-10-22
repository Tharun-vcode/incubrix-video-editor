# IncuBrix Prompt-Based Video Editing Engine

A deterministic, source-anchored video-editing engine with a versioned FastAPI
service, local session runner, FFmpeg renderer, and filesystem or S3-compatible
result storage.

## Features

- Word-level transcript model with stable source IDs and speaker/confidence data.
- Prompt-to-edit operations for cuts, keep-only ranges, speed changes, captions,
  mute/bleep effects, filler and silence removal, music, reordering, and history.
- Captions and effects remain anchored to source words through timeline edits.
- Undo, redo, targeted cut restoration, deterministic timeline hashes, and
  idempotent HTTP job submission.
- Background session and render jobs, callback signing, JSON manifests, and
  OpenTimelineIO export.
- MP4/MOV upload and HTTPS URL intake with media limits and probing.
- Mock transcript-sidecar provider for repeatable tests and an optional
  `faster-whisper` provider.

## Architecture

`app/models/timeline.py` owns the validated source-anchored timeline data model.
`app/services/instructions.py` parses supported natural-language commands;
`app/services/timeline_engine.py` applies them deterministically and maintains
edit history. The FastAPI layer in `app/api/application.py` delegates long
operations to the job coordinator, persists jobs and idempotency keys in SQLite,
and writes output through a filesystem or S3-compatible adapter. Media intake
and rendering are isolated under `app/media/`. The local runner uses the same
engine as the API.

Time references use the explicit `current_timeline` policy: after an edit, later
time-based instructions target the resulting timeline. Word-anchored edits
resolve through transcript source timestamps.

## Requirements

- Python 3.11 or newer (the supplied Docker image uses Python 3.13).
- FFmpeg and ffprobe for media intake and actual rendering.
- Docker Compose for the MinIO-backed deployment option.

The mock transcriber expects a JSON transcript sidecar named after the video,
for example `interview.transcript.json` beside `interview.mp4`. The sidecar is a
JSON array of objects with `text`, `start`, and `end`; optional keys are `id`,
`speaker`, and `confidence`. For example:

```json
[
  {"id": "w1", "text": "Hello", "start": 0.2, "end": 0.6, "speaker": "host"}
]
```

## Install and run locally

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

For local development, edit `.env` and set `STORAGE_BACKEND=filesystem`,
`DATA_DIR=data`, `DATABASE_PATH=data/engine.sqlite3`, and
`OUTPUT_DIR=data/outputs`. On Windows, set `FFMPEG_FONT_FILE` in `.env` to a
local font path (for example `C:\Windows\Fonts\arial.ttf`) if caption rendering
reports a missing Fontconfig configuration. Keep the example S3 credentials as
placeholders; they are not needed with filesystem storage. Start the API:

```powershell
python -m uvicorn app.main:app --reload
```

Health endpoints are `/health` and `/ready`. Interactive API documentation is
available at `/api/v1/docs`; the machine-readable OpenAPI document is at
`/api/v1/openapi.json` and the checked-in snapshot is [openapi.yaml](./openapi.yaml).

## Docker Compose (API + MinIO)

Copy `.env.example` to `.env` and replace the local MinIO credentials with
non-production values before starting the stack:

```powershell
docker compose up --build
```

The API is exposed on port 8000 and the MinIO console on port 9001. Compose
persists SQLite and MinIO data in named volumes. Do not expose the example
credentials to an untrusted network.

## Render demo deployment

The checked-in [render.yaml](./render.yaml) describes a free Render web
service. In Render, create a Blueprint from the GitHub repository and apply
`render.yaml`. Render generates revocable demo-token and callback secrets; read
the demo token in the service's Environment page and provide it to reviewers
through the submission form, not in source control.

The free plan sleeps when idle and uses ephemeral storage. Session/job data and
rendered files can disappear when the service restarts, so this configuration
is for a low-cost demo, not the assessment's guarantee that the demo stays
available throughout the review period. Use an always-on plan with a persistent
disk only if you approve its billing in Render.

## API quick start

Every mutating request requires an `Idempotency-Key`. Create a session from a
timeline, poll the returned job, and then submit editing instructions:

```powershell
$timeline = Get-Content .\scenarios\sample_timeline.json -Raw | ConvertFrom-Json
$body = @{ timeline = $timeline } | ConvertTo-Json -Depth 20
$created = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/sessions `
  -Headers @{'Idempotency-Key' = 'demo-create-001'} `
  -ContentType 'application/json' -Body $body
```

Poll `GET /api/v1/jobs/{job_id}` until it reaches `succeeded` or `failed`; the
successful result contains the session ID. Then use
`POST /api/v1/sessions/{session_id}/instructions` with a JSON body such as
`{"instruction":"Cut the first 2 seconds"}` and a new idempotency key. The API
returns a consistent error envelope for validation and operational failures.
When a demo token is configured, send it in the `X-Demo-Token` header. The
interactive docs include an optional `DemoAccessToken` authorization scheme;
use its **Authorize** button with the token value to try protected endpoints.

Common API error codes include `VALIDATION_ERROR`, `HTTP_ERROR`,
`INVALID_SESSION_INPUT`, `IDEMPOTENCY_KEY_REUSED`, `RATE_LIMIT_EXCEEDED`,
`DEMO_ACCESS_REQUIRED`, `UNSUPPORTED_MEDIA_TYPE`, `UPLOAD_TOO_LARGE`,
`EMPTY_UPLOAD`, `SESSION_NOT_FOUND`, `JOB_NOT_FOUND`, `FILE_NOT_FOUND`,
`NOT_READY`, `STORAGE_NOT_READY`, `MEDIA_PROCESSING_FAILED`, and
`INTERNAL_ERROR`.

## Local scripted sessions

A YAML session contains either an inline `timeline`, a `timeline_file`, or a
video path/HTTPS URL and a `steps` list:

```yaml
timeline_file: ../sample_timeline.json
steps:
  - say: Cut the first 2 seconds
  - say: Restore intro
```

Run it with:

```powershell
python -m app.cli.session_runner scenarios/sessions/example.yaml --output output/example
```

For API-backed batches, run the server and use:

```powershell
python -m app.cli.batch_runner scenarios/sessions --api-url http://127.0.0.1:8000
```

The runners write timeline JSON, edit results, and OpenTimelineIO data. A render
step requires an actual source video and working FFmpeg/ffprobe binaries.

## Configuration

Settings are read from environment variables; `.env.example` documents the
available options. Important settings include:

| Variable | Purpose |
| --- | --- |
| `DATA_DIR`, `DATABASE_PATH`, `OUTPUT_DIR` | Local state and output paths |
| `STORAGE_BACKEND` | `filesystem` or `s3` |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | S3/MinIO |
| `TRANSCRIBER` | `mock` or optional `faster-whisper` |
| `MAX_UPLOAD_BYTES`, `MAX_VIDEO_SECONDS`, `MAX_URL_BYTES` | Intake limits |
| `FFMPEG_PATH`, `FFPROBE_PATH` | Media executable paths |
| `FFMPEG_FONT_FILE` | Optional font file for caption rendering; useful with Windows FFmpeg builds that lack a Fontconfig setup |
| `JOB_WORKERS`, `RATE_LIMIT_PER_MINUTE` | API job and request limits |
| `CALLBACK_SECRET` | HMAC key for callback signatures |

Optional transcription dependencies are listed in `requirements-whisper.txt`.
Install that file and set `TRANSCRIBER=faster-whisper` to use the local model;
the first model use may download model data.

## Tests

```powershell
python -m pytest tests -q
```

The suite covers timeline validation and mapping, parsing and editing,
restoration/history, OpenTimelineIO structure, and API behavior. Real rendering
and Docker/MinIO integration require FFmpeg and a running Docker engine,
respectively; see `REPORT.md` for verification status.

The [scenario pack](./scenarios/README.md) contains 15 scripted sessions and
45 expected-status steps. It is run as part of the test suite.

## Assessment documents

- [REPORT.md](./REPORT.md): design, requirement coverage, verification, and
  known limitations.
- [SOURCES.md](./SOURCES.md): third-party references and dependency provenance.
- [AI_USE.md](./AI_USE.md): disclosure of AI-assisted work and human verification.

The assessment submission form contains candidate-specific and deployment
evidence fields. Those must be completed with real candidate information and
measurements; this project does not invent those values or claim a live demo.
