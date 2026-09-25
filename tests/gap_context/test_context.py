from nagare_clip.gap_context.context import (
    anchor_gaps,
    format_gap_block,
)
from nagare_clip.gap_context.gaps import Gap

SEG_TIMES = [(0.0, 10.0), (20.0, 25.0), (25.5, 30.0)]
GAP = Gap(start=10.0, end=20.0, frames=[], description="ビルドが走る")


def test_anchor_gaps_attaches_to_the_last_line_starting_before_the_gap_midpoint():
    assert anchor_gaps([GAP], SEG_TIMES) == [(1, GAP)]


def test_anchor_gaps_before_the_first_line_anchors_to_zero():
    g = Gap(start=0.0, end=5.0, frames=[], description="d")
    assert anchor_gaps([g], [(6.0, 10.0)]) == [(0, g)]


def test_anchor_gaps_with_no_segment_times():
    assert anchor_gaps([GAP], []) == [(0, GAP)]


def test_anchor_gaps_skips_static_gaps():
    # Static gaps carry no editorial signal (dead air is dropped by default
    # anyway); they must not reach the summary/director prompts. A human can
    # flip "static": false in {stem}_gaps.json to force one back in.
    gaps = [
        Gap(start=1.0, end=5.0, description="action"),
        Gap(start=6.0, end=9.0, description="dead air", static=True),
    ]
    anchored = anchor_gaps(gaps, [(0.0, 0.5)])
    assert [g.description for _, g in anchored] == ["action"]


def test_anchor_gaps_empty():
    assert anchor_gaps([], SEG_TIMES) == []


def test_format_gap_block():
    assert format_gap_block([(1, GAP)]) == (
        "## Silent gaps (visual context)\n- after line 1 (10.0s-20.0s, 10.0s): ビルドが走る"
    )


def test_format_gap_block_before_the_first_line():
    g = Gap(start=0.0, end=5.0, frames=[], description="タイトル画面")
    assert format_gap_block([(0, g)]) == (
        "## Silent gaps (visual context)\n- before line 1 (0.0s-5.0s, 5.0s): タイトル画面"
    )


def test_format_gap_block_empty_is_empty_string():
    assert format_gap_block([]) == ""


def test_anchor_gaps_picks_the_last_line_before_the_midpoint_not_the_first():
    """Regression: anchor_gaps should pick the LAST matching line, not the first."""
    # Several lines start before the gap's midpoint (15.0)—take the LAST one.
    seg_times = [(0.0, 3.0), (3.5, 6.0), (6.5, 10.0), (20.0, 25.0)]
    gap = Gap(start=10.0, end=20.0, frames=[], description="test")
    anchored = anchor_gaps([gap], seg_times)
    # Line 3 (starts at 6.5) is the last to match, not line 1 (starts at 0.0)
    assert anchored == [(3, gap)]


def test_anchor_gaps_midpoint_not_start_decides_the_anchor():
    """The midpoint, not the start, picks the line.

    Two gaps that begin at the same instant but end in different places belong
    to different lines: the one that is mostly over before line 2 gets going
    belongs to line 1, the one that mostly runs on through line 2 belongs to
    line 2.  The old rule keyed on ``gap.start`` alone and put both on line 1.
    """
    seg_times = [(0.0, 10.0), (11.0, 40.0)]
    short = Gap(start=10.0, end=11.5, frames=[], description="short")
    long = Gap(start=10.0, end=30.0, frames=[], description="long")
    assert anchor_gaps([short], seg_times) == [(1, short)]
    assert anchor_gaps([long], seg_times) == [(2, long)]


def test_anchor_gaps_line_starting_exactly_on_the_midpoint_owns_the_gap():
    """Tie boundary: half the gap is line 2's lead-in, so line 2 takes it."""
    seg_times = [(0.0, 10.0), (15.0, 40.0)]
    gap = Gap(start=10.0, end=20.0, frames=[], description="exactly halfway")
    assert anchor_gaps([gap], seg_times) == [(2, gap)]


def test_anchor_gaps_a_gap_inside_a_line_span_anchors_to_that_line():
    """Bucket (ii): sentence_split declines to split a silence whose midpoint
    falls inside a stretched word, so the silence stays INSIDE the following
    line's span, where format_dur_gap reports it as that line's own
    ``Ys silence``.  The annotation must sit on that line, not above it."""
    seg_times = [(0.0, 10.0), (12.0, 40.0)]
    gap = Gap(start=20.0, end=30.0, frames=[], description="inside line 2")
    assert anchor_gaps([gap], seg_times) == [(2, gap)]


def test_anchor_gaps_gap_starting_inside_a_stretched_tail_anchors_after_it():
    """Bucket (iii), the real line-64/65 fixture from PXL_20260328_082352713.

    WhisperX stretched line 65's final word 0.157s past the start of the 35s
    silence that follows it.  Under the old ``end <= gap.start`` rule line 65
    failed to qualify and 35 seconds of footage belonging to the gap AFTER
    line 65 were advertised under line 64's 12.1s gap.
    """
    seg_times = [(626.693, 629.016), (641.163, 645.047), (695.197, 700.0)]
    gap = Gap(start=644.890, end=680.160, frames=[], description="unscrews a fitting")
    assert anchor_gaps([gap], seg_times) == [(2, gap)]


class TestAnchorGapsOnASegment:
    """A split source's director call sees only its own segment's gaps."""

    # Line k ends at TIMES[k-1][1]: 1.0, 3.0, 5.0, 7.0, 9.0.
    TIMES = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (6.0, 7.0), (8.0, 9.0)]

    def _gap(self, start, end, desc="動く"):
        return Gap(start=start, end=end, frames=[], description=desc)

    def test_a_gap_inside_the_segment_is_rebased_onto_it(self):
        # 5.2s follows line 3; for a segment starting at line 3 that is its
        # own second position.
        gaps = [self._gap(5.2, 5.9)]
        assert anchor_gaps(gaps, self.TIMES, lines=(3, 5)) == [(1, gaps[0])]

    def test_a_gap_before_the_segment_is_dropped(self):
        # 1.2s follows line 1, which this segment does not cover.
        assert anchor_gaps([self._gap(1.2, 1.9)], self.TIMES, lines=(3, 5)) == []

    def test_the_gap_that_precedes_the_segment_first_line_belongs_to_it(self):
        # The manifest gives a segment the silent gap before its first line, so
        # the annotation the director sees agrees with the footage it gets.
        gaps = [self._gap(5.2, 5.9)]
        assert anchor_gaps(gaps, self.TIMES, lines=(4, 5)) == [(0, gaps[0])]

    def test_a_gap_after_the_segment_last_line_goes_to_the_next_segment(self):
        # 7.2s follows line 4; a segment ending at line 4 does not own it.
        assert anchor_gaps([self._gap(7.2, 7.9)], self.TIMES, lines=(3, 4)) == []

    def test_the_trailing_gap_of_the_source_stays_with_its_last_segment(self):
        # Nothing plays after it inside this source, so there is no next
        # segment to hand it to.
        gaps = [self._gap(9.2, 9.9)]
        assert anchor_gaps(gaps, self.TIMES, lines=(4, 5)) == [(2, gaps[0])]

    def test_a_whole_source_segment_anchors_exactly_as_before(self):
        gaps = [self._gap(1.2, 1.9), self._gap(9.2, 9.9)]
        assert anchor_gaps(gaps, self.TIMES, lines=(1, 5)) == anchor_gaps(gaps, self.TIMES)
        assert anchor_gaps(gaps, self.TIMES, lines=None) == anchor_gaps(gaps, self.TIMES)
