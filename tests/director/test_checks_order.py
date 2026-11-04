"""The playback order as conversation state: sent in a reply, replaced whole.

The view is shooting order — every source whole, numbered once — so an order
is a list of display ranges, and the view never changes shape under it::

    1 あ1   2 あ2   3 [silent after あ2]   4 あ3   5 あ4   6 あ5
    7 い1   8 い2   9 い3

A source boundary sits after display line 6.
"""

from __future__ import annotations

from nagare_clip.director.checks import order_breaks, order_ranges, order_segments
from nagare_clip.director.display import build_display_view
from nagare_clip.director.load import SegmentTranscript
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


class TestTheView:
    def test_it_is_shooting_order_with_a_silence_line(self):
        view = _view()
        assert len(view.lines) == 9
        assert view.line(3).is_silence
        assert view.segment_join_after(6)


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
