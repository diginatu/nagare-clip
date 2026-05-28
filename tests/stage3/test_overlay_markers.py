"""Tests for <overlay text="...">...</overlay> marker handling in Stage 3 sync."""

from __future__ import annotations

import pytest

from nagare_clip.stage3.sync_json import (
    extract_overlay_ranges,
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


# --- sync_text_to_json strips <overlay> tags from corrected text ---


class TestSyncStripsOverlayTags:
    def test_pure_overlay_only_line_keeps_words_unchanged(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        result = sync_text_to_json(data, ['あ<overlay text="X">い</overlay>う'])
        assert result["segments"][0]["text"] == "あいう"
        assert result["segments"][0]["words"] == words

    def test_overlay_tag_with_patch_inside(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("え", 0.2, 0.4),
            _word("ー", 0.4, 0.6),
            _word("う", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あえーう", words))
        result = sync_text_to_json(
            data, ['あ<overlay text="X">{{えー->}}</overlay>う']
        )
        assert result["segments"][0]["text"] == "あう"
        assert [w["word"] for w in result["segments"][0]["words"]] == ["あ", "う"]

    def test_unclosed_overlay_tag_is_stripped(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        result = sync_text_to_json(data, ['あ<overlay text="X">い'])
        assert result["segments"][0]["text"] == "あい"


# --- extract_overlay_ranges ---


class TestExtractOverlayRanges:
    def test_no_overlay_tags_returns_empty(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_overlay_ranges(["あい"], data) == []

    def test_single_overlay_block_returns_triple(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.5),
            _word("う", 0.5, 0.9),
            _word("え", 0.9, 1.2),
        ]
        data = _whisperx(_segment("あいうえ", words))
        ranges = extract_overlay_ranges(
            ['あ<overlay text="Chapter 1">いう</overlay>え'], data
        )
        assert ranges == [(0.2, 0.9, "Chapter 1")]

    def test_multiple_overlay_blocks_emit_multiple_triples(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
            _word("お", 0.8, 1.0),
        ]
        data = _whisperx(_segment("あいうえお", words))
        ranges = extract_overlay_ranges(
            ['<overlay text="A">あ</overlay>い<overlay text="B">うえ</overlay>お'],
            data,
        )
        assert ranges == [(0.0, 0.2, "A"), (0.4, 0.8, "B")]

    def test_overlay_spanning_multiple_lines(self):
        seg1_words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg2_words = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(
            _segment("あい", seg1_words),
            _segment("うえ", seg2_words),
        )
        ranges = extract_overlay_ranges(
            ['あ<overlay text="X">い', "う</overlay>え"], data
        )
        # First wrapped word "い" → 0.2; last wrapped word "う" → 1.2
        assert ranges == [(0.2, 1.2, "X")]

    def test_unclosed_overlay_is_skipped_with_warning(self, caplog):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        with caplog.at_level("WARNING"):
            ranges = extract_overlay_ranges(['あ<overlay text="X">い'], data)
        assert ranges == []
        assert "Unclosed <overlay>" in caplog.text

    def test_unmatched_close_overlay_is_skipped_with_warning(self, caplog):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        with caplog.at_level("WARNING"):
            ranges = extract_overlay_ranges(["あい</overlay>"], data)
        assert ranges == []
        assert "Unmatched </overlay>" in caplog.text

    def test_nested_overlay_inner_opener_ignored(self, caplog):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        with caplog.at_level("WARNING"):
            ranges = extract_overlay_ranges(
                ['<overlay text="A">あい<overlay text="B">う</overlay>え</overlay>'],
                data,
            )
        # Outer span resolves; inner opener is warned and dropped
        assert ranges == [(0.0, 0.8, "A")]
        assert "Nested <overlay>" in caplog.text

    def test_overlay_coexists_with_keep_and_speed(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        # Overlay around い only; keep and speed wrappers around う
        line = '<overlay text="X">あい</overlay><keep>う</keep>え'
        ranges = extract_overlay_ranges([line], data)
        assert ranges == [(0.0, 0.4, "X")]

    def test_empty_text_attribute_resolves_to_empty_string(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        ranges = extract_overlay_ranges(['あ<overlay text="">い</overlay>'], data)
        # Overlay has no wrapped words → empty range, skipped
        # (this asserts the "</overlay>" placed right after "<overlay text>" with nothing wrapped is dropped)
        assert ranges == []
