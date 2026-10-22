# Scripted assessment sessions

This pack contains 15 runnable sessions and 45 scripted editing instructions.
Each session reads the shared `sample_timeline.json` fixture and records an
expected status for every step. The suite runs all sessions through the same
local runner and engine used for interactive work.

Run one session with:

```powershell
python -m app.cli.session_runner scenarios/sessions/01-history.yaml --output output/01-history
```

Run the regression suite, which executes all 15 sessions, with:

```powershell
python -m pytest tests/test_scenarios.py -q
```

The sample is transcript-only, so it validates editing behavior and export
generation but deliberately contains no `render` step. A real render requires
a source video and FFmpeg/ffprobe.
