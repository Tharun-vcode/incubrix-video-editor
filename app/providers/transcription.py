from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.media.intake import MediaError
from app.models.timeline import Word


class TranscriptionProvider(Protocol):
    name: str
    version: str

    def transcribe(self, video_path: Path) -> list[Word]: ...


class MockTranscriber:
    name = "mock-sidecar"
    version = "1"

    def transcribe(self, video_path: Path) -> list[Word]:
        sidecar = video_path.with_name(f"{video_path.stem}.transcript.json")
        if not sidecar.is_file():
            raise MediaError(
                "The mock transcription provider requires a matching "
                f"{sidecar.name} transcript sidecar. Configure TRANSCRIBER=faster-whisper "
                "for real audio transcription."
            )
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            items = payload["words"] if isinstance(payload, dict) else payload
            words = [
                Word(
                    id=str(item.get("id") or f"w{index + 1:06d}"),
                    text=str(item["text"]),
                    start=float(item["start"]),
                    end=float(item["end"]),
                    speaker=(
                        str(item["speaker"])
                        if item.get("speaker") is not None
                        else None
                    ),
                    confidence=float(item.get("confidence", 1.0)),
                )
                for index, item in enumerate(items)
            ]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise MediaError(f"Invalid transcript sidecar {sidecar.name}: {error}") from error
        return sorted(words, key=lambda word: (word.start, word.end, word.id))


class FasterWhisperTranscriber:
    name = "faster-whisper"

    def __init__(self, settings: Settings):
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise MediaError(
                "The faster-whisper provider is not installed. Install the "
                "transcription extra or use TRANSCRIBER=mock."
            ) from error
        self._model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
        )
        self.version = self._package_version()

    def transcribe(self, video_path: Path) -> list[Word]:
        try:
            segments, _ = self._model.transcribe(
                str(video_path),
                word_timestamps=True,
                beam_size=1,
                vad_filter=False,
            )
            words: list[Word] = []
            for segment in segments:
                for item in segment.words or []:
                    text = item.word.strip()
                    if not text:
                        continue
                    words.append(
                        Word(
                            id=f"w{len(words) + 1:06d}",
                            text=text,
                            start=float(item.start),
                            end=float(item.end),
                            speaker=None,
                            confidence=max(
                                0.0,
                                min(1.0, float(item.probability or 0.0)),
                            ),
                        )
                    )
        except (RuntimeError, OSError, ValueError) as error:
            raise MediaError(f"Speech transcription failed: {error}") from error
        if not words:
            raise MediaError("The transcription provider returned no timed words.")
        return words

    @staticmethod
    def _package_version() -> str:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("faster-whisper")
        except PackageNotFoundError:
            return "unknown"


def create_transcriber(settings: Settings) -> TranscriptionProvider:
    if settings.transcriber == "mock":
        return MockTranscriber()
    if settings.transcriber == "faster-whisper":
        return FasterWhisperTranscriber(settings)
    raise ValueError(
        f"unsupported TRANSCRIBER {settings.transcriber!r}; "
        "use mock or faster-whisper"
    )
