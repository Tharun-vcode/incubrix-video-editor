from app.models.timeline import Caption, Timeline, Word
from app.services.timeline_engine import TimelineEngine


def make_engine() -> TimelineEngine:
    words = [
        Word("w1", "Welcome", 1.0, 1.4),
        Word("w2", "to", 1.4, 1.6),
        Word("w3", "the", 1.6, 1.8),
        Word("w4", "channel", 1.8, 2.3),
        Word("w5", "pricing", 4.0, 4.5),
        Word("w6", "is", 4.5, 4.7),
        Word("w7", "here", 4.7, 5.0),
        Word("w8", "cost", 7.0, 7.3),
        Word("w9", "matters", 7.3, 7.8),
        Word("w10", "thanks", 9.0, 9.4),
    ]
    timeline = Timeline(
        duration=12.0,
        words=words,
        captions=[Caption("caption-0001", "Welcome!", "w1", "w4")],
    )
    return TimelineEngine(timeline)


def test_cut_keeps_caption_attached_to_words_and_updates_duration():
    engine = make_engine()

    response = engine.execute("Cut the first 2 seconds")

    assert response.status == "applied"
    assert response.duration_s == 9.7
    assert engine.timeline.word_positions()["w1"] == []
    assert round(engine.timeline.word_positions()["w5"][0][0], 2) == 1.7
    assert "word-safe boundaries" in response.summary


def test_ambiguous_semantic_match_asks_then_uses_answer():
    engine = make_engine()

    question = engine.execute("Remove the part about pricing")

    assert question.status == "needs_clarification"
    assert len(question.options) == 3
    applied = engine.execute("Remove the part about pricing", answer="the second one")

    assert applied.status == "applied"
    assert engine.timeline.word_positions()["w5"]
    assert engine.timeline.word_positions()["w8"] == []


def test_undo_and_redo_restore_exact_timeline_hashes():
    engine = make_engine()
    initial_hash = engine.timeline_hash

    engine.execute("Cut the first 2 seconds")
    edited_hash = engine.timeline_hash
    undone = engine.execute("Undo")
    redone = engine.execute("Redo")

    assert undone.status == "applied"
    assert undone.timeline_hash == initial_hash
    assert redone.status == "applied"
    assert redone.timeline_hash == edited_hash


def test_repeated_cut_is_idempotent_and_does_not_create_history():
    engine = make_engine()

    first = engine.execute("Cut the first 2 seconds")
    second = engine.execute("Cut the first 2 seconds")

    assert first.status == second.status == "applied"
    assert first.timeline_hash == second.timeline_hash
    assert len(engine.history) == 1


def test_new_edit_clears_redo_history():
    engine = make_engine()
    engine.execute("Cut the first 2 seconds")
    engine.execute("Undo")

    engine.execute("Cut the last 1 seconds")

    assert engine.execute("Redo").status == "rejected"


def test_cutting_all_caption_words_removes_caption_and_undo_restores_it():
    engine = make_engine()

    result = engine.execute("Cut the first 3 seconds")

    assert result.status == "applied"
    assert not engine.timeline.captions
    assert engine.execute("Undo").status == "applied"
    assert engine.timeline.captions[0].text == "Welcome!"


def test_restoring_an_earlier_cut_preserves_later_content_edits():
    engine = make_engine()
    cut = engine.execute("Cut the first 2 seconds")
    cut_id = cut.edit_id
    engine.execute("Mute the word saying pricing", answer="the first one")

    restored = engine.execute(f"Restore edit {cut_id}")

    assert restored.status == "applied"
    assert engine.timeline.word_positions()["w1"]
    assert engine.timeline.effects[0].word_id == "w5"
    assert engine.execute("Undo").status == "applied"
    assert engine.timeline.word_positions()["w1"] == []


def test_restoring_unspecified_cut_asks_and_accepts_an_ordinal_answer():
    engine = make_engine()
    engine.execute("Cut the first 2 seconds")
    second_cut = engine.execute("Cut the last 4 seconds")

    question = engine.execute("Put back what I removed")
    restored = engine.execute("the second one")

    assert question.status == "needs_clarification"
    assert len(question.options) == 2
    assert restored.status == "applied"
    assert engine.timeline.word_positions()["w10"]
    assert engine.timeline.word_positions()["w1"] == []
    assert second_cut.edit_id is not None


def test_explicit_cut_id_can_resolve_pending_restore_clarification():
    engine = make_engine()
    first_cut = engine.execute("Cut the first 2 seconds")
    engine.execute("Cut the last 4 seconds")

    question = engine.execute("Put back what I removed")
    restored = engine.execute(f"Restore cut {first_cut.edit_id}")

    assert question.status == "needs_clarification"
    assert restored.status == "applied"
    assert engine.timeline.word_positions()["w1"]
    assert engine.timeline.word_positions()["w10"] == []


def test_full_video_cut_is_rejected_without_changing_hash():
    engine = make_engine()
    original_hash = engine.timeline_hash

    response = engine.execute("Cut everything")

    assert response.status == "rejected"
    assert engine.timeline_hash == original_hash


def test_instruction_time_policy_is_explicit():
    response = make_engine().execute("Cut the first 2 seconds")

    assert response.time_policy == "current_timeline"
