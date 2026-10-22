from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any


@dataclass
class Word:
    id: str
    text: str
    start: float
    end: float
    speaker: str | None = None
    confidence: float = 1.0

    def validate(self, source_duration: float) -> None:
        if not self.id.strip() or not self.text.strip():
            raise ValueError("word id and text must not be empty")
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError(f"word {self.id} has non-finite timestamps")
        if self.start < 0 or self.end <= self.start or self.end > source_duration:
            raise ValueError(f"word {self.id} has timestamps outside the source video")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError(f"word {self.id} confidence must be between 0 and 1")


@dataclass
class Caption:
    id: str
    text: str
    start_word_id: str | None = None
    end_word_id: str | None = None
    source_start: float | None = None
    source_end: float | None = None
    kind: str = "caption"
    color: str = "white"
    font_size: int = 48


@dataclass
class Segment:
    id: str
    source_start: float
    source_end: float
    speed: float = 1.0

    @property
    def duration(self) -> float:
        return (self.source_end - self.source_start) / self.speed


@dataclass
class WordEffect:
    id: str
    kind: str
    word_id: str
    frequency: float = 1000.0


@dataclass
class MusicTrack:
    id: str
    source: str
    volume: float = 0.2
    ducking: bool = True


@dataclass
class Timeline:
    duration: float
    words: list[Word] = field(default_factory=list)
    captions: list[Caption] = field(default_factory=list)
    source_duration: float | None = None
    segments: list[Segment] = field(default_factory=list)
    effects: list[WordEffect] = field(default_factory=list)
    music_tracks: list[MusicTrack] = field(default_factory=list)
    deliberate_pauses: list[tuple[float, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _next_segment_id: int = field(default=1, repr=False)

    def __post_init__(self) -> None:
        if self.source_duration is None:
            self.source_duration = self.duration
        if not self.segments and self.source_duration > 0:
            self.segments = [Segment("segment-1", 0.0, self.source_duration)]
            self._next_segment_id = 2
        elif self.segments:
            self._next_segment_id = max(
                (self._segment_number(segment.id) for segment in self.segments),
                default=0,
            ) + 1
        self.validate()
        self.refresh_duration()

    @staticmethod
    def _segment_number(segment_id: str) -> int:
        try:
            return int(segment_id.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    @property
    def current_duration(self) -> float:
        return sum(segment.duration for segment in self.segments)

    def refresh_duration(self) -> None:
        self.duration = self.current_duration

    def new_segment(self, source_start: float, source_end: float, speed: float) -> Segment:
        segment = Segment(
            f"segment-{self._next_segment_id}",
            source_start,
            source_end,
            speed,
        )
        self._next_segment_id += 1
        return segment

    def validate(self) -> None:
        source_duration = self.source_duration
        if (
            source_duration is None
            or not math.isfinite(source_duration)
            or source_duration <= 0
        ):
            raise ValueError("source duration must be a positive finite number")
        if len({word.id for word in self.words}) != len(self.words):
            raise ValueError("word ids must be unique")
        word_ids = {word.id for word in self.words}
        for word in self.words:
            word.validate(source_duration)
        if len({caption.id for caption in self.captions}) != len(self.captions):
            raise ValueError("caption ids must be unique")
        for caption in self.captions:
            if not caption.id.strip() or not caption.text.strip():
                raise ValueError("caption id and text must not be empty")
            if caption.kind not in {"caption", "text_overlay"}:
                raise ValueError(f"unsupported caption kind: {caption.kind}")
            if caption.font_size <= 0:
                raise ValueError(f"caption {caption.id} font size must be positive")
            if caption.start_word_id is not None or caption.end_word_id is not None:
                if (
                    caption.start_word_id not in word_ids
                    or caption.end_word_id not in word_ids
                ):
                    raise ValueError(f"caption {caption.id} references an unknown word")
                start_index = next(
                    i for i, word in enumerate(self.words)
                    if word.id == caption.start_word_id
                )
                end_index = next(
                    i for i, word in enumerate(self.words)
                    if word.id == caption.end_word_id
                )
                if start_index > end_index:
                    raise ValueError(f"caption {caption.id} word anchors are reversed")
            elif (
                caption.source_start is None
                or caption.source_end is None
                or not 0 <= caption.source_start < caption.source_end <= source_duration
            ):
                raise ValueError(
                    f"caption {caption.id} requires word anchors or a valid source range"
                )
        for segment in self.segments:
            if (
                not math.isfinite(segment.source_start)
                or not math.isfinite(segment.source_end)
                or not math.isfinite(segment.speed)
                or segment.source_start < 0
                or segment.source_end <= segment.source_start
                or segment.source_end > source_duration
                or segment.speed <= 0
            ):
                raise ValueError(f"segment {segment.id} is invalid")
        for effect in self.effects:
            if effect.kind not in {"mute", "bleep"}:
                raise ValueError(f"unsupported word effect: {effect.kind}")
            if effect.word_id not in word_ids:
                raise ValueError(f"effect {effect.id} references an unknown word")
        for track in self.music_tracks:
            if not track.source.strip() or not 0 <= track.volume <= 1:
                raise ValueError(f"music track {track.id} is invalid")
        for start, end in self.deliberate_pauses:
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or end > source_duration
            ):
                raise ValueError("deliberate pause is outside the source video")

    def current_ranges_to_source(
        self, start: float, end: float
    ) -> list[tuple[int, float, float, float, float]]:
        """Map an edited-time range to source spans and their edited-time offsets."""
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or end > self.current_duration + 1e-6
        ):
            raise ValueError("range is outside the current timeline")
        matches: list[tuple[int, float, float, float, float]] = []
        cursor = 0.0
        for index, segment in enumerate(self.segments):
            segment_end = cursor + segment.duration
            overlap_start = max(start, cursor)
            overlap_end = min(end, segment_end)
            if overlap_start < overlap_end:
                source_start = segment.source_start + (overlap_start - cursor) * segment.speed
                source_end = segment.source_start + (overlap_end - cursor) * segment.speed
                matches.append(
                    (index, source_start, source_end, overlap_start, overlap_end)
                )
            cursor = segment_end
        return matches

    def word_positions(self) -> dict[str, list[tuple[float, float]]]:
        positions: dict[str, list[tuple[float, float]]] = {
            word.id: [] for word in self.words
        }
        cursor = 0.0
        for segment in self.segments:
            for word in self.words:
                start = max(segment.source_start, word.start)
                end = min(segment.source_end, word.end)
                if start < end:
                    edited_start = cursor + (start - segment.source_start) / segment.speed
                    edited_end = cursor + (end - segment.source_start) / segment.speed
                    positions[word.id].append((edited_start, edited_end))
            cursor += segment.duration
        return positions

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "duration": self.current_duration,
            "source_duration": self.source_duration,
            "words": [asdict(word) for word in self.words],
            "captions": [asdict(caption) for caption in self.captions],
            "segments": [asdict(segment) for segment in self.segments],
            "effects": [asdict(effect) for effect in self.effects],
            "music_tracks": [asdict(track) for track in self.music_tracks],
            "deliberate_pauses": [list(item) for item in self.deliberate_pauses],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Timeline:
        source_duration = float(data.get("source_duration", data["duration"]))
        return cls(
            duration=float(data["duration"]),
            source_duration=source_duration,
            words=[Word(**word) for word in data.get("words", [])],
            captions=[Caption(**caption) for caption in data.get("captions", [])],
            segments=[Segment(**segment) for segment in data.get("segments", [])],
            effects=[WordEffect(**effect) for effect in data.get("effects", [])],
            music_tracks=[MusicTrack(**track) for track in data.get("music_tracks", [])],
            deliberate_pauses=[
                (float(start), float(end))
                for start, end in data.get("deliberate_pauses", [])
            ],
            warnings=list(data.get("warnings", [])),
        )
