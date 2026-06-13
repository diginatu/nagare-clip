"""Tests for the <cut>...</cut> deletion-shorthand marker.

`<cut>` desugars to `{{wrapped->}}` deletion patches before the normal patch
flow, so the wrapped words are removed from the synced JSON.  A large deletion
then falls out of the timeline via the existing silence-gap mechanism (covered
by the interval-stage tests); here we test the desugaring and word removal.
"""

from __future__ import annotations

from nagare_clip.stage3.sync_json import _expand_cut_tags, sync_text_to_json


def _make_word(char: str, start: float, end: float, score: float = 0.9) -> dict:
    return {"word": char, "start": start, "end": end, "score": score}


def _make_segment(text: str, words: list) -> dict:
    return {"text": text, "start": 0.0, "end": 1.0, "words": words}


class TestExpandCutTags:
    def test_whole_line_cut_becomes_full_deletion(self):
        assert _expand_cut_tags(["<cut>あいう</cut>"]) == ["{{あいう->}}"]

    def test_partial_in_line_cut(self):
        # head kept, tail cut
        assert _expand_cut_tags(["あ<cut>いう</cut>"]) == ["あ{{いう->}}"]
        # cut in the middle
        assert _expand_cut_tags(["あ<cut>い</cut>う"]) == ["あ{{い->}}う"]

    def test_cross_line_cut(self):
        # open on line 0 (tail), whole middle line, close on line 2 (head)
        lines = ["あ<cut>いう", "かきく", "けこ</cut>さ"]
        assert _expand_cut_tags(lines) == ["あ{{いう->}}", "{{かきく->}}", "{{けこ->}}さ"]

    def test_inner_patch_resolved_to_original_old(self):
        # A {{old->new}} inside a cut resolves to its `old` side for deletion.
        assert _expand_cut_tags(["<cut>{{あ->X}}い</cut>"]) == ["{{あい->}}"]

    def test_empty_cut_is_noop(self):
        assert _expand_cut_tags(["<cut></cut>"]) == [""]
        assert _expand_cut_tags(["あ<cut></cut>い"]) == ["あい"]

    def test_unmatched_close_ignored(self):
        assert _expand_cut_tags(["あ</cut>い"]) == ["あい"]

    def test_unclosed_open_at_eof_ignored(self):
        # The dangling <cut> is dropped; wrapped text is still converted to a
        # deletion through EOF (it opened, never closed).
        assert _expand_cut_tags(["あ<cut>い"]) == ["あ{{い->}}"]

    def test_lines_without_cut_untouched(self):
        assert _expand_cut_tags(["あ{{い->う}}え", "かき"]) == ["あ{{い->う}}え", "かき"]


class TestCutSyncRemovesWords:
    def test_whole_segment_cut_removes_all_words(self):
        words = [_make_word("あ", 0.0, 0.5), _make_word("い", 0.5, 1.0)]
        json_data = {
            "segments": [_make_segment("あい", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["<cut>あい</cut>"])
        assert result["segments"][0]["text"] == ""
        assert result["segments"][0]["words"] == []
        assert result["word_segments"] == []

    def test_partial_cut_keeps_unwrapped_words(self):
        words = [
            _make_word("あ", 0.0, 0.3),
            _make_word("い", 0.3, 0.6),
            _make_word("う", 0.6, 1.0),
        ]
        json_data = {
            "segments": [_make_segment("あいう", words)],
            "word_segments": words,
        }
        result = sync_text_to_json(json_data, ["あ<cut>いう</cut>"])
        w = result["segments"][0]["words"]
        assert [x["word"] for x in w] == ["あ"]
        assert w[0]["start"] == 0.0
        assert w[0]["end"] == 0.3
        assert result["segments"][0]["text"] == "あ"

    def test_cross_line_cut_removes_spanned_words(self):
        s1 = [_make_word("あ", 0.0, 0.3), _make_word("い", 0.3, 0.6)]
        s2 = [_make_word("か", 1.0, 1.3), _make_word("き", 1.3, 1.6)]
        s3 = [_make_word("け", 2.0, 2.3), _make_word("こ", 2.3, 2.6)]
        json_data = {
            "segments": [
                _make_segment("あい", s1),
                _make_segment("かき", s2),
                _make_segment("けこ", s3),
            ],
            "word_segments": s1 + s2 + s3,
        }
        # cut from "い" through "け": keep あ ... こ
        result = sync_text_to_json(
            json_data, ["あ<cut>い", "かき", "け</cut>こ"]
        )
        segs = result["segments"]
        assert [x["word"] for x in segs[0]["words"]] == ["あ"]
        assert segs[1]["words"] == []
        assert [x["word"] for x in segs[2]["words"]] == ["こ"]
        assert [x["word"] for x in result["word_segments"]] == ["あ", "こ"]
