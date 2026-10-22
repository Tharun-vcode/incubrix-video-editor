from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.core.config import Settings
from app.models.timeline import Timeline


class MediaError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_video_path(path: Path) -> None:
    if path.suffix.lower() not in {".mp4", ".mov"}:
        raise MediaError("Only MP4 and MOV video files are accepted.")
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError("The uploaded video is empty or unavailable.")


def probe_video(path: Path, settings: Settings) -> dict[str, Any]:
    validate_video_path(path)
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise MediaError(
            "ffprobe is not installed or FFMPEG_PATH/FFPROBE_PATH is incorrect."
        ) from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip()[:500] or "ffprobe could not read the video."
        raise MediaError(message) from error
    except subprocess.TimeoutExpired as error:
        raise MediaError("Video probing timed out.") from error

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MediaError("ffprobe returned invalid metadata.") from error
    video_stream = next(
        (stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise MediaError("The input does not contain a video stream.")
    try:
        format_duration = float(info.get("format", {}).get("duration") or 0.0)
        video_duration = _optional_float(video_stream.get("duration"))
        duration = format_duration or video_duration
        if duration is None:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise MediaError("The video duration could not be determined.") from error
    if not 0 < duration <= settings.max_video_seconds:
        raise MediaError(
            f"Video duration must be greater than zero and no longer than "
            f"{settings.max_video_seconds} seconds."
        )
    avg_rate = _rate(video_stream.get("avg_frame_rate"))
    real_rate = _rate(video_stream.get("r_frame_rate"))
    audio_stream = next(
        (stream for stream in info.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    video_start = _optional_float(video_stream.get("start_time")) or 0.0
    audio_start = (
        _optional_float(audio_stream.get("start_time")) or 0.0
        if audio_stream is not None else None
    )
    audio_duration = (
        _optional_float(audio_stream.get("duration"))
        if audio_stream is not None else None
    )
    av_start_offset = (
        abs(video_start - audio_start) if audio_start is not None else None
    )
    av_end_offset = (
        abs(
            (video_start + (video_duration or duration))
            - (audio_start + (audio_duration or duration))
        )
        if audio_start is not None else None
    )
    return {
        "duration_s": duration,
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "frame_rate": avg_rate or real_rate,
        "average_frame_rate": avg_rate,
        "nominal_frame_rate": real_rate,
        "variable_frame_rate": bool(
            avg_rate and real_rate and abs(avg_rate - real_rate) > 0.01
        ),
        "has_audio": any(
            stream.get("codec_type") == "audio"
            for stream in info.get("streams", [])
        ),
        "video_codec": video_stream.get("codec_name"),
        "audio_codecs": [
            stream.get("codec_name")
            for stream in info.get("streams", [])
            if stream.get("codec_type") == "audio"
        ],
        "video_duration_s": video_duration or duration,
        "audio_duration_s": audio_duration,
        "video_start_time_s": video_start,
        "audio_start_time_s": audio_start,
        "av_start_offset_s": av_start_offset,
        "av_end_offset_s": av_end_offset,
        "av_offset_s": (
            max(av_start_offset or 0.0, av_end_offset or 0.0)
            if audio_start is not None else None
        ),
        "source_sha256": sha256_file(path),
        "warnings": [],
    }


def _rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", maxsplit=1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "N/A":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def download_video_url(
    url: str,
    destination: Path,
    settings: Settings,
) -> Path:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise MediaError("Video URLs must be public HTTPS URLs without embedded credentials.")
    try:
        addresses = socket.getaddrinfo(parts.hostname, parts.port or 443)
    except socket.gaierror as error:
        raise MediaError("The video URL host could not be resolved.") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise MediaError("Video URLs cannot point to private or local network addresses.")
    request = Request(url, headers={"User-Agent": "IncuBrix-VideoEngine/1.0"})
    try:
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with build_opener(NoRedirect).open(
            request,
            timeout=settings.download_timeout_seconds,
        ) as response:
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != parts.hostname:
                raise MediaError("The video URL redirected to an untrusted host.")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > settings.max_url_bytes:
                raise MediaError("The remote video exceeds the configured size limit.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with destination.open("wb") as output:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > settings.max_url_bytes:
                        raise MediaError("The remote video exceeds the configured size limit.")
                    output.write(block)
    except MediaError:
        raise
    except (OSError, ValueError) as error:
        raise MediaError(f"Could not download video URL: {error}") from error
    validate_video_path(destination)
    return destination


def timeline_from_transcript(
    duration: float,
    transcript: list[dict[str, Any]],
) -> Timeline:
    from app.models.timeline import Word

    words = [
        Word(
            id=str(item.get("id") or f"w{index + 1:06d}"),
            text=str(item["text"]),
            start=float(item["start"]),
            end=float(item["end"]),
            speaker=str(item["speaker"]) if item.get("speaker") is not None else None,
            confidence=float(item.get("confidence", 1.0)),
        )
        for index, item in enumerate(transcript)
    ]
    words.sort(key=lambda word: (word.start, word.end, word.id))
    return Timeline(duration=duration, words=words)
