import pytest

from app.models.timeline import Caption, Segment, Timeline, Word


def test_timeline_can_store_words_and_caption():
    words = [
        Word("w1", "welcome", 42.0, 42.5),
        Word("w2", "to", 42.5, 42.7),
        Word("w3", "the", 42.7, 42.9),
        Word("w4", "channel", 42.9, 43.5),
    ]

    caption = Caption(
        id="c1",
        text="Welcome!",
        start_word_id="w1",
        end_word_id="w4",
    )

    timeline = Timeline(
        duration=120.0,
        words=words,
        captions=[caption],
    )

    assert timeline.captions[0].start_word_id == "w1"
    assert timeline.captions[0].end_word_id == "w4"


def test_timeline_maps_current_time_to_source_after_speed_change():
    timeline = Timeline(
        duration=10.0,
        segments=[Segment("segment-1", 0.0, 10.0, speed=2.0)],
    )

    assert timeline.duration == 5.0
    assert timeline.current_ranges_to_source(1.0, 2.0) == [
        (0, 2.0, 4.0, 1.0, 2.0)
    ]


def test_word_positions_follow_source_words_across_reorder():
    timeline = Timeline(
        duration=10.0,
        words=[
            Word("w1", "first", 1.0, 2.0),
            Word("w2", "last", 8.0, 9.0),
        ],
        segments=[
            Segment("segment-1", 8.0, 10.0),
            Segment("segment-2", 0.0, 8.0),
        ],
    )

    positions = timeline.word_positions()

    assert positions["w1"] == [(3.0, 4.0)]
    assert positions["w2"] == [(0.0, 1.0)]


def test_timeline_round_trip_preserves_word_anchors_and_segments():
    timeline = Timeline(
        duration=10.0,
        words=[Word("w1", "hello", 2.0, 3.0)],
        captions=[
            Caption("c1", "Hello!", "w1", "w1", color="red"),
        ],
        segments=[Segment("segment-2", 1.0, 9.0, speed=2.0)],
    )

    restored = Timeline.from_dict(timeline.to_dict())

    assert restored.to_dict() == timeline.to_dict()


@pytest.mark.parametrize(
    ("word", "message"),
    [
        (Word("bad", "word", -1.0, 0.5), "timestamps outside"),
        (Word("bad", "word", 0.5, 0.8, confidence=1.1), "confidence"),
    ],
)
def test_timeline_rejects_invalid_word_metadata(word, message):
    with pytest.raises(ValueError, match=message):
        Timeline(duration=1.0, words=[word])


def test_timeline_rejects_caption_with_missing_word_anchor():
    with pytest.raises(ValueError, match="unknown word"):
        Timeline(
            duration=2.0,
            words=[Word("w1", "hello", 0.0, 0.5)],
            captions=[Caption("c1", "Hello", "missing", "w1")],
        )