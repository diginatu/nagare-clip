"""Tests for guided_edit reconciliation: verbatim safety + op reflection."""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.reconcile import clean_old, verify_op


class TestCleanOld:
    def test_resolves_patches_to_old_and_strips_tags(self):
        assert clean_old("あ{{い->X}}う") == "あいう"
        assert clean_old("<cut>あ</cut>い") == "あい"
        assert clean_old('<speed factor="2.0">あ</speed>') == "あ"
        assert clean_old('<overlay text="z">あ</overlay>') == "あ"


def _op(t, a, b, **kw):
    return DirectorOp(type=t, lines=(a, b), **kw)


class TestVerifyOk:
    def test_cut_single_line_ok(self):
        before = ["あいう"]
        after = ["あ<cut>いう</cut>"]
        assert verify_op(before, after, _op("cut", 1, 1)) is None

    def test_cut_cross_line_ok(self):
        before = ["あい", "うえ", "おか"]
        after = ["あ<cut>い", "うえ", "お</cut>か"]
        assert verify_op(before, after, _op("cut", 1, 3)) is None

    def test_speed_ok(self):
        before = ["あいう"]
        after = ['<speed factor="2.0">あいう</speed>']
        assert verify_op(before, after, _op("speed", 1, 1, factor=2.0)) is None

    def test_overlay_ok(self):
        before = ["あいう"]
        after = ['<overlay text="z">あいう</overlay>']
        assert verify_op(before, after, _op("overlay", 1, 1, text="z")) is None

    def test_edit_ok(self):
        before = ["あえーとい"]
        after = ["あ{{えーと->}}い"]
        assert verify_op(before, after, _op("edit", 1, 1)) is None


class TestVerifyFails:
    def test_text_altered_rejected(self):
        before = ["あいう"]
        # LLM rephrased outside markers
        after = ["あ<cut>いえ</cut>"]
        assert verify_op(before, after, _op("cut", 1, 1)) is not None

    def test_missing_tag_rejected(self):
        before = ["あいう"]
        after = ["あいう"]  # nothing inserted
        assert verify_op(before, after, _op("cut", 1, 1)) is not None

    def test_cut_missing_closer_rejected(self):
        before = ["あい", "うえ"]
        after = ["あ<cut>い", "うえ"]  # opener but no closer
        assert verify_op(before, after, _op("cut", 1, 2)) is not None

    def test_edit_no_change_rejected(self):
        before = ["あいう"]
        after = ["あいう"]
        assert verify_op(before, after, _op("edit", 1, 1)) is not None

    def test_cut_opener_not_on_first_line_rejected(self):
        # Both tags collapsed onto the last line; first boundary untouched, so
        # only the last line is actually cut (the reported failure mode).
        before = ["あい", "うえ", "おか"]
        after = ["あい", "うえ", "<cut>おか</cut>"]
        assert verify_op(before, after, _op("cut", 1, 3)) is not None

    def test_cut_closer_not_on_last_line_rejected(self):
        # Symmetric: both tags collapsed onto the first line.
        before = ["あい", "うえ", "おか"]
        after = ["<cut>あい</cut>", "うえ", "おか"]
        assert verify_op(before, after, _op("cut", 1, 3)) is not None

    def test_keep_opener_not_on_first_line_rejected(self):
        before = ["あい", "うえ"]
        after = ["あい", "<keep>うえ</keep>"]
        assert verify_op(before, after, _op("keep", 1, 2)) is not None

    def test_speed_opener_not_on_first_line_rejected(self):
        before = ["あい", "うえ"]
        after = ["あい", '<speed factor="2.0">うえ</speed>']
        assert verify_op(before, after, _op("speed", 1, 2, factor=2.0)) is not None

    def test_overlay_opener_not_on_first_line_rejected(self):
        before = ["あい", "うえ"]
        after = ["あい", '<overlay text="z">うえ</overlay>']
        assert verify_op(before, after, _op("overlay", 1, 2, text="z")) is not None
