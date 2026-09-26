"""The playback order as conversation state: sent in a reply, replaced whole.

The view is shooting order — every source whole, numbered once — so an order
is a list of display ranges, and the view never changes shape under it::

    1 あ1   2 あ2   3 [silent after あ2]   4 あ3   5 あ4   6 あ5
    7 い1   8 い2   9 い3

A source boundary sits after display line 6.
"""

from __future__ import annotations

import json

from nagare_clip.director.display import build_display_view
from nagare_clip.director.loop import (
    LoopState,
    apply_reply,
    order_breaks,
    order_ranges,
    order_segments,
)
from nagare_clip.director.run import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.order import Segment

A = SegmentTranscript(
    edit_lines=["あ1", "あ2", "あ3", "あ4", "あ5"],
    first_line=1,
    seg_times=[(0.0, 1.0), (1.5, 2.5), (10.0, 11.0), (11.5, 12.5), (13.0, 14.0)],
    silences=None,
    gaps=[],
    silence_lines=[SilenceLine(2, 2.5, 10.0, ())],
)
B = SegmentTranscript(
    edit_lines=["い1", "い2", "い3"],
    first_line=1,
    seg_times=[(0.0, 1.0), (1.5, 2.5), (3.0, 4.0)],
    silences=None,
    gaps=[],
    silence_lines=[],
)


def _view():
    return build_display_view([(Segment("A"), A), (Segment("B"), B)])


def _reply(order=None, range_=(1, 9), ops=(), **extra):
    data = {"range": list(range_), "reviewed_through": range_[1], "ops": list(ops), **extra}
    if order is not None:
        data["order"] = order
    return json.dumps(data)


def _timelapse(first, last):
    return {"type": "timelapse", "lines": [first, last], "factor": 8.0, "text": "作業", "note": ""}


def _cut(first, last):
    return {"type": "cut", "lines": [first, last], "note": "x"}


class TestTheView:
    def test_it_is_shooting_order_with_a_silence_line(self):
        view = _view()
        assert len(view.lines) == 9
        assert view.line(3).is_silence
        assert view.segment_join_after(6)


class TestAnOrderIsAccepted:
    def test_it_becomes_the_state(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(view, state, _reply([[4, 6], [1, 3], [7, 9]]))
        assert result.reordered and result.refusal is None
        assert state.order == [(4, 6), (1, 3), (7, 9)]

    def test_a_range_ending_on_a_silence_line_keeps_that_silence(self):
        segments = order_segments(_view(), [(4, 6), (1, 3), (7, 9)])
        assert segments == [
            Segment("A", (3, 5)),
            Segment("A", (1, 2), gap_end=True),
            Segment("B", (1, 3)),
        ]

    def test_a_range_starting_on_a_silence_line_starts_at_the_next_line(self):
        segments = order_segments(_view(), [(3, 6), (1, 2), (7, 9)])
        assert segments == [Segment("A", (3, 5)), Segment("A", (1, 2)), Segment("B", (1, 3))]

    def test_a_range_over_a_source_boundary_is_two_segments(self):
        segments = order_segments(_view(), [(5, 9), (1, 4)])
        assert segments == [Segment("A", (4, 5)), Segment("B", (1, 3)), Segment("A", (1, 3))]

    def test_one_line_is_a_range(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply([[5, 5], [1, 4], [6, 9]]))
        assert state.order == [(5, 5), (1, 4), (6, 9)]

    def test_the_last_reply_replaces_it_whole(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply([[4, 6], [1, 3], [7, 9]]))
        apply_reply(view, state, _reply([[7, 9], [1, 6]]))
        assert state.order == [(7, 9), (1, 6)]

    def test_a_reply_without_it_keeps_the_order_in_force(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply([[7, 9], [1, 6]]))
        result = apply_reply(view, state, _reply(ops=[_cut(1, 1)]))
        assert state.order == [(7, 9), (1, 6)]
        assert not result.reordered

    def test_it_may_come_alone(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply(ops=[_cut(1, 1)], range_=(1, 4)))
        result = apply_reply(view, state, json.dumps({"order": [[7, 9], [1, 6]]}))
        assert result.error is None and result.reordered
        assert state.order == [(7, 9), (1, 6)]
        assert state.reviewed_through == 4  # nothing else moved
        assert sum(len(v) for v in state.ops.values()) == 1

    def test_alone_it_cannot_carry_ops(self):
        result = apply_reply(
            _view(), LoopState(plan="p"), json.dumps({"order": [[1, 9]], "ops": [_cut(1, 1)]})
        )
        assert result.error is not None


class TestAnOrderIsRefused:
    def _refused(self, order):
        view, state = _view(), LoopState(plan="p", order=[(7, 9), (1, 6)])
        result = apply_reply(view, state, _reply(order, ops=[_cut(1, 1)]))
        assert state.order == [(7, 9), (1, 6)], "the previous order stays"
        assert result.ops, "the reply's ops still land"
        assert result.refusal and "order not applied" in result.refusal
        return result.refusal

    def test_a_missing_line(self):
        assert "lines 7-9 are in no range" in self._refused([[1, 6]])

    def test_a_line_twice(self):
        assert "more than one range" in self._refused([[1, 6], [6, 9]])

    def test_a_line_outside_the_video(self):
        assert "not a [first, last] range" in self._refused([[1, 10]])

    def test_not_a_list(self):
        assert "non-empty list" in self._refused("1-9")

    def test_a_range_of_only_a_silence_line(self):
        assert "only a silence line" in self._refused([[3, 3], [1, 2], [4, 9]])


class TestTimelapses:
    """An order break inside one source would cut a timelapse's speed-up in
    two with its caption on one side only; cut/keep survive it."""

    def test_an_order_that_splits_one_is_refused(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply(ops=[_timelapse(4, 6)]))
        result = apply_reply(view, state, _reply([[1, 5], [7, 9], [6, 6]], range_=(7, 9)))
        assert state.order == []
        assert "splits timelapse [4,6]" in result.refusal

    def test_one_reply_may_move_the_timelapse_out_of_the_way(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply(ops=[_timelapse(4, 6)]))
        result = apply_reply(
            view, state, _reply([[1, 5], [7, 9], [6, 6]], range_=(4, 6), ops=[_timelapse(4, 5)])
        )
        assert result.refusal is None
        assert state.order == [(1, 5), (7, 9), (6, 6)]

    def test_a_new_one_across_a_break_is_refused(self):
        view, state = _view(), LoopState(plan="p", order=[(1, 5), (7, 9), (6, 6)])
        result = apply_reply(view, state, _reply(ops=[_timelapse(4, 6)]))
        assert "crosses the order's break after line 5" in result.refusal
        assert not state.ops

    def test_a_cut_across_a_break_lands(self):
        view, state = _view(), LoopState(plan="p", order=[(1, 5), (7, 9), (6, 6)])
        result = apply_reply(view, state, _reply(ops=[_cut(4, 6)]))
        assert result.refusal is None and result.ops


class TestBreaks:
    def test_ranges_played_back_to_back_are_no_break(self):
        assert order_breaks([(1, 3), (4, 9)], 9) == set()

    def test_a_jump_is_a_break(self):
        assert order_breaks([(4, 6), (1, 3), (7, 9)], 9) == {6, 3}

    def test_the_last_line_is_never_one(self):
        assert order_breaks([(7, 9), (1, 6)], 9) == {6}


class TestSeeding:
    """The plan's order, in source coordinates, as the conversation's start."""

    def test_a_segment_takes_the_silence_before_it(self):
        seed = [Segment("A", (3, 5)), Segment("A", (1, 2)), Segment("B")]
        assert order_ranges(_view(), seed) == [(3, 6), (1, 2), (7, 9)]

    def test_unless_the_segment_before_it_ends_on_that_silence(self):
        seed = [Segment("A", (3, 5)), Segment("A", (1, 2), gap_end=True), Segment("B")]
        assert order_ranges(_view(), seed) == [(4, 6), (1, 3), (7, 9)]

    def test_it_round_trips(self):
        view = _view()
        order = [(4, 6), (1, 3), (7, 9)]
        assert order_ranges(view, order_segments(view, order)) == order

    def test_an_order_that_does_not_tile_is_shooting_order(self):
        assert order_ranges(_view(), [Segment("A", (3, 5)), Segment("B")]) == []


class TestWithDone:
    def test_an_order_sent_with_done_is_applied(self):
        view, state = _view(), LoopState(plan="p", reviewed_through=9)
        result = apply_reply(view, state, json.dumps({"done": True, "order": [[7, 9], [1, 6]]}))
        assert result.done and result.reordered
        assert state.order == [(7, 9), (1, 6)]

    def test_a_refused_one_is_not_done(self):
        view, state = _view(), LoopState(plan="p", reviewed_through=9)
        result = apply_reply(view, state, json.dumps({"done": True, "order": [[1, 6]]}))
        assert not result.done
        assert "order not applied" in result.refusal
