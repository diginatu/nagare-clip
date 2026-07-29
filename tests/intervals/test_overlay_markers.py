"""Tests for `<overlay text="..." duration="N.N"/>` point markers in intervals sync."""

from __future__ import annotations

from nagare_clip.intervals.sync_json import (
    extract_overlay_marks,
    sync_text_to_json,
)


def _word(char: str, start: float, end: float, score: float = 0.9) -> dict:
    return {"word": char, "start": start, "end": end, "score": score}


def _segment(text: str, words: list) -> dict:
    return {"text": text, "start": words[0]["start"], "end": words[-1]["end"], "words": words}


def _whisperx(*segments: dict) -> dict:
    all_words: list = []
    for s in segments:
        all_words.extend(s["words"])
    return {"segments": list(segments), "word_segments": all_words}


# --- sync_text_to_json strips <overlay .../> markers from corrected text ---


class TestSyncStripsOverlayMarkers:
    def test_marker_only_line_keeps_words_unchanged(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        result = sync_text_to_json(data, ['あ<overlay text="X" duration="3.0"/>いう'])
        assert result["segments"][0]["text"] == "あいう"
        assert result["segments"][0]["words"] == words

    def test_marker_next_to_patch(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("え", 0.2, 0.4),
            _word("ー", 0.4, 0.6),
            _word("う", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あえーう", words))
        result = sync_text_to_json(data, ['あ<overlay text="X" duration="2.5"/>{{えー->}}う'])
        assert result["segments"][0]["text"] == "あう"
        assert [w["word"] for w in result["segments"][0]["words"]] == ["あ", "う"]


# --- extract_overlay_marks ---


class TestExtractOverlayMarks:
    def test_no_markers_returns_empty(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_overlay_marks(["あい"], data) == []

    def test_single_marker_returns_start_duration_text(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.5),
            _word("う", 0.5, 0.9),
            _word("え", 0.9, 1.2),
        ]
        data = _whisperx(_segment("あいうえ", words))
        marks = extract_overlay_marks(['あ<overlay text="Chapter 1" duration="3.0"/>いうえ'], data)
        # Anchored to the first word AFTER the marker ("い" → 0.2)
        assert marks == [(0.2, 3.0, "Chapter 1")]

    def test_marker_at_line_start_anchors_to_first_word(self):
        words = [_word("あ", 1.5, 1.7), _word("い", 1.7, 1.9)]
        data = _whisperx(_segment("あい", words))
        marks = extract_overlay_marks(['<overlay text="X" duration="2.0"/>あい'], data)
        assert marks == [(1.5, 2.0, "X")]

    def test_multiple_markers_on_one_line(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        marks = extract_overlay_marks(
            ['<overlay text="A" duration="1.0"/>あい<overlay text="B" duration="2.0"/>うえ'],
            data,
        )
        assert marks == [(0.0, 1.0, "A"), (0.4, 2.0, "B")]

    def test_marker_on_later_line(self):
        seg1 = _segment("あい", [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)])
        seg2 = _segment("うえ", [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)])
        data = _whisperx(seg1, seg2)
        marks = extract_overlay_marks(["あい", 'う<overlay text="X" duration="4.0"/>え'], data)
        assert marks == [(1.2, 4.0, "X")]

    def test_marker_at_end_of_line_anchors_to_next_line(self):
        seg1 = _segment("あい", [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)])
        seg2 = _segment("うえ", [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)])
        data = _whisperx(seg1, seg2)
        marks = extract_overlay_marks(['あい<overlay text="X" duration="1.5"/>', "うえ"], data)
        assert marks == [(1.0, 1.5, "X")]

    def test_marker_after_last_word_falls_back_to_that_word_end(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        marks = extract_overlay_marks(['あい<overlay text="X" duration="1.5"/>'], data)
        assert marks == [(0.4, 1.5, "X")]

    def test_empty_text_is_skipped_with_warning(self, caplog):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        with caplog.at_level("WARNING"):
            marks = extract_overlay_marks(['<overlay text="" duration="2.0"/>あい'], data)
        assert marks == []
        assert "empty text" in caplog.text

    def test_escaped_newline_decodes_to_a_real_line_break(self):
        # The marker lives on one physical _edits.txt line, so a multi-line
        # caption travels escaped and is decoded here — Blender's TextStrip
        # renders the resulting "\n" as a line break.
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        marks = extract_overlay_marks(['<overlay text="上\\n下" duration="2.0"/>あい'], data)
        assert marks == [(0.0, 2.0, "上\n下")]

    def test_escaped_backslash_decodes_to_one_backslash(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        marks = extract_overlay_marks(['<overlay text="a\\\\nb" duration="2.0"/>あい'], data)
        assert marks == [(0.0, 2.0, "a\\nb")]

    def test_unknown_escape_is_left_alone(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        marks = extract_overlay_marks(['<overlay text="a\\tb" duration="2.0"/>あい'], data)
        assert marks == [(0.0, 2.0, "a\\tb")]

    def test_zero_duration_is_skipped_with_warning(self, caplog):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        with caplog.at_level("WARNING"):
            marks = extract_overlay_marks(['<overlay text="X" duration="0"/>あい'], data)
        assert marks == []
        assert "duration" in caplog.text

    def test_marker_coexists_with_keep_and_speed(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        line = '<overlay text="X" duration="2.0"/>あい<keep>う</keep>え'
        assert extract_overlay_marks([line], data) == [(0.0, 2.0, "X")]

    def test_marker_position_unaffected_by_other_tags(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        # <keep> tags before the marker must not shift its anchor position.
        line = '<keep>あい</keep><overlay text="X" duration="2.0"/>う'
        assert extract_overlay_marks([line], data) == [(0.4, 2.0, "X")]

    def test_lines_past_the_segment_count_are_ignored(self):
        words = [_word("あ", 0.0, 0.2)]
        data = _whisperx(_segment("あ", words))
        marks = extract_overlay_marks(["あ", '<overlay text="X" duration="2.0"/>い'], data)
        assert marks == []
