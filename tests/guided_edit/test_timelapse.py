"""guided_edit: a timelapse op desugars into overlay + speed + keep."""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops


def _tl(a=1, b=3, factor=4.0, text="作業", note=""):
    return DirectorOp(type="timelapse", lines=(a, b), factor=factor, text=text, note=note)


# Lines 1..4 -> segments; line 1 starts at 10.0, line 3 ends at 50.0 (span 40s).
TIMES = [(10.0, 20.0), (20.0, 30.0), (30.0, 50.0), (50.0, 60.0)]


def test_expands_into_overlay_speed_keep_in_that_order():
    out = expand_timelapse_ops([_tl()], TIMES)
    assert [o.type for o in out] == ["overlay", "speed", "keep"]


def test_overlay_is_a_point_at_the_first_line_and_spans_run_the_range():
    out = expand_timelapse_ops([_tl(a=1, b=3)], TIMES)
    overlay, speed, keep = out
    assert overlay.lines == (1, 1)
    assert speed.lines == (1, 3)
    assert keep.lines == (1, 3)


def test_caption_duration_is_the_span_divided_by_the_factor():
    # (50.0 - 10.0) / 4.0 = 10.0
    out = expand_timelapse_ops([_tl(factor=4.0)], TIMES)
    assert out[0].duration == 10.0


def test_caption_duration_is_rounded_to_two_decimals():
    # (50.0 - 10.0) / 3.0 = 13.333... -> 13.33  (factor is not validated here;
    # the 4.0 floor is enforced at parse time)
    out = expand_timelapse_ops([_tl(factor=3.0)], TIMES)
    assert out[0].duration == 13.33


def test_caption_is_not_clamped_up_for_a_short_timelapse():
    # A 10s span at 8x is a 1.25s caption. A floor would let it outlive its own
    # span and collide with the next timelapse's caption on Blender's single
    # overlay channel.
    out = expand_timelapse_ops([_tl(a=1, b=1, factor=8.0)], TIMES)
    assert out[0].duration == 1.25


def test_derived_ops_carry_the_factor_and_the_note():
    out = expand_timelapse_ops([_tl(factor=6.0, note="配管作業")], TIMES)
    overlay, speed, keep = out
    assert speed.factor == 6.0
    assert keep.factor is None
    assert overlay.text == "作業"
    assert all(o.note == "配管作業" for o in out)


def test_no_text_yields_speed_and_keep_only():
    out = expand_timelapse_ops([_tl(text=None)], TIMES)
    assert [o.type for o in out] == ["speed", "keep"]


def test_missing_times_drop_the_caption_but_keep_the_continuity_fix(caplog):
    out = expand_timelapse_ops([_tl()], [])
    assert [o.type for o in out] == ["speed", "keep"]
    assert "caption dropped" in caplog.text


def test_unknown_boundary_time_drops_the_caption_only():
    times = [(None, 20.0), (20.0, 30.0), (30.0, 50.0)]
    out = expand_timelapse_ops([_tl()], times)
    assert [o.type for o in out] == ["speed", "keep"]


def test_other_ops_pass_through_unchanged_and_in_place():
    cut = DirectorOp(type="cut", lines=(4, 4))
    out = expand_timelapse_ops([cut, _tl(), cut], TIMES)
    assert [o.type for o in out] == ["cut", "overlay", "speed", "keep", "cut"]
    assert out[0] is cut and out[-1] is cut


def test_consecutive_timelapses_produce_captions_that_do_not_overlap():
    # Two phases back to back: each caption lasts exactly its own span / factor,
    # so the second starts where the first ends instead of stacking on it.
    times = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)]
    first = _tl(a=1, b=2, factor=4.0, text="第一段階")
    second = _tl(a=3, b=4, factor=4.0, text="第二段階")
    out = expand_timelapse_ops([first, second], times)
    captions = [o for o in out if o.type == "overlay"]
    assert [c.duration for c in captions] == [5.0, 5.0]
    assert [c.lines for c in captions] == [(1, 1), (3, 3)]


def test_apply_ops_reports_an_unexpanded_timelapse_instead_of_raising():
    """Defence in depth: expansion happens in run_guided_edit, but apply_ops is
    public. An unexpanded op must be reported, not crash _span_tags."""
    from nagare_clip.guided_edit.apply import apply_ops

    lines, unapplied = apply_ops(["あ", "い"], [_tl(a=1, b=2)], {})
    assert lines == ["あ", "い"]
    assert len(unapplied) == 1
    assert "unexpanded" in unapplied[0][1]
