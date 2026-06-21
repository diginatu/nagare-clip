"""Tests for <keep>...</keep> force-keep marker handling in intervals sync."""

from __future__ import annotations

import pytest

from nagare_clip.intervals.sync_json import (
    extract_keep_ranges,
    extract_speed_ranges,
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


# --- sync_text_to_json strips <keep> tags from corrected text ---


class TestSyncStripsKeepTags:
    def test_pure_keep_only_line_keeps_words_unchanged(self):
        """Wrapping text in <keep> with no other change should be a no-op for segment text & words."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        result = sync_text_to_json(data, ["あ<keep>い</keep>う"])
        assert result["segments"][0]["text"] == "あいう"
        assert result["segments"][0]["words"] == words

    def test_keep_tag_with_patch_inside(self):
        """<keep> wrapping a patch: patch still applies, tag is stripped."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("え", 0.2, 0.4),
            _word("ー", 0.4, 0.6),
            _word("う", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あえーう", words))
        result = sync_text_to_json(data, ["あ<keep>{{えー->}}</keep>う"])
        # Patch deletes "えー", <keep> tags do not appear in output
        assert result["segments"][0]["text"] == "あう"
        assert [w["word"] for w in result["segments"][0]["words"]] == ["あ", "う"]

    def test_unclosed_keep_tag_is_stripped(self):
        """An unclosed <keep> opener is still removed from the corrected text."""
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        result = sync_text_to_json(data, ["あ<keep>い"])
        # Tag removed; no patch needed
        assert result["segments"][0]["text"] == "あい"


# --- extract_keep_ranges ---


class TestExtractKeepRanges:
    def test_no_keep_tags_returns_empty(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_keep_ranges(["あい"], data) == []

    def test_single_keep_block_returns_word_time_span(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.5),
            _word("う", 0.5, 0.9),
            _word("え", 0.9, 1.2),
        ]
        data = _whisperx(_segment("あいうえ", words))
        ranges = extract_keep_ranges(["あ<keep>いう</keep>え"], data)
        # Inside <keep>: chars "いう" → words[1] and words[2] → (0.2, 0.9)
        assert ranges == [(0.2, 0.9)]

    def test_multiple_keep_blocks_emit_multiple_ranges(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
            _word("お", 0.8, 1.0),
        ]
        data = _whisperx(_segment("あいうえお", words))
        ranges = extract_keep_ranges(["<keep>あ</keep>い<keep>うえ</keep>お"], data)
        assert ranges == [(0.0, 0.2), (0.4, 0.8)]

    def test_keep_with_patch_inside_uses_post_patch_words(self):
        """A <keep> wrapping {{old->new}} should map to the timing of the wrapped post-patch chars."""
        # Original: あえーう → after {{えー->}} patch: あう
        # <keep>{{えー->}}</keep> → visible content is empty → no range
        words = [
            _word("あ", 0.0, 0.2),
            _word("え", 0.2, 0.4),
            _word("ー", 0.4, 0.6),
            _word("う", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あえーう", words))
        synced = sync_text_to_json(data, ["あ<keep>{{えー->}}</keep>う"])
        ranges = extract_keep_ranges(["あ<keep>{{えー->}}</keep>う"], synced)
        # Empty visible content → no range
        assert ranges == []

    def test_keep_with_substitution_patch_inside(self):
        """<keep> wrapping a substitution patch uses the patched word timings."""
        # Original "今日わ" → patch {{わ->は}} → "今日は"
        # <keep>今日{{わ->は}}</keep> → visible content "今日は" → range covers all 3 chars
        words = [
            _word("今", 1.0, 1.2),
            _word("日", 1.2, 1.4),
            _word("わ", 1.4, 1.7),
        ]
        data = _whisperx(_segment("今日わ", words))
        synced = sync_text_to_json(data, ["<keep>今日{{わ->は}}</keep>"])
        ranges = extract_keep_ranges(["<keep>今日{{わ->は}}</keep>"], synced)
        assert len(ranges) == 1
        start, end = ranges[0]
        assert start == pytest.approx(1.0)
        assert end == pytest.approx(1.7)

    def test_empty_keep_block_skipped(self):
        words = [_word("あ", 0.0, 0.2)]
        data = _whisperx(_segment("あ", words))
        assert extract_keep_ranges(["<keep></keep>あ"], data) == []

    def test_unclosed_keep_skipped(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        # `<keep>` with no `</keep>` produces no range
        assert extract_keep_ranges(["<keep>あい"], data) == []

    def test_unmatched_closing_keep_skipped(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_keep_ranges(["あい</keep>"], data) == []

    def test_nested_keep_treats_outer_only(self):
        """Nested <keep> tags: outer span is used, inner opener is ignored."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        ranges = extract_keep_ranges(["あ<keep>い<keep>う</keep>え"], data)
        # Outer opener at pos 1; first matched </keep> closes at pos 3 (after "いう")
        # Inner <keep> is dropped (nested-opener warning); outer block = "いう"
        assert ranges == [(0.2, 0.6)]

    def test_multiple_segments(self):
        """Edit lines correspond to segments; ranges are collected across segments."""
        seg1_words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg2_words = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(_segment("あい", seg1_words), _segment("うえ", seg2_words))
        ranges = extract_keep_ranges(
            ["<keep>あい</keep>", "う<keep>え</keep>"], data
        )
        assert ranges == [(0.0, 0.4), (1.2, 1.4)]

    def test_extra_edit_lines_ignored(self):
        words = [_word("あ", 0.0, 0.2)]
        data = _whisperx(_segment("あ", words))
        # Second edit line has no matching segment
        ranges = extract_keep_ranges(["あ", "<keep>extra</keep>"], data)
        assert ranges == []


class TestExtractKeepRangesCrossLine:
    """`<keep>` opened on one line, closed on a later line — the resolved
    range spans the wrapped words *and* the inter-segment silences."""

    def test_open_in_seg0_close_in_seg1(self):
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg1 = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(_segment("あい", seg0), _segment("うえ", seg1))
        ranges = extract_keep_ranges(
            ["あ<keep>い", "う</keep>え"], data
        )
        # First wrapped word = seg0 'い' (start=0.2); last wrapped word = seg1 'う' (end=1.2)
        assert ranges == [(0.2, 1.2)]

    def test_open_in_seg0_close_in_seg2_middle_untagged(self):
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg1 = [_word("う", 1.0, 1.2)]
        seg2 = [_word("え", 2.0, 2.2), _word("お", 2.2, 2.4)]
        data = _whisperx(
            _segment("あい", seg0), _segment("う", seg1), _segment("えお", seg2)
        )
        ranges = extract_keep_ranges(
            ["あ<keep>い", "う", "え</keep>お"], data
        )
        # First wrapped = seg0 'い' (0.2); last wrapped = seg2 'え' (2.2)
        # The inter-segment silences fall inside the single (0.2, 2.2) range.
        assert ranges == [(0.2, 2.2)]

    def test_opener_at_end_of_segment_advances_to_next(self):
        """`<keep>` placed after the last word of seg0 → first wrapped word
        is the first word of seg1."""
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg1 = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(_segment("あい", seg0), _segment("うえ", seg1))
        ranges = extract_keep_ranges(
            ["あい<keep>", "う</keep>え"], data
        )
        # seg0 has no word at pos 2 → fall through to seg1[0] = 'う' (start=1.0)
        assert ranges == [(1.0, 1.2)]

    def test_closer_at_start_of_segment_falls_back_to_prev(self):
        """`</keep>` placed before the first word of seg1 → last wrapped word
        is the last word of seg0."""
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg1 = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(_segment("あい", seg0), _segment("うえ", seg1))
        ranges = extract_keep_ranges(
            ["あ<keep>い", "</keep>うえ"], data
        )
        # closer at seg1 pos 0 → fall back to seg0[-1] = 'い' (end=0.4)
        assert ranges == [(0.2, 0.4)]

    def test_boundary_only_span_skipped(self):
        """Opener at end of seg0 + closer at start of seg1 wraps no words →
        the resolved (first, last) would invert → skip with warning."""
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg1 = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(_segment("あい", seg0), _segment("うえ", seg1))
        ranges = extract_keep_ranges(
            ["あい<keep>", "</keep>うえ"], data
        )
        assert ranges == []

    def test_unclosed_at_eof_skipped(self):
        """`<keep>` opened with no `</keep>` anywhere in the file → drop."""
        seg0 = [_word("あ", 0.0, 0.2)]
        seg1 = [_word("い", 1.0, 1.2)]
        data = _whisperx(_segment("あ", seg0), _segment("い", seg1))
        ranges = extract_keep_ranges(["あ<keep>", "い"], data)
        assert ranges == []

    def test_mixed_single_line_and_cross_line(self):
        """Single-line block in seg0 emits one range; later cross-line block
        emits another. Both are reported."""
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4), _word("う", 0.4, 0.6)]
        seg1 = [_word("え", 1.0, 1.2)]
        seg2 = [_word("お", 2.0, 2.2), _word("か", 2.2, 2.4)]
        data = _whisperx(
            _segment("あいう", seg0),
            _segment("え", seg1),
            _segment("おか", seg2),
        )
        ranges = extract_keep_ranges(
            ["<keep>あ</keep>い<keep>う", "え", "お</keep>か"], data
        )
        # First block: single-line, seg0 'あ' → (0.0, 0.2)
        # Second block: cross-line, seg0 'う' → seg2 'お' → (0.4, 2.2)
        assert ranges == [(0.0, 0.2), (0.4, 2.2)]


# --- sync_text_to_json strips <speed factor="..."> tags from corrected text ---


class TestSyncStripsSpeedTags:
    def test_pure_speed_only_line_keeps_words_unchanged(self):
        """Wrapping text in <speed> with no other change should be a no-op."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        result = sync_text_to_json(data, ['あ<speed factor="2.0">い</speed>う'])
        assert result["segments"][0]["text"] == "あいう"
        assert result["segments"][0]["words"] == words

    def test_speed_tag_with_patch_inside(self):
        """<speed> wrapping a patch: patch still applies, tag is stripped."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("え", 0.2, 0.4),
            _word("ー", 0.4, 0.6),
            _word("う", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あえーう", words))
        result = sync_text_to_json(
            data, ['あ<speed factor="0.5">{{えー->}}</speed>う']
        )
        assert result["segments"][0]["text"] == "あう"
        assert [w["word"] for w in result["segments"][0]["words"]] == ["あ", "う"]

    def test_unclosed_speed_tag_is_stripped(self):
        """An unclosed <speed> opener is still removed from the corrected text."""
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        result = sync_text_to_json(data, ['あ<speed factor="2.0">い'])
        assert result["segments"][0]["text"] == "あい"

    def test_speed_inside_keep_both_stripped(self):
        """Both <keep> and <speed> tags can coexist; both stripped from text."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        result = sync_text_to_json(
            data, ['<keep>あ</keep><speed factor="1.5">い</speed>う']
        )
        assert result["segments"][0]["text"] == "あいう"


# --- extract_speed_ranges ---


class TestExtractSpeedRanges:
    def test_no_speed_tags_returns_empty(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_speed_ranges(["あい"], data) == []

    def test_single_speed_block_returns_word_time_span_with_factor(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.5),
            _word("う", 0.5, 0.9),
            _word("え", 0.9, 1.2),
        ]
        data = _whisperx(_segment("あいうえ", words))
        ranges = extract_speed_ranges(
            ['あ<speed factor="2.0">いう</speed>え'], data
        )
        assert ranges == [(0.2, 0.9, 2.0)]

    def test_factor_parses_various_floats(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        for factor_str, expected in (("0.5", 0.5), ("1.5", 1.5), ("3.0", 3.0)):
            ranges = extract_speed_ranges(
                [f'<speed factor="{factor_str}">あい</speed>'], data
            )
            assert ranges == [(0.0, 0.4, expected)], (
                f"factor {factor_str!r} should parse to {expected}"
            )

    def test_multiple_speed_blocks_emit_multiple_ranges(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
            _word("お", 0.8, 1.0),
        ]
        data = _whisperx(_segment("あいうえお", words))
        ranges = extract_speed_ranges(
            ['<speed factor="2.0">あ</speed>い<speed factor="0.5">うえ</speed>お'],
            data,
        )
        assert ranges == [(0.0, 0.2, 2.0), (0.4, 0.8, 0.5)]

    def test_empty_speed_block_skipped(self):
        words = [_word("あ", 0.0, 0.2)]
        data = _whisperx(_segment("あ", words))
        assert extract_speed_ranges(['<speed factor="2.0"></speed>あ'], data) == []

    def test_unclosed_speed_skipped(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_speed_ranges(['<speed factor="2.0">あい'], data) == []

    def test_unmatched_closing_speed_skipped(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_speed_ranges(["あい</speed>"], data) == []

    def test_nested_speed_treats_outer_only(self):
        """Nested <speed> tags: outer span is used, inner opener is ignored."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        ranges = extract_speed_ranges(
            ['あ<speed factor="2.0">い<speed factor="3.0">う</speed>え'], data
        )
        assert ranges == [(0.2, 0.6, 2.0)]

    def test_cross_line_speed_span(self):
        """<speed> can open on one segment line and close on a later one."""
        seg0 = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg1 = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(_segment("あい", seg0), _segment("うえ", seg1))
        ranges = extract_speed_ranges(
            ['あ<speed factor="2.0">い', "う</speed>え"], data
        )
        assert ranges == [(0.2, 1.2, 2.0)]

    def test_keep_and_speed_do_not_interfere(self):
        """Speed extraction ignores <keep> tags entirely; <keep> extraction
        ignores <speed> tags entirely."""
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        edit_line = '<keep>あ</keep>い<speed factor="2.0">うえ</speed>'
        assert extract_speed_ranges([edit_line], data) == [(0.4, 0.8, 2.0)]
        assert extract_keep_ranges([edit_line], data) == [(0.0, 0.2)]
