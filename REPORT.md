# Implementation Report

## Summary

This repository implements a deterministic prompt-based video-editing engine.
The timeline is represented as source-time segments; transcript words have
stable IDs, and captions/effects can refer to those IDs. Edits therefore change
the visible timeline without rewriting or losing the original media
coordinates. The API and local runner share the same editing engine.

## Requirement coverage

| Area | Implemented |
| --- | --- |
| Source-anchored timeline and word-level transcript | Validated model, source/current-time mapping, confidence/speaker metadata |
| Prompt interpretation | Deterministic parser for supported time-, transcript-, and effect-based commands; asks for clarification when ambiguous |
| Reversible editing | Undo/redo, edit history, targeted restoration, repeat suppression |
| Media intake and rendering | MP4/MOV validation, probing, transcript sidecars, optional faster-whisper, FFmpeg renderer |
| API | Versioned FastAPI routes, request validation, structured errors, correlation IDs, health/readiness |
| Asynchronous jobs and idempotency | SQLite-persisted jobs and idempotency records; in-process worker pool |
| Storage and exports | Filesystem/S3-compatible object storage, manifests, OpenTimelineIO JSON |
| Developer workflow | Dockerfile/Compose, environment example, local/API batch runners, tests and setup documentation |

## Design choices

- **Current-timeline time policy:** numeric time references are evaluated against
  the current edited timeline. Responses and exports retain this policy.
- **Source anchors:** source timestamps remain the canonical coordinates for
  transcript words and media segments.
- **Deterministic parser:** supported command patterns are implemented locally;
  there is no external LLM call, hidden key, or nondeterministic generation.
- **SQLite and in-process workers:** this keeps a single-instance assessment
  setup easy to run. A multi-instance production service would need a shared
  durable queue and coordination layer.
- **Mock transcription by default:** the sidecar provider is reproducible and
  avoids network/model costs in tests; faster-whisper is optional.

## Verification performed

- `python -m pytest tests -q`: **36 passed** after adding restore-clarification,
  removed-content matching, scripted-session coverage, and OpenAPI snapshot
  validation.
- All 15 scripted sessions (45 expected-status steps) pass through the local
  session runner.
- Determinism check: all 15 scripted sessions were run three times; 15/15 had
  identical final `timeline_hash` values (100%). Details are in the ignored
  local evidence file `output/determinism/summary.json`.
- Installed FFmpeg 9.0.2 and ffprobe 9.0.2 locally. A generated 8-second,
  30-fps test video completed two renders: after a 1-second cut (7-second
  timeline) and after restoring the intro (8-second timeline). Both reported
  0.00 frames of duration error and 0.00 frames of A/V offset. Source SHA-256
  was identical before and after both renders. This is a synthetic-media smoke
  test, not a review of a real interview video or a broad render-accuracy
  benchmark.
- Caption rendering on this Windows FFmpeg build requires `FFMPEG_FONT_FILE`
  because it has no default Fontconfig configuration. The smoke test used the
  system Arial font via this setting.
- Added an optional revocable `X-Demo-Token` access control for public API
  deployments and a Render Blueprint configured for the free plan. The free
  deployment sleeps when idle and has ephemeral storage; it does not satisfy
  the assessment's continuous-availability/live-review requirement.
- API route registration and imports were previously verified in the configured
  Python environment.
- A GitHub Actions Linux run exposed an OpenTimelineIO file-URI path handling
  bug for Windows-style source paths; this has been corrected using
  platform-aware URI conversion. The updated workflow must pass before treating
  the repository CI check as green.
- Started the FastAPI service locally and verified both `/health` and `/ready`
  returned `ok`/`ready`; the smoke-test process was stopped afterward.
- Static diagnostics for the modified parser, engine, and regression test report
  no errors.

## Not verified / limitations

- Rendering on real, user-provided footage and frame/audio accuracy across the
  complete set of edit combinations have not been verified.
- Docker is installed but its daemon is unavailable; Compose and MinIO startup
  were not verified.
- The background job pool is process-local. Persisted jobs are not re-enqueued
  after a process restart, so this is not a multi-instance production queue.
- The parser is intentionally bounded and deterministic; it is not an
  unrestricted natural-language model. Unsupported or ambiguous instructions
  should be clarified/rejected rather than guessed.
- No public demo deployment, candidate-specific submission details, latency
  benchmarks, or external evaluation evidence were supplied. They remain
  unclaimed and must be added only after real execution.

## Assessment evidence

The provided assessment submission workbook should be completed with the
candidate's own details, live deployment information (if created), and measured
results. Do not treat the implementation test count as an editing-quality or
render-accuracy benchmark.
