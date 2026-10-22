from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import uuid

import yaml

from app.core.config import settings
from app.media.intake import (
    download_video_url,
    probe_video,
    sha256_file,
    timeline_from_transcript,
)
from app.media.render import FFmpegRenderer
from app.providers.transcription import create_transcriber
from app.models.timeline import Timeline
from app.services.otio_export import export_otio
from app.services.timeline_engine import TimelineEngine

def _read_timeline(session_data: dict[str, Any], session_path: Path) -> Timeline | None:
    inline = session_data.get("timeline")
    if isinstance(inline, dict):
        return Timeline.from_dict(inline)
    timeline_file = session_data.get("timeline_file")
    if timeline_file:
        source = (session_path.parent / timeline_file).resolve()
        payload = json.loads(source.read_text(encoding="utf-8"))
        return Timeline.from_dict(payload)
    return None


def _load_media(
    session_data: dict[str, Any],
    session_path: Path,
    job_dir: Path,
) -> tuple[Path | None, dict[str, Any], Timeline | None]:
    timeline = _read_timeline(session_data, session_path)
    if timeline is not None:
        return None, {}, timeline
    video = session_data.get("video")
    if not video:
        raise ValueError("Session YAML must provide video or timeline/timeline_file.")
    parts = urlsplit(str(video))
    local_path = (session_path.parent / str(video)).resolve()
    if local_path.is_file():
        media_path = local_path
    elif parts.scheme == "https":
        media_path = download_video_url(
            str(video),
            job_dir / "source.mp4",
            settings,
        )
    elif parts.scheme in {"http", "ftp"}:
        raise ValueError("Remote videos must use HTTPS.")
    else:
        raise FileNotFoundError(f"Session video was not found: {local_path}")
    media_info = probe_video(media_path, settings)
    provider = create_transcriber(settings)
    words = provider.transcribe(media_path)
    timeline = timeline_from_transcript(
        media_info["duration_s"],
        [
            {
                "id": word.id,
                "text": word.text,
                "start": word.start,
                "end": word.end,
                "speaker": word.speaker,
                "confidence": word.confidence,
            }
            for word in words
        ],
    )
    timeline.warnings.extend(media_info["warnings"])
    return media_path, media_info, timeline


def run_session(session_file: Path, output_dir: Path) -> dict[str, Any]:
    session_file = session_file.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_load(session_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
        raise ValueError("Session YAML must contain a steps list.")
    media_path, media_info, timeline = _load_media(
        payload,
        session_file,
        output_dir,
    )
    if timeline is None:
        raise ValueError("Session input did not produce a timeline.")
    engine = TimelineEngine(timeline)
    initial_hash = media_info.get("source_sha256")
    results: list[dict[str, Any]] = []
    final_video: str | None = None
    for index, step in enumerate(payload["steps"], start=1):
        if not isinstance(step, dict) or not isinstance(step.get("say"), str):
            raise ValueError(f"Session step {index} must contain a say string.")
        instruction = step["say"]
        if instruction.strip().lower() == "render":
            if media_path is None:
                rendered = {
                    "status": "rejected",
                    "operations": [],
                    "summary": "This session has no source video to render.",
                    "question": None,
                    "options": [],
                    "timeline_hash": engine.timeline_hash,
                    "duration_s": round(engine.timeline.current_duration, 6),
                    "time_policy": engine.time_policy,
                    "error": "This session has no source video to render.",
                }
            else:
                try:
                    render_info = FFmpegRenderer(settings).render(
                        media_path,
                        engine.timeline,
                        output_dir / "final.mp4",
                        has_audio=bool(media_info.get("has_audio")),
                    )
                    final_video = render_info["path"]
                    rendered = {
                        "status": "applied",
                        "operations": [{"kind": "render"}],
                        "summary": "Rendered video and audio from the current timeline.",
                        "question": None,
                        "options": [],
                        "timeline_hash": engine.timeline_hash,
                        "duration_s": render_info["duration_s"],
                        "time_policy": engine.time_policy,
                        "render": render_info,
                    }
                except (OSError, ValueError) as error:
                    rendered = {
                        "status": "rejected",
                        "operations": [],
                        "summary": str(error),
                        "question": None,
                        "options": [],
                        "timeline_hash": engine.timeline_hash,
                        "duration_s": round(engine.timeline.current_duration, 6),
                        "time_policy": engine.time_policy,
                        "error": str(error),
                    }
            response = rendered
        else:
            response = engine.execute(instruction, step.get("answer")).to_dict()
        results.append(
            {
                "step": index,
                "instruction": instruction,
                "answer": step.get("answer"),
                **response,
            }
        )

    final_timeline = engine.timeline.to_dict()
    (output_dir / "timeline.json").write_text(
        json.dumps(final_timeline, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    frame_rate = float(media_info.get("frame_rate") or 30.0)
    (output_dir / "timeline.otio").write_text(
        json.dumps(
            export_otio(engine.timeline, media_path, frame_rate),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = {
        "session_id": str(uuid.uuid4()),
        "steps": results,
        "timeline_hash": engine.timeline_hash,
        "duration_s": engine.timeline.current_duration,
        "final_timeline": final_timeline,
        "final_video": final_video,
        "source_sha256_before": initial_hash,
        "source_sha256_after": (
            sha256_file(media_path)
            if media_path is not None else None
        ),
        "manifest": engine.manifest(),
    }
    (output_dir / "results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a deterministic YAML video-editing session locally."
    )
    parser.add_argument("session", type=Path, help="Path to a scripted session YAML file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("session-output"),
        help="Directory for results.json, timeline.json, and rendered media",
    )
    arguments = parser.parse_args()
    result = run_session(arguments.session, arguments.output)
    print(json.dumps({
        "results": str(arguments.output.resolve() / "results.json"),
        "timeline_hash": result["timeline_hash"],
        "duration_s": result["duration_s"],
        "final_video": result["final_video"],
    }, indent=2))


if __name__ == "__main__":
    main()
