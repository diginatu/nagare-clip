"""Tests for guided_edit apply orchestration (deterministic, no LLM)."""

from __future__ import annotations

from nagare_clip.director.ops import DirectorOp
from nagare_clip.guided_edit.apply import apply_ops


def _op(t, a, b, **kw):
    return DirectorOp(type=t, lines=(a, b), **kw)


class TestSpanDeterministic:
    """cut/speed/overlay/keep are pure line-range wraps — applied without any
    LLM call (the director already fixed the boundaries)."""

    def test_single_line_cut_wraps_whole_line(self):
        out, unapplied = apply_ops(["あいう"], [_op("cut", 1, 1)])
        assert out == ["<cut>あいう</cut>"]
        assert unapplied == []

    def test_multi_line_speed_wraps_boundaries(self):
        out, unapplied = apply_ops(["あい", "うえ", "おか"], [_op("speed", 1, 3, factor=2.0)])
        assert out == ['<speed factor="2.0">あい', "うえ", "おか</speed>"]
        assert unapplied == []

    def test_overlay_inserts_a_point_marker_on_the_first_line(self):
        # An overlay is a point + a stated duration: no closing tag, so the
        # rest of the range is untouched and no tag placement can stretch it.
        out, unapplied = apply_ops(
            ["あい", "うえ"], [_op("overlay", 1, 2, text="メモ", duration=3.0)]
        )
        assert out == ['<overlay text="メモ" duration="3.0"/>あい', "うえ"]
        assert unapplied == []

    def test_keep_single_line(self):
        out, unapplied = apply_ops(["あいう"], [_op("keep", 1, 1)])
        assert out == ["<keep>あいう</keep>"]
        assert unapplied == []

    def test_overlay_text_line_break_is_escaped_into_the_marker(self):
        # A two-line caption must not become two _edits.txt lines: an edit line
        # maps 1:1 to a WhisperX segment, so a raw newline spliced into the
        # marker shifts every later line off its segment (the intervals stage
        # then rejects the file as "text changed without {{old->new}}").
        out, unapplied = apply_ops(
            ["あい", "うえ"], [_op("overlay", 1, 1, text="問題①：A\n問題②：B", duration=5.0)]
        )
        assert out == ['<overlay text="問題①：A\\n問題②：B" duration="5.0"/>あい', "うえ"]
        assert unapplied == []

    def test_overlay_text_backslash_is_escaped(self):
        # A literal backslash must survive the round trip distinguishable from
        # an encoded line break.
        out, _ = apply_ops(["あい"], [_op("overlay", 1, 1, text="a\\nb", duration=2.0)])
        assert out == ['<overlay text="a\\\\nb" duration="2.0"/>あい']


class TestOverlayEditLineInvariant:
    """No applied op may put a newline inside an edit line.

    ``_edits.txt`` is written with ``"\\n".join(lines)``, so a newline inside a
    list element silently becomes an extra physical line at write time — after
    every in-memory guard has already passed.
    """

    def test_written_file_keeps_one_line_per_segment(self):
        lines = ["あい", "うえ", "おか"]
        out, unapplied = apply_ops(lines, [_op("overlay", 2, 2, text="上\n下", duration=4.0)])
        assert unapplied == []
        assert "\n".join(out).split("\n") == out
        assert len("\n".join(out).split("\n")) == len(lines)

    def test_span_wraps_outside_existing_markers(self):
        # speed applied over a line that already carries an overlay + patches:
        # the whole line range is wrapped, underlying text untouched.
        lines = ['<overlay text="x" duration="2.0"/>はい{{で->}}これ', "おわり"]
        out, unapplied = apply_ops(lines, [_op("speed", 1, 2, factor=4.0)])
        assert out[0] == '<speed factor="4.0"><overlay text="x" duration="2.0"/>はい{{で->}}これ'
        assert out[1] == "おわり</speed>"
        assert unapplied == []

    def test_clipped_around_existing_same_type_span(self):
        # lines 1-2 already carry a <speed> span; a director speed[1-4] is
        # clipped to the free tail [3-4] so the same-type tags stay disjoint.
        lines = ['<speed factor="3.0">L1', "L2</speed>", "L3", "L4"]
        out, unapplied = apply_ops(lines, [_op("speed", 1, 4, factor=2.0)])
        assert out == [
            '<speed factor="3.0">L1',
            "L2</speed>",
            '<speed factor="2.0">L3',
            "L4</speed>",
        ]
        assert unapplied == []

    def test_fully_overlapping_same_type_dropped(self):
        lines = ['<speed factor="3.0">L1', "L2</speed>"]
        out, unapplied = apply_ops(lines, [_op("speed", 1, 2, factor=1.5)])
        assert out == lines  # unchanged
        assert [u[0].type for u in unapplied] == ["speed"]

    def test_director_vs_director_overlap_first_wins(self):
        # op0 speed[1-3] applies; op1 speed[2-3] now fully overlaps -> dropped.
        lines = ["L1", "L2", "L3"]
        ops = [_op("speed", 1, 3, factor=4.0), _op("speed", 2, 3, factor=2.0)]
        out, unapplied = apply_ops(lines, ops)
        assert out == ['<speed factor="4.0">L1', "L2", "L3</speed>"]
        assert [u[0].factor for u in unapplied] == [2.0]

    def test_clip_picks_largest_free_run(self):
        # occupied middle line splits [1-5] into runs [1-2] and [4-5] (tie ->
        # earliest), keep clips to [1-2].
        lines = ["L1", "L2", "<keep>L3</keep>", "L4", "L5"]
        out, unapplied = apply_ops(lines, [_op("keep", 1, 5)])
        assert out[0] == "<keep>L1"
        assert out[1] == "L2</keep>"
        assert out[3] == "L4" and out[4] == "L5"
        assert unapplied == []

    def test_overlay_clips_past_a_line_that_already_carries_a_marker(self):
        # Two overlays on one line would stack on screen; the op moves to the
        # first free line of its range instead.
        lines = ['<overlay text="x" duration="2.0"/>L1', "L2"]
        out, unapplied = apply_ops(lines, [_op("overlay", 1, 2, text="メモ", duration=1.5)])
        assert out[0] == '<overlay text="x" duration="2.0"/>L1'
        assert out[1] == '<overlay text="メモ" duration="1.5"/>L2'
        assert unapplied == []

    def test_overlay_fully_blocked_is_dropped(self):
        lines = ['<overlay text="x" duration="2.0"/>L1']
        out, unapplied = apply_ops(lines, [_op("overlay", 1, 1, text="メモ", duration=1.5)])
        assert out == lines
        assert [u[0].type for u in unapplied] == ["overlay"]

    def test_different_type_does_not_block(self):
        # an existing <overlay/> never blocks a <speed> op (independent types).
        lines = ['<overlay text="x" duration="2.0"/>L1', "L2"]
        out, unapplied = apply_ops(lines, [_op("speed", 1, 2, factor=2.0)])
        assert out[0] == '<speed factor="2.0"><overlay text="x" duration="2.0"/>L1'
        assert out[1] == "L2</speed>"
        assert unapplied == []

    def test_overlay_marker_and_span_ops_coexist(self):
        # overlay applied after speed over the same range: the marker sits
        # outside the span tags, which stay balanced.
        lines = ["あい", "うえ"]
        ops = [_op("speed", 1, 2, factor=2.0), _op("overlay", 1, 2, text="メモ", duration=3.0)]
        out, unapplied = apply_ops(lines, ops)
        assert out[0] == '<overlay text="メモ" duration="3.0"/><speed factor="2.0">あい'
        assert out[1] == "うえ</speed>"
        assert unapplied == []


class TestCutExclusive:
    """`<cut>` is destructive: the intervals stage deletes whatever it wraps,
    silently swallowing any other tag caught inside. Cut ops are therefore
    applied after all other span ops and clip against lines occupied by ANY
    tag type; conversely non-cut ops clip against existing `<cut>` spans."""

    def test_cut_clips_around_later_keep_op(self):
        # The real-world failure: the director emitted cut[1-3] AND keep[3-3]
        # ("keep only the conclusion at 3"). The keep must win: it is applied
        # first and the cut clips to [1-2].
        lines = ["L1", "L2", "L3"]
        ops = [_op("cut", 1, 3), _op("keep", 3, 3)]
        out, unapplied = apply_ops(lines, ops)
        assert out == ["<cut>L1", "L2</cut>", "<keep>L3</keep>"]
        assert unapplied == []

    def test_cut_clips_around_an_existing_overlay_marker(self):
        # <cut> would swallow the marker into its deletion patch.
        lines = ['<overlay text="x" duration="2.0"/>L1', "L2"]
        out, unapplied = apply_ops(lines, [_op("cut", 1, 2)])
        assert out == ['<overlay text="x" duration="2.0"/>L1', "<cut>L2</cut>"]
        assert unapplied == []

    def test_cut_clips_around_existing_speed_span(self):
        lines = ['<speed factor="2.0">L1', "L2</speed>", "L3"]
        out, unapplied = apply_ops(lines, [_op("cut", 1, 3)])
        assert out == ['<speed factor="2.0">L1', "L2</speed>", "<cut>L3</cut>"]
        assert unapplied == []

    def test_cut_fully_inside_keep_dropped(self):
        lines = ["<keep>L1", "L2</keep>"]
        out, unapplied = apply_ops(lines, [_op("cut", 1, 2)])
        assert out == lines
        assert [u[0].type for u in unapplied] == ["cut"]

    def test_non_cut_op_clips_around_existing_cut(self):
        lines = ["<cut>L1</cut>", "L2"]
        out, unapplied = apply_ops(lines, [_op("keep", 1, 2)])
        assert out == ["<cut>L1</cut>", "<keep>L2</keep>"]
        assert unapplied == []


class TestEdit:
    """An edit op is one exact {{old->new}} patch, applied with no LLM."""

    def test_single_line_edit_applied(self):
        out, unapplied = apply_ops(["あいう"], [_op("edit", 1, 1, old="いう", new="イウ")])
        assert out == ["あ{{いう->イウ}}"]
        assert unapplied == []

    def test_deletion(self):
        out, unapplied = apply_ops(["えーと今日は"], [_op("edit", 1, 1, old="えーと", new="")])
        assert out == ["{{えーと->}}今日は"]
        assert unapplied == []

    def test_old_not_in_line_is_unapplied(self):
        out, unapplied = apply_ops(["あいう"], [_op("edit", 1, 1, old="か", new="")])
        assert out == ["あいう"]
        assert len(unapplied) == 1 and "does not occur" in unapplied[0][1]

    def test_multi_line_edit_is_unapplied(self):
        out, unapplied = apply_ops(["あ", "い"], [_op("edit", 1, 2, old="あ", new="")])
        assert out == ["あ", "い"]
        assert "exactly one line" in unapplied[0][1]

    def test_edit_without_old_is_unapplied(self):
        out, unapplied = apply_ops(["あいう"], [_op("edit", 1, 1)])
        assert out == ["あいう"]
        assert len(unapplied) == 1

    def test_span_and_edit_mixed(self):
        ops = [_op("keep", 1, 1), _op("edit", 2, 2, old="きく", new="キク")]
        out, unapplied = apply_ops(["あいう", "かきく"], ops)
        assert out == ["<keep>あいう</keep>", "か{{きく->キク}}"]
        assert unapplied == []

    def test_edit_lands_inside_a_span_op_on_the_same_line(self):
        ops = [_op("edit", 1, 1, old="いう", new="イウ"), _op("cut", 1, 1)]
        out, unapplied = apply_ops(["あいう"], ops)
        # cut is blocked by the patch (cut deletes whatever it wraps)
        assert out[0].startswith("あ{{いう->イウ}}") or "<cut>" in out[0]
        assert apply_ops(["あいう"], [ops[0]])[0] == ["あ{{いう->イウ}}"]
