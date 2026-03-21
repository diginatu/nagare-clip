"""Tests for Stage 3 JSON sync."""

from __future__ import annotations

import pytest

from nagare_clip.stage3.sync_json import _decompose_edit_line, sync_text_to_json


def _make_segment(text: str, words: list) -> dict:
    return {"text": text, "start": 0.0, "end": 1.0, "words": words}


def _make_word(char: str, start: float, end: float, score: float = 0.9) -> dict:
    return {"word": char, "start": start, "end": end, "score": score}


class TestSyncTextToJson:
    def test_unchanged_text_preserves_everything(self):
        words = [_make_word("あ", 0.0, 0.5), _make_word("い", 0.5, 1.0)]
        json_data = {
            "segments": [_make_segment("あい", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["あい"])
        assert result["segments"][0]["words"] == words

    def test_word_segments_rebuilt(self):
        w1 = [_make_word("あ", 0.0, 0.5)]
        w2 = [_make_word("い", 1.0, 1.5)]
        json_data = {
            "segments": [
                _make_segment("あ", w1),
                _make_segment("い", w2),
            ],
            "word_segments": w1 + w2,
        }
        # Delete first segment via marker, keep second
        result = sync_text_to_json(json_data, ["{{あ->}}", "い"])
        assert len(result["word_segments"]) == 1
        assert result["word_segments"][0]["word"] == "い"

    def test_does_not_mutate_input(self):
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        sync_text_to_json(json_data, ["{{あ->い}}"])
        # Original should be unchanged
        assert json_data["segments"][0]["text"] == "あ"
        assert json_data["segments"][0]["words"][0]["word"] == "あ"

    def test_more_lines_than_segments(self):
        """Extra edit lines beyond segments are ignored."""
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["あ", "extra line"])
        assert len(result["segments"]) == 1

    def test_segment_without_words_and_patch(self):
        """Segments with no words: text is updated, words stays empty."""
        json_data = {
            "segments": [{"text": "test", "start": 0.0, "end": 1.0, "words": []}],
            "word_segments": [],
        }
        result = sync_text_to_json(json_data, ["{{test->changed}}"])
        assert result["segments"][0]["text"] == "changed"
        assert result["segments"][0]["words"] == []

    def test_change_without_markers_raises(self):
        """Changing text without markers raises ValueError."""
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        with pytest.raises(ValueError, match="without.*markers"):
            sync_text_to_json(json_data, ["い"])


class TestFineGrainedSync:
    """Tests for fine-grained sync using {{old->new}} edit_lines."""

    def test_keeps_unchanged_timing_redistributes_patch(self):
        """Unchanged chars preserve original timing; patched chars redistribute."""
        words = [
            _make_word("今", 1.0, 1.2, 0.95),
            _make_word("日", 1.2, 1.4, 0.90),
            _make_word("わ", 1.4, 1.7, 0.80),
        ]
        json_data = {
            "segments": [_make_segment("今日わ", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["今日{{わ->は}}"])
        w = result["segments"][0]["words"]

        assert len(w) == 3
        # First two chars should preserve original timing exactly
        assert w[0]["word"] == "今"
        assert w[0]["start"] == 1.0
        assert w[0]["end"] == 1.2
        assert w[0]["score"] == 0.95
        assert w[1]["word"] == "日"
        assert w[1]["start"] == 1.2
        assert w[1]["end"] == 1.4
        assert w[1]["score"] == 0.90
        # Patched char gets redistributed timing in [1.4, 1.7]
        assert w[2]["word"] == "は"
        assert abs(w[2]["start"] - 1.4) < 0.01
        assert abs(w[2]["end"] - 1.7) < 0.01

    def test_deletion_via_patch(self):
        """{{えーと->}} removes filler, keeps surrounding timing."""
        words = [
            _make_word("え", 0.0, 0.2),
            _make_word("ー", 0.2, 0.4),
            _make_word("と", 0.4, 0.6),
            _make_word("今", 0.6, 0.8),
            _make_word("日", 0.8, 1.0),
        ]
        json_data = {
            "segments": [_make_segment("えーと今日", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["{{えーと->}}今日"])
        w = result["segments"][0]["words"]

        assert len(w) == 2
        assert w[0]["word"] == "今"
        assert w[0]["start"] == 0.6
        assert w[1]["word"] == "日"
        assert w[1]["start"] == 0.8

    def test_multiple_patches_in_one_line(self):
        """Multiple {{old->new}} patches in a single line."""
        words = [
            _make_word("あ", 0.0, 0.2),
            _make_word("い", 0.2, 0.4),
            _make_word("う", 0.4, 0.6),
            _make_word("え", 0.6, 0.8),
        ]
        json_data = {
            "segments": [_make_segment("あいうえ", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["{{あ->A}}い{{う->U}}え"])
        w = result["segments"][0]["words"]

        assert len(w) == 4
        assert w[0]["word"] == "A"
        assert abs(w[0]["start"] - 0.0) < 0.01
        assert abs(w[0]["end"] - 0.2) < 0.01
        assert w[1]["word"] == "い"
        assert w[1]["start"] == 0.2
        assert w[1]["end"] == 0.4
        assert w[2]["word"] == "U"
        assert abs(w[2]["start"] - 0.4) < 0.01
        assert abs(w[2]["end"] - 0.6) < 0.01
        assert w[3]["word"] == "え"
        assert w[3]["start"] == 0.6
        assert w[3]["end"] == 0.8

    def test_insertion_via_patch(self):
        """{{->X}} inserts text with zero-duration at boundary."""
        words = [
            _make_word("あ", 0.0, 0.5),
            _make_word("い", 0.5, 1.0),
        ]
        json_data = {
            "segments": [_make_segment("あい", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["あ{{->X}}い"])
        w = result["segments"][0]["words"]

        assert len(w) == 3
        assert w[0]["word"] == "あ"
        assert w[0]["start"] == 0.0
        assert w[0]["end"] == 0.5
        assert w[1]["word"] == "X"
        assert w[1]["start"] == 0.5
        assert w[1]["end"] == 0.5
        assert w[2]["word"] == "い"
        assert w[2]["start"] == 0.5
        assert w[2]["end"] == 1.0

    def test_no_markers_raises_error(self):
        """Edit line without markers raises ValueError."""
        words = [
            _make_word("あ", 0.0, 0.5),
            _make_word("い", 0.5, 1.0),
        ]
        json_data = {
            "segments": [_make_segment("あい", words)],
            "word_segments": words,
        }
        with pytest.raises(ValueError, match="without.*markers"):
            sync_text_to_json(json_data, ["うえ"])

    def test_clearing_line_without_markers_raises_error(self):
        """Emptying a line without {{text->}} markers raises ValueError."""
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        with pytest.raises(ValueError, match="without.*markers"):
            sync_text_to_json(json_data, [""])

    def test_full_deletion_via_patch(self):
        """{{entire->}} deletes a whole segment via marker."""
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["{{あ->}}"])
        assert result["segments"][0]["text"] == ""
        assert result["segments"][0]["words"] == []
        assert result["word_segments"] == []

    def test_decompose_mismatch_returns_none(self):
        """Decomposition returns None when old text doesn't match original."""
        result = _decompose_edit_line("{{あ->い}}う", "Xう")
        assert result is None

    def test_decompose_valid(self):
        """Decomposition returns correct regions for a simple patch."""
        regions = _decompose_edit_line("あ{{い->う}}え", "あいえ")
        assert regions is not None
        assert len(regions) == 3
        assert regions[0] == ("keep", 0, 1, "あ")
        assert regions[1] == ("patch", 1, 2, "う")
        assert regions[2] == ("keep", 2, 3, "え")
