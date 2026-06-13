"""Tests for guided_edit apply orchestration (fake LLM, no network)."""

from __future__ import annotations

import pytest

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.apply import apply_ops, build_user_prompt


def _op(t, a, b, **kw):
    return DirectorOp(type=t, lines=(a, b), **kw)


def _queue_llm(responses):
    it = iter(responses)

    def fake(_messages, _cfg):
        return next(it)

    return fake


def _seq_llm(items, temps=None):
    """Fake call_llm yielding *items*; an ``Exception`` item is raised.
    Records each call's temperature into *temps* if given."""
    box = {"i": 0}

    def fake(_messages, cfg):
        if temps is not None:
            temps.append(cfg.get("temperature"))
        item = items[box["i"]]
        box["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    fake.calls = box
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


class TestRetry:
    def test_retry_on_llm_error_then_success(self):
        fake = _seq_llm([ConnectionError("x"), "1: あ<cut>いう</cut>"])
        out, unapplied = apply_ops(
            ["あいう"], [_op("cut", 1, 1)], {"prompt": "P", "max_retries": 2}, call_llm=fake
        )
        assert out == ["あ<cut>いう</cut>"]
        assert unapplied == []
        assert fake.calls["i"] == 2

    def test_retry_on_verify_fail_then_success(self):
        # first response makes no change (verify fails), second is correct
        fake = _seq_llm(["1: あいう", "1: あ<cut>いう</cut>"])
        out, unapplied = apply_ops(
            ["あいう"], [_op("cut", 1, 1)], {"prompt": "P", "max_retries": 2}, call_llm=fake
        )
        assert out == ["あ<cut>いう</cut>"]
        assert unapplied == []
        assert fake.calls["i"] == 2

    def test_all_attempts_fail_unapplied(self):
        fake = _seq_llm(["1: あいう"] * 3)  # always no change
        out, unapplied = apply_ops(
            ["あいう"], [_op("cut", 1, 1)], {"prompt": "P", "max_retries": 2}, call_llm=fake
        )
        assert out == ["あいう"]
        assert len(unapplied) == 1 and unapplied[0][0].type == "cut"
        assert fake.calls["i"] == 3

    def test_max_retries_zero_is_single_attempt(self):
        fake = _seq_llm(["1: あいう"])  # no change -> verify fails
        out, unapplied = apply_ops(
            ["あいう"], [_op("cut", 1, 1)], {"prompt": "P", "max_retries": 0}, call_llm=fake
        )
        assert len(unapplied) == 1
        assert fake.calls["i"] == 1

    def test_temperature_nudged_per_attempt(self):
        temps: list = []
        fake = _seq_llm(["1: あいう"] * 3, temps=temps)  # always fail
        apply_ops(
            ["あいう"],
            [_op("cut", 1, 1)],
            {
                "prompt": "P",
                "temperature": 0.1,
                "max_retries": 2,
                "retry_temp_step": 0.2,
                "retry_temp_cap": 0.8,
            },
            call_llm=fake,
        )
        assert temps == [
            pytest.approx(0.1),
            pytest.approx(0.3),
            pytest.approx(0.5),
        ]
