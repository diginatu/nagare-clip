"""Tests for Stage 3 JSON sync."""

from __future__ import annotations

from nagare_clip.stage3.sync_json import sync_text_to_json


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

    def test_deleted_segment(self):
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, [""])
        assert result["segments"][0]["text"] == ""
        assert result["segments"][0]["words"] == []
        assert result["word_segments"] == []

    def test_changed_text_redistributes_timing(self):
        words = [
            _make_word("急", 1.0, 1.5, 0.8),
            _make_word("は", 1.5, 2.0, 0.9),
        ]
        json_data = {
            "segments": [_make_segment("急は", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["今日は"])
        seg = result["segments"][0]
        assert seg["text"] == "今日は"
        assert len(seg["words"]) == 3
        # Check timing is linearly distributed across [1.0, 2.0]
        w = seg["words"]
        assert w[0]["word"] == "今"
        assert abs(w[0]["start"] - 1.0) < 0.01
        assert abs(w[1]["word"] == "日")
        assert abs(w[2]["word"] == "は")
        assert abs(w[2]["end"] - 2.0) < 0.01
        # Score should be average of originals
        avg = (0.8 + 0.9) / 2
        assert abs(w[0]["score"] - avg) < 0.01

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
        # Delete first segment, keep second
        result = sync_text_to_json(json_data, ["", "い"])
        assert len(result["word_segments"]) == 1
        assert result["word_segments"][0]["word"] == "い"

    def test_does_not_mutate_input(self):
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        sync_text_to_json(json_data, ["い"])
        # Original should be unchanged
        assert json_data["segments"][0]["text"] == "あ"
        assert json_data["segments"][0]["words"][0]["word"] == "あ"

    def test_more_lines_than_segments(self):
        """Extra corrected lines beyond segments are ignored."""
        words = [_make_word("あ", 0.0, 0.5)]
        json_data = {
            "segments": [_make_segment("あ", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["あ", "extra line"])
        assert len(result["segments"]) == 1

    def test_segment_without_words(self):
        """Segments with no words and changed text skip redistribution."""
        json_data = {
            "segments": [{"text": "test", "start": 0.0, "end": 1.0, "words": []}],
            "word_segments": [],
        }
        result = sync_text_to_json(json_data, ["changed"])
        assert result["segments"][0]["text"] == "changed"
        assert result["segments"][0]["words"] == []
