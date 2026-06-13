"""Tests for guided_edit apply orchestration (fake LLM, no network)."""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.apply import apply_ops, build_user_prompt


def _op(t, a, b, **kw):
    return DirectorOp(type=t, lines=(a, b), **kw)


def _queue_llm(responses):
    it = iter(responses)

    def fake(_messages, _cfg):
        return next(it)

    return fake


CFG = {"prompt": "P"}


class TestBuildPrompt:
    def test_single_line_shows_one_line(self):
        p = build_user_prompt(_op("cut", 2, 2), ["a", "b", "c"])
        assert "2: b" in p
        assert "1: a" not in p and "3: c" not in p

    def test_wide_range_shows_boundaries_and_omission(self):
        p = build_user_prompt(_op("cut", 1, 4), ["a", "b", "c", "d"])
        assert "1: a" in p and "4: d" in p
        assert "2: b" not in p and "3: c" not in p
        assert "between" in p.lower()


class TestApply:
    def test_single_line_cut_applied(self):
        lines = ["あいう"]
        llm = _queue_llm(["1: あ<cut>いう</cut>"])
        out, unapplied = apply_ops(lines, [_op("cut", 1, 1)], CFG, call_llm=llm)
        assert out == ["あ<cut>いう</cut>"]
        assert unapplied == []

    def test_cross_line_cut_only_touches_boundaries(self):
        lines = ["あい", "うえ", "おか"]
        # LLM returns the two boundary lines; middle stays untouched
        llm = _queue_llm(["1: あ<cut>い\n3: お</cut>か"])
        out, unapplied = apply_ops(lines, [_op("cut", 1, 3)], CFG, call_llm=llm)
        assert out == ["あ<cut>い", "うえ", "お</cut>か"]
        assert unapplied == []

    def test_rephrase_reverted_and_reported(self):
        lines = ["あいう"]
        # LLM rephrased 'う' -> 'え' outside markers
        llm = _queue_llm(["1: あ<cut>いえ</cut>"])
        out, unapplied = apply_ops(lines, [_op("cut", 1, 1)], CFG, call_llm=llm)
        assert out == ["あいう"]  # reverted
        assert len(unapplied) == 1 and unapplied[0][0].type == "cut"

    def test_no_change_reverted(self):
        lines = ["あいう"]
        llm = _queue_llm(["1: あいう"])  # nothing inserted
        out, unapplied = apply_ops(lines, [_op("cut", 1, 1)], CFG, call_llm=llm)
        assert out == ["あいう"]
        assert len(unapplied) == 1

    def test_llm_error_reported(self):
        lines = ["あいう"]

        def boom(_m, _c):
            raise ConnectionError("down")

        out, unapplied = apply_ops(lines, [_op("cut", 1, 1)], CFG, call_llm=boom)
        assert out == ["あいう"]
        assert len(unapplied) == 1

    def test_mixed_success_and_failure(self):
        lines = ["あいう", "かきく"]
        # op1 ok on line 1; op2 fails (no change) on line 2
        llm = _queue_llm(["1: あ<cut>いう</cut>", "2: かきく"])
        ops = [_op("cut", 1, 1), _op("speed", 2, 2, factor=2.0)]
        out, unapplied = apply_ops(lines, ops, CFG, call_llm=llm)
        assert out[0] == "あ<cut>いう</cut>"
        assert out[1] == "かきく"
        assert [u[0].type for u in unapplied] == ["speed"]
