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


def _no_llm(_messages, _cfg):
    raise AssertionError("LLM must not be called for span ops")


class TestSpanDeterministic:
    """cut/speed/overlay/keep are pure line-range wraps — applied without any
    LLM call (the director already fixed the boundaries)."""

    def test_single_line_cut_wraps_whole_line(self):
        out, unapplied = apply_ops(["あいう"], [_op("cut", 1, 1)], CFG, call_llm=_no_llm)
        assert out == ["<cut>あいう</cut>"]
        assert unapplied == []

    def test_multi_line_speed_wraps_boundaries(self):
        out, unapplied = apply_ops(
            ["あい", "うえ", "おか"],
            [_op("speed", 1, 3, factor=2.0)],
            CFG,
            call_llm=_no_llm,
        )
        assert out == ['<speed factor="2.0">あい', "うえ", "おか</speed>"]
        assert unapplied == []

    def test_overlay_uses_text_attr(self):
        out, unapplied = apply_ops(
            ["あい", "うえ"],
            [_op("overlay", 1, 2, text="メモ")],
            CFG,
            call_llm=_no_llm,
        )
        assert out == ['<overlay text="メモ">あい', "うえ</overlay>"]
        assert unapplied == []

    def test_keep_single_line(self):
        out, unapplied = apply_ops(
            ["あいう"], [_op("keep", 1, 1)], CFG, call_llm=_no_llm
        )
        assert out == ["<keep>あいう</keep>"]
        assert unapplied == []

    def test_span_wraps_outside_existing_markers(self):
        # speed applied over a line that already carries an overlay + patches:
        # the whole line range is wrapped, underlying text untouched.
        lines = ['<overlay text="x">はい</overlay>{{で->}}これ', "おわり"]
        out, unapplied = apply_ops(
            lines, [_op("speed", 1, 2, factor=4.0)], CFG, call_llm=_no_llm
        )
        assert out[0] == '<speed factor="4.0"><overlay text="x">はい</overlay>{{で->}}これ'
        assert out[1] == "おわり</speed>"
        assert unapplied == []

    def test_clipped_around_existing_same_type_span(self):
        # lines 1-2 already carry a <speed> span; a director speed[1-4] is
        # clipped to the free tail [3-4] so the same-type tags stay disjoint.
        lines = ['<speed factor="3.0">L1', "L2</speed>", "L3", "L4"]
        out, unapplied = apply_ops(
            lines, [_op("speed", 1, 4, factor=2.0)], CFG, call_llm=_no_llm
        )
        assert out == [
            '<speed factor="3.0">L1',
            "L2</speed>",
            '<speed factor="2.0">L3',
            "L4</speed>",
        ]
        assert unapplied == []

    def test_fully_overlapping_same_type_dropped(self):
        lines = ['<speed factor="3.0">L1', "L2</speed>"]
        out, unapplied = apply_ops(
            lines, [_op("speed", 1, 2, factor=1.5)], CFG, call_llm=_no_llm
        )
        assert out == lines  # unchanged
        assert [u[0].type for u in unapplied] == ["speed"]

    def test_director_vs_director_overlap_first_wins(self):
        # op0 speed[1-3] applies; op1 speed[2-3] now fully overlaps -> dropped.
        lines = ["L1", "L2", "L3"]
        ops = [_op("speed", 1, 3, factor=4.0), _op("speed", 2, 3, factor=2.0)]
        out, unapplied = apply_ops(lines, ops, CFG, call_llm=_no_llm)
        assert out == ['<speed factor="4.0">L1', "L2", "L3</speed>"]
        assert [u[0].factor for u in unapplied] == [2.0]

    def test_clip_picks_largest_free_run(self):
        # occupied middle line splits [1-5] into runs [1-2] and [4-5] (tie ->
        # earliest), overlay clips to [1-2].
        lines = ["L1", "L2", '<overlay text="x">L3</overlay>', "L4", "L5"]
        out, unapplied = apply_ops(
            lines, [_op("overlay", 1, 5, text="メモ")], CFG, call_llm=_no_llm
        )
        assert out[0] == '<overlay text="メモ">L1'
        assert out[1] == "L2</overlay>"
        assert out[3] == "L4" and out[4] == "L5"
        assert unapplied == []

    def test_different_type_does_not_block(self):
        # an existing <overlay> never blocks a <speed> op (independent types).
        lines = ['<overlay text="x">L1</overlay>', "L2"]
        out, unapplied = apply_ops(
            lines, [_op("speed", 1, 2, factor=2.0)], CFG, call_llm=_no_llm
        )
        assert out[0] == '<speed factor="2.0"><overlay text="x">L1</overlay>'
        assert out[1] == "L2</speed>"
        assert unapplied == []

    def test_nested_span_ops_stay_balanced(self):
        # overlay applied after speed over the same range nests cleanly.
        lines = ["あい", "うえ"]
        ops = [_op("speed", 1, 2, factor=2.0), _op("overlay", 1, 2, text="メモ")]
        out, unapplied = apply_ops(lines, ops, CFG, call_llm=_no_llm)
        assert out[0] == '<overlay text="メモ"><speed factor="2.0">あい'
        assert out[1] == "うえ</speed></overlay>"
        assert unapplied == []


class TestApply:
    """edit ops are the only LLM-driven path; span ops are covered above."""

    def test_single_line_edit_applied(self):
        lines = ["あいう"]
        llm = _queue_llm(["1: あ{{いう->イウ}}"])
        out, unapplied = apply_ops(lines, [_op("edit", 1, 1)], CFG, call_llm=llm)
        assert out == ["あ{{いう->イウ}}"]
        assert unapplied == []

    def test_cross_line_edit_only_touches_boundaries(self):
        lines = ["あい", "うえ", "おか"]
        # LLM returns the two boundary lines; middle stays untouched
        llm = _queue_llm(["1: あ{{い->イ}}\n3: お{{か->カ}}"])
        out, unapplied = apply_ops(lines, [_op("edit", 1, 3)], CFG, call_llm=llm)
        assert out == ["あ{{い->イ}}", "うえ", "お{{か->カ}}"]
        assert unapplied == []

    def test_rephrase_reverted_and_reported(self):
        lines = ["あいう"]
        # LLM rephrased 'う' -> 'え' outside any patch
        llm = _queue_llm(["1: あいえ"])
        out, unapplied = apply_ops(lines, [_op("edit", 1, 1)], CFG, call_llm=llm)
        assert out == ["あいう"]  # reverted
        assert len(unapplied) == 1 and unapplied[0][0].type == "edit"

    def test_no_change_reverted(self):
        lines = ["あいう"]
        llm = _queue_llm(["1: あいう"])  # nothing changed
        out, unapplied = apply_ops(lines, [_op("edit", 1, 1)], CFG, call_llm=llm)
        assert out == ["あいう"]
        assert len(unapplied) == 1

    def test_llm_error_reported(self):
        lines = ["あいう"]

        def boom(_m, _c):
            raise ConnectionError("down")

        out, unapplied = apply_ops(lines, [_op("edit", 1, 1)], CFG, call_llm=boom)
        assert out == ["あいう"]
        assert len(unapplied) == 1

    def test_mixed_success_and_failure(self):
        lines = ["あいう", "かきく"]
        # op1 (edit) ok on line 1; op2 (edit) fails (no change) on line 2
        llm = _queue_llm(["1: あ{{いう->イウ}}", "2: かきく"])
        ops = [_op("edit", 1, 1), _op("edit", 2, 2)]
        out, unapplied = apply_ops(lines, ops, CFG, call_llm=llm)
        assert out[0] == "あ{{いう->イウ}}"
        assert out[1] == "かきく"
        assert [u[0].type for u in unapplied] == ["edit"]

    def test_span_and_edit_mixed(self):
        # span op applied deterministically, edit op via the queued LLM
        lines = ["あいう", "かきく"]
        llm = _queue_llm(["2: か{{きく->キク}}"])
        ops = [_op("keep", 1, 1), _op("edit", 2, 2)]
        out, unapplied = apply_ops(lines, ops, CFG, call_llm=llm)
        assert out == ["<keep>あいう</keep>", "か{{きく->キク}}"]
        assert unapplied == []


class TestRetry:
    def test_retry_on_llm_error_then_success(self):
        fake = _seq_llm([ConnectionError("x"), "1: あ{{いう->イウ}}"])
        out, unapplied = apply_ops(
            ["あいう"], [_op("edit", 1, 1)], {"prompt": "P", "max_retries": 2}, call_llm=fake
        )
        assert out == ["あ{{いう->イウ}}"]
        assert unapplied == []
        assert fake.calls["i"] == 2

    def test_retry_on_verify_fail_then_success(self):
        # first response makes no change (verify fails), second is correct
        fake = _seq_llm(["1: あいう", "1: あ{{いう->イウ}}"])
        out, unapplied = apply_ops(
            ["あいう"], [_op("edit", 1, 1)], {"prompt": "P", "max_retries": 2}, call_llm=fake
        )
        assert out == ["あ{{いう->イウ}}"]
        assert unapplied == []
        assert fake.calls["i"] == 2

    def test_all_attempts_fail_unapplied(self):
        fake = _seq_llm(["1: あいう"] * 3)  # always no change
        out, unapplied = apply_ops(
            ["あいう"], [_op("edit", 1, 1)], {"prompt": "P", "max_retries": 2}, call_llm=fake
        )
        assert out == ["あいう"]
        assert len(unapplied) == 1 and unapplied[0][0].type == "edit"
        assert fake.calls["i"] == 3

    def test_max_retries_zero_is_single_attempt(self):
        fake = _seq_llm(["1: あいう"])  # no change -> verify fails
        out, unapplied = apply_ops(
            ["あいう"], [_op("edit", 1, 1)], {"prompt": "P", "max_retries": 0}, call_llm=fake
        )
        assert len(unapplied) == 1
        assert fake.calls["i"] == 1

    def test_temperature_nudged_per_attempt(self):
        temps: list = []
        fake = _seq_llm(["1: あいう"] * 3, temps=temps)  # always fail
        apply_ops(
            ["あいう"],
            [_op("edit", 1, 1)],
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


# add to tests/guided_edit/test_apply.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder


def _fm(tmp_path, unit):
    text = (tmp_path / "guided_edit" / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)


class TestGuidedEditRecorder:
    def test_records_unapplied_op_as_dropped_items(self, tmp_path):
        rec = Recorder("guided_edit", tmp_path, enabled=True)
        op = DirectorOp(type="edit", lines=(1, 1), note="")
        # call_llm returns the line unchanged -> op never reflected -> verify fails ->
        # all retries exhausted -> op unapplied.

        def fake(_messages, _cfg):
            return "1: hello world"

        lines = ["hello world"]
        new_lines, unapplied = apply_ops(
            lines, [op], {"max_retries": 1}, call_llm=fake, recorder=rec, unit="vid",
        )
        assert len(unapplied) == 1
        fm = _fm(tmp_path, "vid")
        assert fm["outcome"] == "dropped-items"
        body = (tmp_path / "guided_edit" / "vid.md").read_text(encoding="utf-8")
        assert "op 0: edit" in body  # section header rendered

    def test_records_span_op_as_ok_without_llm(self, tmp_path):
        rec = Recorder("guided_edit", tmp_path, enabled=True)
        op = DirectorOp(type="keep", lines=(1, 1), note="")

        new_lines, unapplied = apply_ops(
            ["hello world"], [op], {"max_retries": 0}, call_llm=_no_llm,
            recorder=rec, unit="vid",
        )
        assert new_lines == ["<keep>hello world</keep>"]
        assert unapplied == []
        assert _fm(tmp_path, "vid")["outcome"] == "ok"
