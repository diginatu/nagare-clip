"""`snap_overlay_starts()`: an overlay anchored on cut footage moves to the
first surviving moment of its own line instead of being dropped."""

from nagare_clip.intervals.intervals import snap_overlay_starts

KEEP = [{"start": 4.488, "end": 12.638}, {"start": 14.0, "end": 18.0}]
# One transcript line spanning both keep intervals and the cut opening.
LINES = [(2.495, 18.751)]


def test_anchor_already_on_a_keep_interval_is_untouched():
    out = snap_overlay_starts([(5.0, 5.0, "recap")], LINES, KEEP)
    assert out == [(5.0, 5.0, "recap")]


def test_anchor_on_cut_opening_snaps_to_first_surviving_moment():
    """The water_pump_3 case: line 2.495-18.751, first keep at 4.488."""
    out = snap_overlay_starts([(2.495, 5.0, "recap")], LINES, KEEP)
    assert out == [(4.488, 5.0, "recap")]


def test_duration_is_not_clipped_to_the_surviving_footage():
    """A stated duration is reading time; snapping must not shorten it."""
    out = snap_overlay_starts([(2.495, 30.0, "recap")], LINES, KEEP)
    assert out == [(4.488, 30.0, "recap")]


def test_line_with_no_surviving_footage_is_left_alone():
    """Nowhere to put it — left off-keep so the blender stage warns and skips."""
    out = snap_overlay_starts(
        [(2.495, 5.0, "recap")],
        [(2.495, 4.0)],
        [
            {
                "start": 4.488,
                "end": 12.638,
            }
        ],
    )
    assert out == [(2.495, 5.0, "recap")]


def test_snap_stays_inside_the_marker_line():
    """A keep interval reaching in from the previous line must not pull the
    caption back before the line the marker sits on."""
    keep = [{"start": 1.0, "end": 3.0}]
    out = snap_overlay_starts([(2.495, 5.0, "recap")], [(2.0, 6.0)], keep)
    assert out == [(2.495, 5.0, "recap")]  # 2.495 is inside (1.0, 3.0) already

    # Same interval, but the anchor sits in the cut part after it.
    out = snap_overlay_starts([(4.0, 5.0, "recap")], [(2.0, 6.0)], keep)
    assert out == [(2.0, 5.0, "recap")]


def test_snap_prefers_a_later_surviving_chunk_over_an_earlier_one():
    """Mid-line anchor: the caption moves forward, not back."""
    keep = [{"start": 0.0, "end": 3.0}, {"start": 8.0, "end": 12.0}]
    out = snap_overlay_starts([(5.0, 2.0, "x")], [(0.0, 12.0)], keep)
    assert out == [(8.0, 2.0, "x")]


def test_anchor_at_a_keep_interval_end_is_not_treated_as_covered():
    """The blender stage's containment is half-open; snapping must agree."""
    keep = [{"start": 0.0, "end": 3.0}, {"start": 8.0, "end": 12.0}]
    out = snap_overlay_starts([(3.0, 2.0, "x")], [(0.0, 12.0)], keep)
    assert out == [(8.0, 2.0, "x")]


def test_line_with_unknown_times_is_left_alone():
    out = snap_overlay_starts([(2.495, 5.0, "recap")], [(None, None)], KEEP)
    assert out == [(2.495, 5.0, "recap")]


def test_anchor_outside_every_line_is_left_alone():
    out = snap_overlay_starts([(99.0, 5.0, "recap")], LINES, KEEP)
    assert out == [(99.0, 5.0, "recap")]


def test_no_keep_intervals_at_all_is_left_alone():
    out = snap_overlay_starts([(2.495, 5.0, "recap")], LINES, [])
    assert out == [(2.495, 5.0, "recap")]
