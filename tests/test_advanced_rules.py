from app.models.timeline import Segment, Timeline, Word
from app.services.instructions import parse_time
from app.services.otio_export import export_otio
from app.services.timeline_engine import TimelineEngine


def test_time_parser_accepts_seconds_and_colloquial_minute_time():
    assert parse_time("80 seconds", 120) == 80
    assert parse_time("a minute twenty", 120) == 80
    assert parse_time("1:20", 120) == 80


def test_keep_only_and_speed_use_current_timeline_time():
    engine = TimelineEngine(Timeline(duration=60))

    kept = engine.execute("Keep only from 10 seconds to 20 seconds")
    sped_up = engine.execute("Speed up from 2 seconds to 6 seconds by 2x")

    assert kept.status == "applied"
    assert kept.duration_s == 10
    assert sped_up.status == "applied"
    assert sped_up.duration_s == 8


def test_reorder_keeps_source_words_and_moves_the_selected_section():
    words = [
        Word("w1", "welcome", 1, 2),
        Word("w2", "pricing", 4, 5),
        Word("w3", "thanks", 8, 9),
    ]
    engine = TimelineEngine(Timeline(duration=10, words=words))

    result = engine.execute("Move the part about pricing before welcome")

    assert result.status == "applied"
    positions = engine.timeline.word_positions()
    assert positions["w2"][0][0] < positions["w1"][0][0]


def test_auto_caption_only_adds_visible_words():
    timeline = Timeline(
        duration=10,
        words=[
            Word("w1", "one", 1, 1.2),
            Word("w2", "two", 3, 3.2),
        ],
        segments=[Segment("segment-1", 0, 2)],
    )
    engine = TimelineEngine(timeline)

    response = engine.execute("Auto-caption the whole video")

    assert response.status == "applied"
    assert [caption.text for caption in engine.timeline.captions] == ["one"]


def test_deliberate_pause_is_not_removed_as_silence():
    timeline = Timeline(
        duration=10,
        words=[
            Word("w1", "start", 1, 1.5),
            Word("w2", "finish", 6, 6.5),
        ],
        deliberate_pauses=[(1.5, 6.0)],
    )
    engine = TimelineEngine(timeline)

    response = engine.execute("Remove silences longer than 2 seconds")

    assert response.status == "applied"
    assert response.duration_s == 10


def test_remove_everything_after_repeated_word_asks_which_occurrence():
    words = [
        Word("w1", "thanks", 1, 1.2),
        Word("w2", "again", 2, 2.2),
        Word("w3", "thanks", 4, 4.2),
        Word("w4", "goodbye", 5, 5.2),
    ]
    engine = TimelineEngine(Timeline(duration=8, words=words))

    response = engine.execute("Remove everything after I say thanks")

    assert response.status == "needs_clarification"
    assert len(response.options) == 3


def test_out_of_range_caption_is_rejected():
    engine = TimelineEngine(Timeline(duration=30))

    response = engine.execute("Add a caption 'Hello!' at 5:00")

    assert response.status == "rejected"
    assert "outside" in response.summary


def test_multipart_instruction_is_atomic_when_one_part_is_impossible():
    engine = TimelineEngine(Timeline(duration=20))

    response = engine.execute(
        "Cut the first 2 seconds and speed up from 1:00 to 1:10 by 2x"
    )

    assert response.status == "rejected"
    assert response.duration_s == 20


def test_guest_answer_requires_host_lead_in_clarification_then_removes_only_guest():
    words = [
        Word("w1", "What", 1.0, 1.2, speaker="host"),
        Word("w2", "about", 1.2, 1.4, speaker="host"),
        Word("w3", "your", 1.4, 1.6, speaker="host"),
        Word("w4", "old", 1.6, 1.8, speaker="host"),
        Word("w5", "job?", 1.8, 2.1, speaker="host"),
        Word("w6", "I", 3.0, 3.1, speaker="guest"),
        Word("w7", "left", 3.1, 3.3, speaker="guest"),
        Word("w8", "my", 3.3, 3.4, speaker="guest"),
        Word("w9", "old", 3.4, 3.6, speaker="guest"),
        Word("w10", "job.", 3.6, 4.0, speaker="guest"),
        Word("w11", "Thanks.", 6.0, 6.3, speaker="host"),
    ]
    engine = TimelineEngine(Timeline(duration=8, words=words))
    instruction = "Remove everything the guest says about their old job"

    question = engine.execute(instruction)
    result = engine.execute("Remove only the guest's answer")

    assert question.status == "needs_clarification"
    assert result.status == "applied"
    assert engine.timeline.word_positions()["w1"]
    assert engine.timeline.word_positions()["w6"] == []


def test_otio_export_contains_speed_and_media_reference():
    timeline = Timeline(
        duration=10,
        segments=[Segment("segment-1", 0, 10, speed=2.0)],
    )

    exported = export_otio(timeline, r"C:\media\source.mp4", frame_rate=30)

    assert exported["OTIO_SCHEMA"] == "Timeline.1"
    clip = exported["tracks"]["children"][0]["children"][0]
    assert clip["effects"][0]["time_scalar"] == 2.0
    assert clip["media_reference"]["target_url"] == "file:///C:/media/source.mp4"
