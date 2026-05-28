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
