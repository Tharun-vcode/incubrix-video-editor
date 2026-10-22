from __future__ import annotations

from pathlib import Path, PureWindowsPath
import re
from typing import Any
from app.models.timeline import Timeline
from app.services.timeline_engine import TimelineEngine


def export_otio(
    timeline: Timeline,
    media_path: str | Path | None,
    frame_rate: float = 30.0,
) -> dict[str, Any]:
    if frame_rate <= 0:
        raise ValueError("frame_rate must be a positive number")
    if media_path is None:
        target_url = ""
    else:
        media_location = str(media_path)
        if re.match(r"^[A-Za-z]:[\\/]", media_location) or media_location.startswith("\\\\"):
            target_url = PureWindowsPath(media_location).as_uri()
        else:
            target_url = Path(media_location).resolve().as_uri()
    available_range = {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": {"OTIO_SCHEMA": "RationalTime.1", "value": 0, "rate": frame_rate},
        "duration": {
            "OTIO_SCHEMA": "RationalTime.1",
            "value": timeline.source_duration * frame_rate,
            "rate": frame_rate,
        },
    }
    media_reference = {
        "OTIO_SCHEMA": "ExternalReference.1",
        "target_url": target_url,
        "available_range": available_range,
        "metadata": {},
    }

    def track(name: str, kind: str) -> dict[str, Any]:
        children: list[dict[str, Any]] = []
        for index, segment in enumerate(timeline.segments):
            source_range = {
                "OTIO_SCHEMA": "TimeRange.1",
                "start_time": {
                    "OTIO_SCHEMA": "RationalTime.1",
                    "value": segment.source_start * frame_rate,
                    "rate": frame_rate,
                },
                "duration": {
                    "OTIO_SCHEMA": "RationalTime.1",
                    "value": (segment.source_end - segment.source_start) * frame_rate,
                    "rate": frame_rate,
                },
            }
            effects = []
            if segment.speed != 1.0:
                effects.append(
                    {
                        "OTIO_SCHEMA": "LinearTimeWarp.1",
                        "name": f"Speed {segment.speed:g}x",
                        "time_scalar": segment.speed,
                        "metadata": {},
                    }
                )
            children.append(
                {
                    "OTIO_SCHEMA": "Clip.2",
                    "name": f"{name} segment {index + 1}",
                    "source_range": source_range,
                    "media_reference": media_reference,
                    "effects": effects,
                    "markers": [],
                    "enabled": True,
                    "metadata": {
                        "source_start": segment.source_start,
                        "source_end": segment.source_end,
                        "speed": segment.speed,
                    },
                }
            )
        return {
            "OTIO_SCHEMA": "Track.1",
            "name": name,
            "kind": kind,
            "children": children,
            "markers": [],
            "enabled": True,
            "metadata": {},
        }

    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": "IncuBrix edited timeline",
        "global_start_time": {
            "OTIO_SCHEMA": "RationalTime.1",
            "value": 0,
            "rate": frame_rate,
        },
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "Tracks",
            "children": [track("Video", "Video"), track("Audio", "Audio")],
            "markers": [],
            "enabled": True,
            "metadata": {},
        },
        "metadata": {
            "timeline_hash": TimelineEngine(timeline).timeline_hash,
            "source_duration_s": timeline.source_duration,
            "edited_duration_s": timeline.current_duration,
            "frame_rate": frame_rate,
        },
    }
