from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Any

from app.models.timeline import Caption, MusicTrack, Segment, Timeline, WordEffect
from app.services.instructions import ParsedInstruction, parse_instruction


@dataclass
class EditRecord:
    id: str
    description: str
    operations: list[dict[str, Any]]
    active: bool = True
    request_fingerprint: str = ""


@dataclass
class EngineResponse:
    status: str
    operations: list[dict[str, Any]]
    summary: str
    question: str | None
    options: list[str]
    timeline_hash: str
    duration_s: float
    time_policy: str = "current_timeline"
    edit_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TimelineEngine:
    time_policy = "current_timeline"

    def __init__(self, original: Timeline):
        original.validate()
        self.original = deepcopy(original)
        self.timeline = deepcopy(original)
        self.history: list[EditRecord] = []
        self._undo_states: list[tuple[Timeline, list[EditRecord]]] = []
        self._redo_states: list[tuple[Timeline, list[EditRecord]]] = []
        self._next_edit_number = 1
        self.pending_instruction: str | None = None

    @property
    def timeline_hash(self) -> str:
        payload = json.dumps(
            self.timeline.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def execute(self, instruction: str, answer: str | None = None) -> EngineResponse:
        if self.pending_instruction and answer is None:
            if re.match(r"\s*(?:restore|reinstate)\s+(?:the\s+)?(?:edit|cut)\s+", instruction, re.I):
                self.pending_instruction = None
            else:
                original_instruction = self.pending_instruction
                answer = instruction
                instruction = original_instruction
        request_fingerprint = " ".join(
            f"{instruction.strip()} {answer or ''}".lower().split()
        )
        if any(
            entry.active and entry.request_fingerprint == request_fingerprint
            for entry in self.history
            if request_fingerprint
        ):
            return self._response(
                "applied",
                [],
                "No change: this exact instruction was already applied.",
            )
        parsed = parse_instruction(instruction, self.timeline, answer)
        if parsed.status == "needs_clarification":
            self.pending_instruction = instruction
            return self._response(
                "needs_clarification",
                parsed.operations,
                parsed.summary,
                question=parsed.question,
                options=parsed.options,
            )
        if parsed.status == "rejected":
            self.pending_instruction = None
            return self._response(
                "rejected",
                parsed.operations,
                parsed.reason or "The instruction could not be applied.",
                error=parsed.reason,
            )
        if answer and parsed.operations and parsed.operations[0]["kind"] in {
            "restore_any_cut",
            "restore_content",
        }:
            parsed.operations[0]["answer"] = answer
        if len(parsed.operations) == 1:
            action = parsed.operations[0]["kind"]
            if action in {"undo", "redo"}:
                return self._history_action(action)
        self.pending_instruction = None

        before = self._snapshot()
        try:
            restore_kinds = {
                "restore",
                "restore_intro",
                "restore_content",
                "restore_any_cut",
                "undo_cut",
            }
            if len(parsed.operations) == 1 and parsed.operations[0]["kind"] in restore_kinds:
                restore_result = self._restore_edit(parsed.operations[0])
                if isinstance(restore_result, ParsedInstruction):
                    if restore_result.status == "needs_clarification":
                        self.pending_instruction = instruction
                        return self._response(
                            "needs_clarification",
                            [],
                            restore_result.question or "Which earlier cut should I restore?",
                            question=restore_result.question,
                            options=restore_result.options,
                        )
                    raise ValueError(restore_result.reason or "No earlier edit matched.")
                if restore_result is None:
                    raise ValueError("No matching earlier edit is available to restore.")
                summary = restore_result
                operation_output = parsed.operations
            else:
                candidate = deepcopy(self.timeline)
                for operation in parsed.operations:
                    self._apply_operation(candidate, operation)
                candidate.validate()
                if not candidate.segments:
                    raise ValueError("The edit would remove the entire video.")
                self.timeline = candidate
                added_warnings = [
                    warning for warning in candidate.warnings
                    if warning not in before[0].warnings
                ]
                summary = parsed.summary
                if added_warnings:
                    summary = f"{summary} Warning: {' '.join(added_warnings)}"
                if self.timeline.to_dict() == before[0].to_dict():
                    return self._response(
                        "applied",
                        parsed.operations,
                        "No change: the requested edit is already present.",
                    )
                self._record(
                    parsed.summary,
                    parsed.operations,
                    before,
                    request_fingerprint,
                )
                operation_output = parsed.operations
        except (ValueError, OSError) as error:
            self.timeline, self.history = before
            return self._response(
                "rejected",
                parsed.operations,
                str(error),
                error=str(error),
            )

        if parsed.operations and parsed.operations[0]["kind"] in {
            "restore",
            "restore_intro",
            "restore_content",
            "restore_any_cut",
            "undo_cut",
        }:
            self._record(
                summary,
                operation_output,
                before,
                request_fingerprint,
            )
        return self._response(
            "applied",
            operation_output,
            summary,
            edit_id=self.history[-1].id if self.history else None,
        )

    def _response(
        self,
        status: str,
        operations: list[dict[str, Any]],
        summary: str,
        question: str | None = None,
        options: list[str] | None = None,
        edit_id: str | None = None,
        error: str | None = None,
    ) -> EngineResponse:
        return EngineResponse(
            status=status,
            operations=operations,
            summary=summary,
            question=question,
            options=options or [],
            timeline_hash=self.timeline_hash,
            duration_s=round(self.timeline.current_duration, 6),
            edit_id=edit_id,
            error=error,
        )

    def _snapshot(self) -> tuple[Timeline, list[EditRecord]]:
        return deepcopy(self.timeline), deepcopy(self.history)

    def _record(
        self,
        description: str,
        operations: list[dict[str, Any]],
        previous_state: tuple[Timeline, list[EditRecord]],
        request_fingerprint: str,
    ) -> None:
        self._undo_states.append(previous_state)
        self._redo_states.clear()
        edit_id = f"edit-{self._next_edit_number:04d}"
        self._next_edit_number += 1
        self.history.append(
            EditRecord(
                edit_id,
                description,
                deepcopy(operations),
                active=True,
                request_fingerprint=request_fingerprint,
            )
        )

    def _history_action(self, action: str) -> EngineResponse:
        if action == "undo":
            if not self._undo_states:
                return self._response("rejected", [], "There is no edit to undo.")
            self._redo_states.append(self._snapshot())
            self.timeline, self.history = self._undo_states.pop()
            summary = "Undid the most recent instruction."
        else:
            if not self._redo_states:
                return self._response("rejected", [], "There is no edit to redo.")
            self._undo_states.append(self._snapshot())
            self.timeline, self.history = self._redo_states.pop()
            summary = "Redid the most recently undone instruction."
        return self._response("applied", [{"kind": action}], summary)

    def _restore_edit(
        self,
        operation: dict[str, Any],
    ) -> str | ParsedInstruction | None:
        edit_id = operation.get("edit_id")
        if operation["kind"] == "restore_intro":
            matches = [
                entry for entry in self.history
                if entry.active
                and any(
                    op.get("kind") == "cut"
                    and any(start <= 0.01 and end > 0 for start, end in op.get("source_ranges", []))
                    for op in entry.operations
                )
            ]
        elif operation["kind"] == "undo_cut":
            cuts = [
                entry for entry in reversed(self.history)
                if entry.active
                and any(item.get("kind") == "cut" for item in entry.operations)
            ]
            ordinal = operation["ordinal"]
            index = {
                "last": 0,
                "second-to-last": 1,
                "third-to-last": 2,
            }.get(ordinal, 0)
            matches = cuts[index:index + 1]
        elif operation["kind"] == "restore_content":
            requested_ranges = operation.get("source_ranges", [])
            matches = [
                entry for entry in self.history
                if entry.active
                and any(
                    item.get("kind") == "cut"
                    and any(
                        max(requested_start, cut_start)
                        < min(requested_end, cut_end)
                        for requested_start, requested_end in requested_ranges
                        for cut_start, cut_end in item.get("source_ranges", [])
                    )
                    for item in entry.operations
                )
            ]
        elif operation["kind"] == "restore_any_cut":
            matches = [
                entry for entry in self.history
                if entry.active
                and any(item.get("kind") == "cut" for item in entry.operations)
            ]
            if len(matches) > 1:
                answer = operation.get("answer", "")
                id_match = re.search(r"\b(edit-\d+)\b", answer, re.IGNORECASE)
                if id_match:
                    matches = [
                        entry for entry in matches
                        if entry.id.lower() == id_match.group(1).lower()
                    ]
                else:
                    ordinal = re.search(
                        r"\b(first|1st|second|2nd|third|3rd|last)\b",
                        answer,
                        re.IGNORECASE,
                    )
                    if ordinal:
                        index = {
                            "first": 0, "1st": 0,
                            "second": 1, "2nd": 1,
                            "third": 2, "3rd": 2,
                            "last": len(matches) - 1,
                        }[ordinal.group(1).lower()]
                        matches = matches[index:index + 1]
            if len(matches) > 1:
                return ParsedInstruction(
                    "needs_clarification",
                    question="More than one cut is active. Which one should I restore?",
                    options=[f"Restore cut {entry.id}" for entry in matches],
                )
        else:
            matches = [
                entry for entry in self.history
                if entry.id == edit_id and entry.active
            ]
        if len(matches) > 1:
            return ParsedInstruction(
                "needs_clarification",
                question="More than one earlier cut matches. Which edit should I restore?",
                options=[f"Restore edit {entry.id}" for entry in matches],
            )
        if len(matches) != 1:
            return None
        matches[0].active = False
        self._rebuild()
        return f"Restored content by reversing earlier edit {matches[0].id}."

    def _rebuild(self) -> None:
        candidate = deepcopy(self.original)
        for entry in self.history:
            if not entry.active:
                continue
            for operation in entry.operations:
                if operation["kind"] in {
                    "undo",
                    "redo",
                    "restore",
                    "restore_intro",
                    "restore_content",
                    "restore_any_cut",
                    "undo_cut",
                }:
                    continue
                self._apply_operation(candidate, operation)
        candidate.validate()
        self.timeline = candidate

    @staticmethod
    def _subtract_ranges(
        timeline: Timeline,
        source_ranges: list[list[float]],
    ) -> None:
        merged = TimelineEngine._merge_ranges(source_ranges)
        output: list[Segment] = []
        for segment in timeline.segments:
            pieces = [(segment.source_start, segment.source_end)]
            for cut_start, cut_end in merged:
                updated: list[tuple[float, float]] = []
                for start, end in pieces:
                    if cut_end <= start or cut_start >= end:
                        updated.append((start, end))
                        continue
                    if start < cut_start:
                        updated.append((start, cut_start))
                    if cut_end < end:
                        updated.append((cut_end, end))
                pieces = updated
            if pieces == [(segment.source_start, segment.source_end)]:
                output.append(segment)
            else:
                output.extend(
                    timeline.new_segment(start, end, segment.speed)
                    for start, end in pieces
                    if end - start > 1e-6
                )
        timeline.segments = output
        timeline.refresh_duration()
        TimelineEngine._remove_orphaned_captions(timeline)

    @staticmethod
    def _snap_cut_ranges(
        timeline: Timeline,
        source_ranges: list[list[float]],
    ) -> list[list[float]]:
        words = sorted(timeline.words, key=lambda word: (word.start, word.end))
        snapped: list[list[float]] = []
        for raw_start, raw_end in source_ranges:
            start, end = float(raw_start), float(raw_end)
            original_start, original_end = start, end
            for index, word in enumerate(words):
                if word.start < start < word.end:
                    previous_end = words[index - 1].end if index else word.start
                    start = (
                        previous_end
                        if word.start - previous_end <= 0.5
                        else word.start
                    )
                    break
            for index, word in enumerate(words):
                if word.start < end < word.end:
                    next_start = (
                        words[index + 1].start
                        if index + 1 < len(words)
                        else word.end
                    )
                    end = (
                        next_start
                        if next_start - word.end <= 0.5
                        else word.end
                    )
                    break
            if (start, end) != (original_start, original_end):
                warning = (
                    f"Cut snapped from {original_start:.3f}–{original_end:.3f}s "
                    f"to word-safe boundaries {start:.3f}–{end:.3f}s."
                )
                if warning not in timeline.warnings:
                    timeline.warnings.append(warning)
            snapped.append([start, end])
        return snapped

    @staticmethod
    def _merge_ranges(ranges: list[list[float]]) -> list[tuple[float, float]]:
        cleaned = sorted(
            (float(start), float(end))
            for start, end in ranges
            if math.isfinite(float(start))
            and math.isfinite(float(end))
            and end > start
        )
        merged: list[tuple[float, float]] = []
        for start, end in cleaned:
            if merged and start <= merged[-1][1] + 1e-6:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _remove_orphaned_captions(timeline: Timeline) -> None:
        visible = timeline.word_positions()
        timeline.captions = [
            caption
            for caption in timeline.captions
            if (
                caption.start_word_id is None
                or caption.end_word_id is None
                or any(visible.get(caption.start_word_id, []))
                or any(visible.get(caption.end_word_id, []))
            )
            and (
                caption.source_start is None
                or any(
                    max(caption.source_start, segment.source_start)
                    < min(caption.source_end or 0, segment.source_end)
                    for segment in timeline.segments
                )
            )
        ]

    @staticmethod
    def _split_with_speed(
        timeline: Timeline,
        source_ranges: list[list[float]],
        speed: float,
    ) -> None:
        ranges = TimelineEngine._merge_ranges(source_ranges)
        output: list[Segment] = []
        for segment in timeline.segments:
            boundaries = {segment.source_start, segment.source_end}
            for start, end in ranges:
                if segment.source_start < start < segment.source_end:
                    boundaries.add(start)
                if segment.source_start < end < segment.source_end:
                    boundaries.add(end)
            ordered = sorted(boundaries)
            for start, end in zip(ordered, ordered[1:]):
                midpoint = (start + end) / 2
                new_speed = (
                    speed
                    if any(range_start <= midpoint < range_end for range_start, range_end in ranges)
                    else segment.speed
                )
                if (
                    start == segment.source_start
                    and end == segment.source_end
                    and new_speed == segment.speed
                ):
                    output.append(segment)
                else:
                    output.append(timeline.new_segment(start, end, new_speed))
        timeline.segments = output
        timeline.refresh_duration()

    @staticmethod
    def _apply_operation(timeline: Timeline, operation: dict[str, Any]) -> None:
        kind = operation["kind"]
        if kind == "cut":
            ranges = TimelineEngine._snap_cut_ranges(
                timeline,
                operation["source_ranges"],
            )
            TimelineEngine._subtract_ranges(timeline, ranges)
        elif kind == "keep_source_ranges":
            ranges = TimelineEngine._merge_ranges(operation["source_ranges"])
            output: list[Segment] = []
            for segment in timeline.segments:
                for start, end in ranges:
                    left = max(start, segment.source_start)
                    right = min(end, segment.source_end)
                    if left < right:
                        output.append(timeline.new_segment(left, right, segment.speed))
            timeline.segments = output
            timeline.refresh_duration()
            TimelineEngine._remove_orphaned_captions(timeline)
        elif kind == "speed":
            TimelineEngine._split_with_speed(
                timeline,
                operation["source_ranges"],
                float(operation["speed"]),
            )
        elif kind == "effect":
            existing = {(effect.kind, effect.word_id) for effect in timeline.effects}
            words_by_id = {word.id: word for word in timeline.words}
            for word_id in operation["word_ids"]:
                if (
                    operation["effect"] == "bleep"
                    and words_by_id[word_id].confidence < 0.55
                ):
                    raise ValueError(
                        f"Word {word_id} has low transcript confidence and cannot be bleeped blindly."
                    )
                key = (operation["effect"], word_id)
                if key not in existing:
                    timeline.effects.append(
                        WordEffect(
                            f"effect-{len(timeline.effects) + 1:04d}",
                            operation["effect"],
                            word_id,
                        )
                    )
                    existing.add(key)
        elif kind == "add_caption":
            if not any(
                caption.start_word_id == operation["start_word_id"]
                and caption.end_word_id == operation["end_word_id"]
                and caption.text == operation["text"]
                for caption in timeline.captions
            ):
                timeline.captions.append(
                    Caption(
                        id=f"caption-{len(timeline.captions) + 1:04d}",
                        text=operation["text"],
                        start_word_id=operation["start_word_id"],
                        end_word_id=operation["end_word_id"],
                    )
                )
        elif kind == "add_overlay":
            timeline.captions.append(
                Caption(
                    id=f"caption-{len(timeline.captions) + 1:04d}",
                    text=operation["text"],
                    source_start=float(operation["source_start"]),
                    source_end=float(operation["source_end"]),
                    kind="text_overlay",
                )
            )
        elif kind == "auto_caption":
            visible_words = timeline.word_positions()
            ordered = sorted(
                (
                    word for word in timeline.words
                    if visible_words.get(word.id)
                ),
                key=lambda word: (word.start, word.end, word.id),
            )
            groups: list[list[Any]] = []
            for word in ordered:
                if (
                    not groups
                    or len(groups[-1]) >= 7
                    or word.start - groups[-1][-1].end > 1.0
                    or word.speaker != groups[-1][-1].speaker
                ):
                    groups.append([word])
                else:
                    groups[-1].append(word)
            for group in groups:
                if not any(
                    caption.start_word_id == group[0].id
                    and caption.end_word_id == group[-1].id
                    for caption in timeline.captions
                ):
                    text = " ".join(word.text for word in group).strip()
                    if text:
                        timeline.captions.append(
                            Caption(
                                f"caption-{len(timeline.captions) + 1:04d}",
                                text,
                                group[0].id,
                                group[-1].id,
                            )
                        )
        elif kind == "remove_fillers":
            tokens = [word.text.lower().strip(".,!?;:") for word in timeline.words]
            filler_ranges: list[list[float]] = []
            nouns_after_like = {
                "people", "person", "thing", "things", "video", "work",
                "money", "food", "music", "time", "someone", "something",
            }
            for index, word in enumerate(timeline.words):
                token = tokens[index]
                filler = token in {"um", "uh", "umm", "uhh"}
                if token == "like":
                    following = tokens[index + 1] if index + 1 < len(tokens) else ""
                    preceding = tokens[index - 1] if index else ""
                    filler = (
                        following not in nouns_after_like
                        and not (
                            preceding in {
                                "i", "you", "we", "they", "he", "she", "it",
                            }
                            and following
                        )
                        and preceding not in {"would", "look", "feel", "seem"}
                    )
                    if not filler:
                        continue
                if filler:
                    filler_ranges.append([word.start, word.end])
            if filler_ranges:
                filler_ids = {
                    word.id
                    for index, word in enumerate(timeline.words)
                    if [word.start, word.end] in filler_ranges
                }
                bleeped = filler_ids.intersection(
                    effect.word_id
                    for effect in timeline.effects
                    if effect.kind == "bleep"
                )
                if bleeped:
                    warning = (
                        "Filler removal also removed bleeped word(s): "
                        + ", ".join(sorted(bleeped))
                        + "."
                    )
                    if warning not in timeline.warnings:
                        timeline.warnings.append(warning)
                TimelineEngine._subtract_ranges(timeline, filler_ranges)
        elif kind == "remove_silence":
            threshold = float(operation["threshold"])
            words = sorted(timeline.words, key=lambda word: (word.start, word.end))
            silence_ranges: list[list[float]] = []
            for left, right in zip(words, words[1:]):
                start, end = left.end, right.start
                if end - start > threshold and start < end:
                    if any(
                        pause_start <= start and end <= pause_end
                        for pause_start, pause_end in timeline.deliberate_pauses
                    ):
                        continue
                    silence_ranges.append([start, end])
            if silence_ranges:
                TimelineEngine._subtract_ranges(timeline, silence_ranges)
        elif kind == "mark_pause":
            pause = (float(operation["source_start"]), float(operation["source_end"]))
            if pause not in timeline.deliberate_pauses:
                timeline.deliberate_pauses.append(pause)
        elif kind == "add_music":
            if not any(track.source == operation["source"] for track in timeline.music_tracks):
                timeline.music_tracks.append(
                    MusicTrack(
                        id=f"music-{len(timeline.music_tracks) + 1:04d}",
                        source=operation["source"],
                        volume=float(operation.get("volume", 0.2)),
                        ducking=True,
                    )
                )
        elif kind == "style_caption":
            caption = next(
                (item for item in timeline.captions if item.id == operation["caption_id"]),
                None,
            )
            if caption is not None:
                if "color" in operation:
                    caption.color = operation["color"]
                if "font_size" in operation:
                    caption.font_size = int(operation["font_size"])
        elif kind == "reorder":
            TimelineEngine._reorder(timeline, operation)
        else:
            raise ValueError(f"unsupported timeline operation: {kind}")

    @staticmethod
    def _reorder(timeline: Timeline, operation: dict[str, Any]) -> None:
        ranges = TimelineEngine._merge_ranges(operation["source_ranges"])
        selected: list[Segment] = []
        remainder: list[Segment] = []
        for segment in timeline.segments:
            pieces = [(segment.source_start, segment.source_end)]
            selected_pieces: list[tuple[float, float]] = []
            for start, end in ranges:
                next_pieces: list[tuple[float, float]] = []
                for left, right in pieces:
                    overlap_start = max(left, start)
                    overlap_end = min(right, end)
                    if overlap_start < overlap_end:
                        selected_pieces.append((overlap_start, overlap_end))
                    if left < overlap_start:
                        next_pieces.append((left, overlap_start))
                    if overlap_end < right:
                        next_pieces.append((overlap_end, right))
                pieces = next_pieces
            remainder.extend(
                timeline.new_segment(start, end, segment.speed)
                for start, end in pieces
                if end - start > 1e-6
            )
            selected.extend(
                timeline.new_segment(start, end, segment.speed)
                for start, end in selected_pieces
                if end - start > 1e-6
            )
        if not selected:
            raise ValueError("The requested section is no longer on the timeline.")
        target_time = float(operation["target_source_time"])
        position = next(
            (
                index
                for index, segment in enumerate(remainder)
                if segment.source_start <= target_time <= segment.source_end
            ),
            None,
        )
        if position is None:
            remainder.extend(selected)
        else:
            target = remainder[position]
            before = target_time - target.source_start
            if before <= 1e-6:
                insertion = position
            elif target.source_end - target_time <= 1e-6:
                insertion = position + 1
            else:
                remainder[position:position + 1] = [
                    timeline.new_segment(target.source_start, target_time, target.speed),
                    timeline.new_segment(target_time, target.source_end, target.speed),
                ]
                insertion = position + 1
            if not operation.get("before", True):
                insertion += 1
            remainder[insertion:insertion] = selected
        timeline.segments = remainder
        timeline.refresh_duration()

    def manifest(
        self,
        provider_name: str = "mock",
        provider_version: str = "1",
        seed: int = 0,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "timeline_hash": self.timeline_hash,
            "models": [{"provider": provider_name, "version": provider_version}],
            "settings": {"time_policy": self.time_policy},
            "seed": seed,
            "timings_ms": {},
            "cost_estimate_usd": 0.0,
            "warnings": list(dict.fromkeys(self.timeline.warnings + (warnings or []))),
            "licence_and_rights": {
                "media": "User-provided media; caller must hold required rights.",
                "generated_media": "No generated media in the deterministic mock path.",
            },
        }

    def export_state(self) -> dict[str, Any]:
        def serialize_history(entries: list[EditRecord]) -> list[dict[str, Any]]:
            return [asdict(entry) for entry in entries]

        def serialize_snapshots(
            snapshots: list[tuple[Timeline, list[EditRecord]]]
        ) -> list[dict[str, Any]]:
            return [
                {
                    "timeline": timeline.to_dict(),
                    "history": serialize_history(history),
                }
                for timeline, history in snapshots
            ]

        return {
            "original": self.original.to_dict(),
            "timeline": self.timeline.to_dict(),
            "history": serialize_history(self.history),
            "undo_states": serialize_snapshots(self._undo_states),
            "redo_states": serialize_snapshots(self._redo_states),
            "next_edit_number": self._next_edit_number,
            "pending_instruction": self.pending_instruction,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> TimelineEngine:
        engine = cls(Timeline.from_dict(state["original"]))

        def deserialize_history(entries: list[dict[str, Any]]) -> list[EditRecord]:
            return [EditRecord(**entry) for entry in entries]

        def deserialize_snapshots(
            snapshots: list[dict[str, Any]]
        ) -> list[tuple[Timeline, list[EditRecord]]]:
            return [
                (
                    Timeline.from_dict(item["timeline"]),
                    deserialize_history(item["history"]),
                )
                for item in snapshots
            ]

        engine.timeline = Timeline.from_dict(state["timeline"])
        engine.history = deserialize_history(state.get("history", []))
        engine._undo_states = deserialize_snapshots(state.get("undo_states", []))
        engine._redo_states = deserialize_snapshots(state.get("redo_states", []))
        engine._next_edit_number = int(
            state.get("next_edit_number", len(engine.history) + 1)
        )
        engine.pending_instruction = state.get("pending_instruction")
        return engine
