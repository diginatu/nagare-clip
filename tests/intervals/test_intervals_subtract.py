"""Tests for subtract_intervals: carve cut ranges out of a base interval list."""

import pytest

from nagare_clip.intervals.intervals import subtract_intervals


def test_no_cuts_returns_base_unchanged():
    base = [(1.0, 5.0), (7.0, 9.0)]
    assert subtract_intervals(base, []) == [(1.0, 5.0), (7.0, 9.0)]


def test_no_base_returns_empty():
    assert subtract_intervals([], [(1.0, 2.0)]) == []


def test_disjoint_cut_is_noop():
    base = [(1.0, 5.0)]
    cuts = [(10.0, 12.0)]
    assert subtract_intervals(base, cuts) == [(1.0, 5.0)]


def test_cut_fully_inside_splits_into_two():
    base = [(0.0, 10.0)]
    cuts = [(3.0, 6.0)]
    assert subtract_intervals(base, cuts) == [(0.0, 3.0), (6.0, 10.0)]


def test_cut_covers_whole_interval_drops_it():
    base = [(2.0, 4.0)]
    cuts = [(1.0, 5.0)]
    assert subtract_intervals(base, cuts) == []


def test_cut_at_left_edge_trims_left():
    base = [(2.0, 8.0)]
    cuts = [(0.0, 4.0)]
    assert subtract_intervals(base, cuts) == [(4.0, 8.0)]


def test_cut_at_right_edge_trims_right():
    base = [(2.0, 8.0)]
    cuts = [(5.0, 10.0)]
    assert subtract_intervals(base, cuts) == [(2.0, 5.0)]


def test_cut_touches_left_boundary_exact_is_noop():
    """A cut that ends exactly at base.start carves nothing."""
    base = [(5.0, 10.0)]
    cuts = [(0.0, 5.0)]
    assert subtract_intervals(base, cuts) == [(5.0, 10.0)]


def test_cut_touches_right_boundary_exact_is_noop():
    """A cut that starts exactly at base.end carves nothing."""
    base = [(5.0, 10.0)]
    cuts = [(10.0, 15.0)]
    assert subtract_intervals(base, cuts) == [(5.0, 10.0)]


def test_multiple_cuts_one_interval():
    base = [(0.0, 20.0)]
    cuts = [(2.0, 4.0), (10.0, 12.0), (15.0, 18.0)]
    assert subtract_intervals(base, cuts) == [
        (0.0, 2.0),
        (4.0, 10.0),
        (12.0, 15.0),
        (18.0, 20.0),
    ]


def test_overlapping_cuts_collapse_correctly():
    """Cuts that overlap each other should still leave the right pieces."""
    base = [(0.0, 10.0)]
    cuts = [(2.0, 5.0), (4.0, 7.0)]
    assert subtract_intervals(base, cuts) == [(0.0, 2.0), (7.0, 10.0)]


def test_multiple_base_intervals_each_carved_independently():
    base = [(0.0, 10.0), (20.0, 30.0)]
    cuts = [(3.0, 6.0), (25.0, 27.0)]
    assert subtract_intervals(base, cuts) == [
        (0.0, 3.0),
        (6.0, 10.0),
        (20.0, 25.0),
        (27.0, 30.0),
    ]


def test_unsorted_input_handled():
    """Caller may pass unsorted base or cuts; result should still be correct."""
    base = [(20.0, 30.0), (0.0, 10.0)]
    cuts = [(25.0, 27.0), (3.0, 6.0)]
    result = subtract_intervals(base, cuts)
    # Result should be sorted ascending
    assert result == [
        (0.0, 3.0),
        (6.0, 10.0),
        (20.0, 25.0),
        (27.0, 30.0),
    ]


def test_zero_length_pieces_dropped():
    """If a cut leaves a zero-length residue, it must not appear in output."""
    base = [(0.0, 5.0)]
    cuts = [(0.0, 5.0)]  # exact cover
    assert subtract_intervals(base, cuts) == []


def test_returns_tuples_of_floats():
    """Output element type is consistent (tuple-like with start, end)."""
    base = [(0.0, 10.0)]
    cuts = [(4.0, 6.0)]
    result = subtract_intervals(base, cuts)
    for r in result:
        assert len(r) == 2
        assert isinstance(r[0], float)
        assert isinstance(r[1], float)
        assert r[0] < r[1]
