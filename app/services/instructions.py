from __future__ import annotations

from dataclasses import dataclass, field
import re
import string
from typing import Any

from app.models.timeline import Timeline, Word


@dataclass
class ParsedInstruction:
    status: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    question: str | None = None
    options: list[str] = field(default_factory=list)
    candidates: list[list[str]] = field(default_factory=list)
    reason: str | None = None


def normalize_token(value: str) -> str:
    return value.lower().strip(string.punctuation + "“”‘’")


def _source_ranges_for_word_ids(
    timeline: Timeline, word_ids: list[str]
) -> list[tuple[float, float]]:
    words = {word.id: word for word in timeline.words}
    ranges: list[tuple[float, float]] = []
    for word_id in word_ids:
        word = words[word_id]
        ranges.append((word.start, word.end))
    return ranges


def _occurrences(timeline: Timeline, phrase: str) -> list[list[Word]]:
    query = [normalize_token(token) for token in phrase.split()]
    query = [token for token in query if token]
    if not query:
        return []
    tokens = [normalize_token(word.text) for word in timeline.words]
    aliases = {
        "pricing": {"pricing", "price", "prices", "cost", "costs"},
        "price": {"pricing", "price", "prices", "cost", "costs"},
        "cost": {"pricing", "price", "prices", "cost", "costs"},
    }
    found: list[list[Word]] = []
    for index in range(len(tokens) - len(query) + 1):
        match = True
        for offset, expected in enumerate(query):
            expected_set = aliases.get(expected, {expected})
            if tokens[index + offset] not in expected_set:
                match = False
                break
        if match:
            found.append(timeline.words[index:index + len(query)])
    return found


def _current_occurrence_range(timeline: Timeline, words: list[Word]) -> tuple[float, float]:
    positions = timeline.word_positions()
    starts = [
        start
        for word in words
        for start, _ in positions.get(word.id, [])
    ]
    ends = [
        end
        for word in words
        for _, end in positions.get(word.id, [])
    ]
    if not starts or not ends:
        raise ValueError("the selected words are no longer present in the timeline")
    return min(starts), max(ends)


def _ranges_from_current_time(
    timeline: Timeline, start: float, end: float
) -> list[list[float]]:
    return [
        [source_start, source_end]
        for _, source_start, source_end, _, _ in timeline.current_ranges_to_source(
            start, end
        )
    ]


def parse_time(value: str, duration: float) -> float:
    text = value.strip().lower()
    if text in {"start", "beginning", "begin"}:
        return 0.0
    if text in {"end", "ending"}:
        return duration
    clock = re.fullmatch(r"(?:(\d+):)?(\d{1,2})(?:\.(\d+))?", text)
    if clock and ":" in text:
        minutes = int(clock.group(1) or 0)
        seconds = int(clock.group(2))
        if seconds >= 60:
            raise ValueError(f"could not understand time value '{value}'")
        fraction = float(f"0.{clock.group(3)}") if clock.group(3) else 0.0
        return minutes * 60 + seconds + fraction
    seconds_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)", text)
    if seconds_match:
        return float(seconds_match.group(1))
    minute_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes?)", text)
    if minute_match:
        return float(minute_match.group(1)) * 60
    colloquial = re.fullmatch(
        r"(?:a|one)?\s*minute(?:\s+(?:and\s+)?(\w+))?(?:\s+seconds?)?", text
    )
    if colloquial:
        tail = colloquial.group(1)
        number_words = {
            "zero": 0,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
            "sixteen": 16,
            "seventeen": 17,
            "eighteen": 18,
            "nineteen": 19,
            "twenty": 20,
            "thirty": 30,
            "forty": 40,
            "fifty": 50,
        }
        if tail is None:
            seconds = 0
        elif tail.isdigit():
            seconds = int(tail)
        elif tail in number_words:
            seconds = number_words[tail]
        elif "-" in tail:
            tens, ones = tail.split("-", maxsplit=1)
            if tens not in number_words or ones not in number_words:
                raise ValueError(f"could not understand time value '{value}'")
            seconds = number_words[tens] + number_words[ones]
        else:
            raise ValueError(f"could not understand time value '{value}'")
        if seconds >= 60:
            raise ValueError(f"could not understand time value '{value}'")
        return 60 + seconds
    bare = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if bare:
        return float(text)
    raise ValueError(f"could not understand time value '{value}'")


def _range_from_text(text: str, duration: float) -> tuple[float, float] | None:
    last = re.search(
        r"(?:the\s+)?last\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)",
        text,
        re.IGNORECASE,
    )
    if last:
        length = parse_time(f"{last.group(1)} {last.group(2)}", duration)
        return max(0.0, duration - length), duration
    first = re.search(
        r"(?:the\s+)?first\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)",
        text,
        re.IGNORECASE,
    )
    if first:
        length = parse_time(f"{first.group(1)} {first.group(2)}", duration)
        return 0.0, length
    match = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?=\s+by\b|$)", text, re.IGNORECASE)
    if match:
        return (
            parse_time(match.group(1), duration),
            parse_time(match.group(2), duration),
        )
    match = re.search(
        r"\b(?:between|from)\s+(\d+(?::\d{1,2})?)\s*(?:-|to|and)\s*"
        r"(\d+(?::\d{1,2})?)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return parse_time(match.group(1), duration), parse_time(match.group(2), duration)
    match = re.search(
        r"\b(\d+(?::\d{1,2})?)\s*-\s*(\d+(?::\d{1,2})?)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return parse_time(match.group(1), duration), parse_time(match.group(2), duration)
    return None


def _candidate_description(timeline: Timeline, words: list[Word], index: int) -> str:
    positions = timeline.word_positions()
    visible = [
        (start, end)
        for word in words
        for start, end in positions.get(word.id, [])
    ]
    if visible:
        start = min(interval[0] for interval in visible)
        end = max(interval[1] for interval in visible)
    else:
        start = min(word.start for word in words)
        end = max(word.end for word in words)
    phrase = " ".join(word.text for word in words)
    return f"{index + 1}. “{phrase}” at {start:.2f}–{end:.2f}s"


def _select_occurrence(
    timeline: Timeline,
    occurrences: list[list[Word]],
    intent: str,
    answer: str | None,
) -> ParsedInstruction | list[Word]:
    if not occurrences:
        return ParsedInstruction(
            "rejected",
            reason=f"No matching words were found for '{intent}'.",
        )
    if len(occurrences) == 1:
        return occurrences[0]
    if answer:
        ordinal = re.search(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|last|all)\b", answer, re.I)
        if ordinal:
            choice = ordinal.group(1).lower()
            if choice == "all":
                return ParsedInstruction(
                    "applied",
                    operations=[{
                        "kind": "cut",
                        "source_ranges": [
                            [word.start, word.end]
                            for group in occurrences
                            for word in group
                        ],
                    }],
                    summary=f"Selected all {len(occurrences)} matching occurrences.",
                )
            index = {
                "first": 0, "1st": 0,
                "second": 1, "2nd": 1,
                "third": 2, "3rd": 2,
                "fourth": 3, "4th": 3,
                "last": len(occurrences) - 1,
            }[choice]
            if index < len(occurrences):
                return occurrences[index]
    descriptions = [
        _candidate_description(timeline, words, index)
        for index, words in enumerate(occurrences)
    ]
    return ParsedInstruction(
        "needs_clarification",
        question=f"'{intent}' matches {len(occurrences)} places. Which one should I use?",
        options=descriptions + ["All matches"],
        candidates=[[word.id for word in group] for group in occurrences],
    )


def parse_instruction(
    text: str,
    timeline: Timeline,
    answer: str | None = None,
) -> ParsedInstruction:
    raw = text.strip()
    lowered = raw.lower()
    if not raw:
        return ParsedInstruction("rejected", reason="Instruction must not be empty.")

    clauses = [
        clause.strip()
        for clause in re.split(
            r";|\s+and\s+(?=(?:cut|remove|delete|mute|bleep|speed\s+up|slow\s+down|"
            r"add|keep|move|reorder|auto-caption|undo|redo)\b)",
            raw,
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    if len(clauses) > 1:
        parsed_parts = [
            parse_instruction(clause, timeline, answer)
            for clause in clauses
        ]
        if any(part.status == "rejected" for part in parsed_parts):
            failed = [
                f"part {index + 1} ({clauses[index]}): "
                f"{part.reason or part.summary}"
                for index, part in enumerate(parsed_parts)
                if part.status == "rejected"
            ]
            return ParsedInstruction(
                "rejected",
                reason=(
                    "No part of this multi-part instruction was applied. "
                    + "; ".join(failed)
                ),
            )
        if any(part.status == "needs_clarification" for part in parsed_parts):
            questions = [
                f"Part {index + 1}: {part.question}"
                for index, part in enumerate(parsed_parts)
                if part.status == "needs_clarification"
            ]
            return ParsedInstruction(
                "needs_clarification",
                question=(
                    "No part was applied. Please clarify before I apply the "
                    "multi-part instruction. " + " ".join(questions)
                ),
                options=[
                    f"Part {index + 1}: {option}"
                    for index, part in enumerate(parsed_parts)
                    if part.status == "needs_clarification"
                    for option in part.options
                ],
            )
        return ParsedInstruction(
            "applied",
            [operation for part in parsed_parts for operation in part.operations],
            "Applied all parts: " + " ".join(part.summary for part in parsed_parts),
        )

    targeted_undo = re.search(
        r"\bundo\s+(?:the\s+)?(last|second-to-last|second last|third-to-last)\s+cut\b",
        lowered,
    )
    if targeted_undo:
        return ParsedInstruction(
            "applied",
            [{
                "kind": "undo_cut",
                "ordinal": targeted_undo.group(1).replace(" ", "-"),
            }],
            f"Undid the {targeted_undo.group(1)} cut; other edits remain.",
        )
    if re.fullmatch(r"(?:undo|undo the last edit)", lowered):
        return ParsedInstruction("applied", [{"kind": "undo"}], "Undid the most recent edit.")
    if re.fullmatch(r"redo", lowered):
        return ParsedInstruction("applied", [{"kind": "redo"}], "Redid the most recently undone edit.")
    restore_id = re.search(
        r"\b(?:restore|reinstate)\s+(?:the\s+)?(?:edit|cut)\s+([a-z0-9_-]+)\b",
        lowered,
    )
    if restore_id:
        return ParsedInstruction(
            "applied",
            [{"kind": "restore", "edit_id": restore_id.group(1)}],
            f"Restored edit {restore_id.group(1)}.",
        )
    if re.search(r"\b(?:put|bring)\s+(?:the\s+)?(?:intro|opening)\s+back\b", lowered):
        return ParsedInstruction(
            "applied",
            [{"kind": "restore_intro"}],
            "Restored the removed intro.",
        )
    put_back = re.search(
        r"\b(?:put|bring)\s+(?:the\s+)?(.+?)\s+back\b",
        raw,
        re.IGNORECASE,
    ) or re.search(
        r"\bput\s+back\s+(?:the\s+)?(.+?)$",
        raw,
        re.IGNORECASE,
    )
    restore_content = re.search(
        r"\brestore\s+(?:the\s+)?(.+?)(?:\s+cut)?$",
        raw,
        re.IGNORECASE,
    )
    if put_back or restore_content:
        phrase = (put_back or restore_content).group(1).strip(" .!?\"'")
        phrase = re.sub(
            r"\s+(?:part|section|sentence|clip)$",
            "",
            phrase,
            flags=re.IGNORECASE,
        )
        if phrase.lower() in {"what i removed", "what was removed", "what i cut"}:
            return ParsedInstruction(
                "applied",
                [{"kind": "restore_any_cut"}],
                "Restored the specified earlier cut.",
            )
        occurrences = _occurrences(timeline, phrase)
        selection = _select_occurrence(timeline, occurrences, phrase, answer)
        if isinstance(selection, ParsedInstruction):
            return selection
        return ParsedInstruction(
            "applied",
            [{
                "kind": "restore_content",
                "source_ranges": _source_ranges_for_word_ids(
                    timeline,
                    [word.id for word in selection],
                ),
            }],
            f"Restored the earlier cut containing “{phrase}”.",
        )

    if "auto-caption" in lowered or "auto caption" in lowered or "caption the whole" in lowered:
        return ParsedInstruction(
            "applied",
            [{"kind": "auto_caption"}],
            "Added captions anchored to transcript words.",
        )

    if "remove filler" in lowered or "cut filler" in lowered or "remove ums" in lowered:
        return ParsedInstruction(
            "applied",
            [{"kind": "remove_fillers"}],
            "Removed transcript filler words (um, uh, and conversational like).",
        )

    silence = re.search(
        r"(?:remove|cut)\s+(?:the\s+)?silences?\s+longer\s+than\s+"
        r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s)",
        lowered,
    )
    if silence:
        return ParsedInstruction(
            "applied",
            [{
                "kind": "remove_silence",
                "threshold": float(silence.group(1)),
            }],
            f"Removed detected silences longer than {silence.group(1)} seconds.",
        )

    pause = re.search(r"\b(?:keep|preserve)\s+(?:the\s+)?pause\b", lowered)
    if pause:
        if range_ is None:
            return ParsedInstruction(
                "needs_clarification",
                question="What time range contains the pause to preserve?",
                options=["Provide the pause's start and end time."],
            )
        start, end = range_
        try:
            source_ranges = _ranges_from_current_time(timeline, start, end)
        except ValueError as error:
            return ParsedInstruction("rejected", reason=str(error))
        return ParsedInstruction(
            "applied",
            [
                {
                    "kind": "mark_pause",
                    "source_start": source_start,
                    "source_end": source_end,
                }
                for source_start, source_end in source_ranges
            ],
            f"Marked {start:.2f}–{end:.2f}s as a deliberate pause.",
        )

    if "background music" in lowered or re.search(r"\badd music\b", lowered):
        source = re.search(r"\b(?:from|at)\s+['\"]?([^'\"]+\.(?:mp3|wav|m4a|aac))", raw, re.I)
        if not source:
            return ParsedInstruction(
                "needs_clarification",
                question="Which music file should I use?",
                options=["Provide a local music file or an accessible URL."],
            )
        return ParsedInstruction(
            "applied",
            [{"kind": "add_music", "source": source.group(1), "volume": 0.2}],
            "Added background music at 20% volume with speech ducking.",
        )

    range_ = _range_from_text(raw, timeline.current_duration)
    whole_video_request = re.fullmatch(
        r"(?:please\s+)?(?:cut|remove|delete)\s+(?:the\s+)?"
        r"(?:everything|whole video|all of it)[.!]?",
        lowered,
    )
    if re.search(r"\b(?:cut|remove|delete)\b", lowered) and (
        range_ is not None or whole_video_request
    ):
        if range_ is None:
            return ParsedInstruction(
                "rejected",
                reason="Removing the whole video is not allowed.",
            )
        start, end = range_
        if start < 0 or end > timeline.current_duration or end <= start:
            return ParsedInstruction(
                "rejected",
                reason="The requested cut is impossible or outside the current timeline.",
            )
        if start <= 1e-6 and end >= timeline.current_duration - 1e-6:
            return ParsedInstruction(
                "rejected",
                reason="Removing the whole video is not allowed.",
            )
        return ParsedInstruction(
            "applied",
            [{
                "kind": "cut",
                "source_ranges": _ranges_from_current_time(timeline, start, end),
            }],
            f"Cut current-timeline range {start:.2f}–{end:.2f}s.",
        )

    if re.search(r"\bkeep\s+only\b", lowered):
        if range_ is None:
            return ParsedInstruction(
                "needs_clarification",
                question="What time range should I keep?",
                options=["Provide a start and end time."],
            )
        start, end = range_
        if start < 0 or end > timeline.current_duration or end <= start:
            return ParsedInstruction("rejected", reason="The keep range is outside the current timeline.")
        selected = timeline.current_ranges_to_source(start, end)
        return ParsedInstruction(
            "applied",
            [{
                "kind": "keep_source_ranges",
                "source_ranges": [[item[1], item[2]] for item in selected],
            }],
            f"Kept current-timeline range {start:.2f}–{end:.2f}s.",
        )

    speed = re.search(r"\b(?:speed\s+up|slow\s+down|change\s+speed)\b", lowered)
    if speed:
        if range_ is None:
            return ParsedInstruction(
                "needs_clarification",
                question="What time range should change speed?",
                options=["Provide a start and end time."],
            )
        rate = re.search(r"\bby\s+(\d+(?:\.\d+)?)\s*x\b", lowered)
        if rate is None:
            return ParsedInstruction(
                "needs_clarification",
                question="What playback speed should I use?",
                options=["For example, 0.5x, 2x, or 4x."],
            )
        multiplier = float(rate.group(1))
        if "slow down" in lowered:
            multiplier = 1 / multiplier
        if not 0.25 <= multiplier <= 4:
            return ParsedInstruction(
                "rejected",
                reason="Playback speed must be between 0.25x and 4x.",
            )
        start, end = range_
        try:
            source_ranges = _ranges_from_current_time(timeline, start, end)
        except ValueError as error:
            return ParsedInstruction("rejected", reason=str(error))
        return ParsedInstruction(
            "applied",
            [{
                "kind": "speed",
                "source_ranges": source_ranges,
                "speed": multiplier,
            }],
            f"Set playback speed to {multiplier:g}x on the selected range.",
        )

    reorder = re.search(
        r"\b(?:reorder|move)\s+(?:the\s+)?(?:section|part)?\s*"
        r"(.+?)\s+(before|after)\s+(?:the\s+)?(?:section|part)?\s*(.+)$",
        raw,
        re.IGNORECASE,
    )
    if reorder:
        selected_phrase = re.sub(
            r"^(?:about|containing|with the words)\s+",
            "",
            reorder.group(1).strip(" '\""),
            flags=re.IGNORECASE,
        )
        target_phrase = re.sub(
            r"^(?:about|containing|with the words)\s+",
            "",
            reorder.group(3).strip(" '\""),
            flags=re.IGNORECASE,
        )
        selected_matches = _occurrences(timeline, selected_phrase)
        selected = _select_occurrence(
            timeline,
            selected_matches,
            selected_phrase,
            answer,
        )
        if isinstance(selected, ParsedInstruction):
            return selected
        target_matches = _occurrences(timeline, target_phrase)
        target = _select_occurrence(
            timeline,
            target_matches,
            target_phrase,
            answer,
        )
        if isinstance(target, ParsedInstruction):
            return target
        selected_start, selected_end = _current_occurrence_range(timeline, selected)
        target_start, _ = _current_occurrence_range(timeline, target)
        if selected_start <= target_start < selected_end:
            return ParsedInstruction(
                "rejected",
                reason="A section cannot be moved relative to itself.",
            )
        try:
            selected_ranges = _ranges_from_current_time(
                timeline, selected_start, selected_end
            )
        except ValueError as error:
            return ParsedInstruction("rejected", reason=str(error))
        target_source_time = target[0].start
        return ParsedInstruction(
            "applied",
            [{
                "kind": "reorder",
                "source_ranges": selected_ranges,
                "target_source_time": target_source_time,
                "before": reorder.group(2).lower() == "before",
            }],
            f"Moved “{reorder.group(1).strip()}” "
            f"{reorder.group(2).lower()} “{reorder.group(3).strip()}”.",
        )
    if re.search(r"\b(?:reorder|move)\b", lowered):
        return ParsedInstruction(
            "needs_clarification",
            question="Which section should move, and where should it go?",
            options=["Name the section and its destination using transcript words or times."],
        )

    match = re.search(r"\b(?:mute|bleep)\b.*?\b(?:word|saying|says)\s+['\"]?([^'\"]+?)['\"]?$", raw, re.I)
    if match:
        effect_kind = "bleep" if "bleep" in lowered else "mute"
        phrase = re.sub(r"^saying\s+", "", match.group(1), flags=re.IGNORECASE)
        occurrences = _occurrences(timeline, phrase)
        selection = _select_occurrence(timeline, occurrences, phrase, answer)
        if isinstance(selection, ParsedInstruction):
            if selection.status == "applied" and selection.operations:
                selection.operations = [
                    {
                        "kind": "effect",
                        "effect": effect_kind,
                        "word_ids": [
                            word.id
                            for group in occurrences
                            for word in group
                        ],
                    }
                ]
            return selection
        return ParsedInstruction(
            "applied",
            [{
                "kind": "effect",
                "effect": effect_kind,
                "word_ids": [word.id for word in selection],
            }],
            f"Applied {effect_kind} to the selected word(s).",
        )

    caption = re.search(r"\bcaption\s+['\"](.+?)['\"]\s+(?:when|on)\s+(?:i\s+)?say\s+(.+)$", raw, re.I)
    if caption:
        matches = _occurrences(timeline, caption.group(2))
        selection = _select_occurrence(
            timeline, matches, caption.group(2), answer
        )
        if isinstance(selection, ParsedInstruction):
            return selection
        return ParsedInstruction(
            "applied",
            [{
                "kind": "add_caption",
                "text": caption.group(1),
                "start_word_id": selection[0].id,
                "end_word_id": selection[-1].id,
            }],
            f"Added a caption attached to words “{caption.group(2)}”.",
        )

    timed_caption = re.search(
        r"\b(?:add|put|show|display)\s+(?:a\s+)?caption\s+"
        r"['\"](.+?)['\"]\s+at\s+(\d+(?::\d{1,2})?(?:\.\d+)?)\b",
        raw,
        re.IGNORECASE,
    )
    if timed_caption:
        try:
            start = parse_time(timed_caption.group(2), timeline.current_duration)
        except ValueError as error:
            return ParsedInstruction("rejected", reason=str(error))
        if start < 0 or start >= timeline.current_duration:
            return ParsedInstruction(
                "rejected",
                reason="The caption time is outside the current video.",
            )
        end = min(start + 2.0, timeline.current_duration)
        source_ranges = _ranges_from_current_time(timeline, start, end)
        if not source_ranges:
            return ParsedInstruction(
                "rejected",
                reason="The caption time falls in content that is no longer on the timeline.",
            )
        return ParsedInstruction(
            "applied",
            [
                {
                    "kind": "add_overlay",
                    "text": timed_caption.group(1),
                    "source_start": source_start,
                    "source_end": source_end,
                }
                for source_start, source_end in source_ranges
            ],
            f"Added a two-second caption at current-timeline time {start:.2f}s.",
        )

    if re.search(r"\b(?:add|put|show|display)\s+(?:a\s+)?caption\b", lowered):
        at_time = re.search(r"\bat\s+(\d+(?::\d{1,2})?(?:\.\d+)?)\b", raw, re.I)
        if at_time:
            try:
                start = parse_time(at_time.group(1), timeline.current_duration)
            except ValueError as error:
                return ParsedInstruction("rejected", reason=str(error))
            if start < 0 or start >= timeline.current_duration:
                return ParsedInstruction(
                    "rejected",
                    reason="The caption time is outside the current video.",
                )
        return ParsedInstruction(
            "needs_clarification",
            question="What caption text should I add?",
            options=["Provide the caption in quotes and a transcript phrase or time."],
        )

    speaker_request = re.search(
        r"\b(?:remove|cut|delete)\s+everything\s+the\s+guest\s+says\s+about\s+(.+)$",
        raw,
        re.IGNORECASE,
    )
    if speaker_request:
        topic = re.sub(
            r"^(?:their|his|her|the guest's|the guest)\s+",
            "",
            speaker_request.group(1).strip(" .!?\"'"),
            flags=re.IGNORECASE,
        )
        labels = list(dict.fromkeys(
            word.speaker for word in timeline.words if word.speaker
        ))
        guest_labels = [
            label for label in labels
            if "guest" in label.lower()
            or label.lower() in {"speaker_2", "speaker 2", "speaker2"}
        ]
        if not guest_labels:
            return ParsedInstruction(
                "needs_clarification",
                question="Which transcript speaker label belongs to the guest?",
                options=[str(label) for label in labels] or ["Transcript has no speaker labels."],
            )
        guest_label = guest_labels[0]
        matches = _occurrences(timeline, topic)
        guest_matches = [
            group for group in matches
            if any(word.speaker == guest_label for word in group)
        ]
        selection = _select_occurrence(
            timeline,
            guest_matches,
            topic,
            answer,
        )
        if isinstance(selection, ParsedInstruction):
            return selection
        guest_indexes = [
            index for index, word in enumerate(timeline.words)
            if word.speaker == guest_label
            and selection[0].start <= word.start
            and word.end <= selection[-1].end
        ]
        if not guest_indexes:
            return ParsedInstruction(
                "rejected",
                reason="No guest speech was found for the requested topic.",
            )
        start_index = min(guest_indexes)
        end_index = max(guest_indexes)
        while start_index > 0:
            current = timeline.words[start_index]
            previous = timeline.words[start_index - 1]
            if (
                previous.speaker != guest_label
                or current.start - previous.end > 1.5
                or re.search(r"[.!?]$", previous.text)
            ):
                break
            start_index -= 1
        while end_index + 1 < len(timeline.words):
            current = timeline.words[end_index]
            following = timeline.words[end_index + 1]
            if (
                following.speaker != guest_label
                or following.start - current.end > 1.5
                or re.search(r"[.!?]$", current.text)
            ):
                break
            end_index += 1
        guest_start = timeline.words[start_index].start
        guest_end = timeline.words[end_index].end
        overlapping_speakers = {
            word.speaker
            for word in timeline.words
            if word.speaker != guest_label
            and word.start < guest_end
            and word.end > guest_start
        }
        if overlapping_speakers:
            return ParsedInstruction(
                "needs_clarification",
                question=(
                    "Guest speech overlaps another speaker. Should I cut the overlapping "
                    "audio too, or keep the overlap and only remove non-overlapping guest words?"
                ),
                options=["Cut the overlapping range", "Keep the overlap"],
            )
        positions = timeline.word_positions()
        guest_time_start = min(
            start for word in timeline.words[start_index:end_index + 1]
            for start, _ in positions.get(word.id, [])
        )
        guest_time_end = max(
            end for word in timeline.words[start_index:end_index + 1]
            for _, end in positions.get(word.id, [])
        )
        question_match = re.search(
            r"\b(?:host|speaker_1|speaker 1|speaker1)\b",
            " ".join(
                word.speaker.lower()
                for word in timeline.words
                if word.speaker
            ),
        )
        host_label = "host" if question_match and "host" in question_match.group(0) else (
            next((label for label in labels if label != guest_label), None)
        )
        lead_in = [
            word for word in timeline.words
            if host_label
            and word.speaker == host_label
            and word.end <= guest_start
            and guest_start - word.end <= 2.0
        ]
        include_lead_in = bool(answer and re.search(
            r"(?:include|remove|cut).*(?:host|question|lead.?in)",
            answer,
            re.IGNORECASE,
        ))
        keep_only = bool(answer and re.search(
            r"(?:guest only|only the guest|leave.*host|keep.*host)",
            answer,
            re.IGNORECASE,
        ))
        if lead_in and not include_lead_in and not keep_only:
            return ParsedInstruction(
                "needs_clarification",
                question=(
                    "I can remove only the guest's answer. The host's lead-in question "
                    "will stay. Should the host question stay or be removed too?"
                ),
                options=["Remove only the guest's answer", "Remove the host question too"],
            )
        ranges = _ranges_from_current_time(
            timeline,
            guest_time_start,
            guest_time_end,
        )
        if include_lead_in and lead_in:
            host_start = min(word.start for word in lead_in)
            ranges = _ranges_from_current_time(
                timeline,
                min(guest_time_start, host_start),
                guest_time_end,
            )
        return ParsedInstruction(
            "applied",
            [{"kind": "cut", "source_ranges": ranges}],
            f"Removed guest speaker {guest_label!r}'s answer; "
            + ("also removed the host lead-in." if include_lead_in else "kept the host lead-in."),
        )

    if re.search(r"\b(?:make|change)\b.*\b(?:caption|it|that)\b", lowered):
        if not timeline.captions:
            return ParsedInstruction(
                "rejected",
                reason="There is no caption to modify.",
            )
        if len(timeline.captions) > 1:
            options = [f"{index + 1}. {caption.text}" for index, caption in enumerate(timeline.captions)]
            return ParsedInstruction(
                "needs_clarification",
                question="Which caption should I change?",
                options=options,
            )
        target = timeline.captions[0]
        operation: dict[str, Any] = {
            "kind": "style_caption",
            "caption_id": target.id,
        }
        color = re.search(r"\b(red|blue|green|yellow|white|black|orange|purple)\b", lowered)
        if color:
            operation["color"] = color.group(1)
        if "bigger" in lowered or "larger" in lowered:
            operation["font_size"] = min(160, target.font_size + 16)
        elif "smaller" in lowered:
            operation["font_size"] = max(12, target.font_size - 8)
        if "color" not in operation and "font_size" not in operation:
            return ParsedInstruction(
                "needs_clarification",
                question="How should I change the caption?",
                options=["Specify a color or say bigger/smaller."],
            )
        return ParsedInstruction("applied", [operation], f"Updated caption “{target.text}”.")

    if re.search(r"\b(?:remove|cut|delete)\b.*\b(?:part|sentence|mentions?|about|says?)\b", lowered):
        remove_after_match = re.search(
            r"\b(?:remove|cut|delete)\s+everything\s+after\s+(?:i\s+)?say\s+(.+)$",
            lowered,
        )
        phrase = re.sub(
            r"^(?:please\s+)?(?:remove|cut|delete)\s+(?:the\s+)?(?:part|sentence|section)?\s*(?:about|where|that|which)?\s*",
            "",
            lowered,
        ).strip(" .!?\"'")
        if remove_after_match:
            phrase = remove_after_match.group(1).strip(" .!?\"'")
        occurrences = _occurrences(timeline, phrase)
        selection = _select_occurrence(timeline, occurrences, phrase, answer)
        if isinstance(selection, ParsedInstruction):
            return selection
        positions = timeline.word_positions()
        if remove_after_match:
            start = max(end for word in selection for _, end in positions[word.id])
            end = timeline.current_duration
        else:
            start = min(start for word in selection for start, _ in positions[word.id])
            end = max(end for word in selection for _, end in positions[word.id])
        if end <= start:
            return ParsedInstruction(
                "rejected",
                reason="The selected content is already at the end of the timeline.",
            )
        ranges = _ranges_from_current_time(timeline, start, end)
        return ParsedInstruction(
            "applied",
            [{"kind": "cut", "source_ranges": ranges}],
            f"Removed matching words at {start:.2f}–{end:.2f}s.",
        )

    if "text overlay" in lowered or re.search(r"\boverlay\b", lowered):
        overlay_text = re.search(r"['\"](.+?)['\"]", raw)
        if not overlay_text or range_ is None:
            return ParsedInstruction(
                "needs_clarification",
                question="What text should appear, and for what time range?",
                options=["Use a quoted text string and provide the range."],
            )
        start, end = range_
        source_ranges = _ranges_from_current_time(timeline, start, end)
        if not source_ranges:
            return ParsedInstruction("rejected", reason="The overlay range is not present.")
        return ParsedInstruction(
            "applied",
            [{
                "kind": "add_overlay",
                "text": overlay_text.group(1),
                "source_start": source_ranges[0][0],
                "source_end": source_ranges[-1][1],
            }],
            f"Added text overlay for {start:.2f}–{end:.2f}s.",
        )

    return ParsedInstruction(
        "needs_clarification",
        question="I could not identify a safe editing operation. What would you like changed?",
        options=[
            "Cut or keep a time range",
            "Change playback speed",
            "Caption transcript words",
            "Mute or bleep a word",
            "Remove fillers or long silences",
        ],
    )
