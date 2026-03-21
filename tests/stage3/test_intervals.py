"""Tests for interval manipulation functions."""

import pytest

from nagare_clip.stage3.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
)


# apply_margins


def test_apply_margins_basic_expansion():
    intervals = [{"start": 5.0, "end": 10.0}]
    result = apply_margins(intervals, 1.0, 1.0, 20.0)
    assert result == [{"start": 4.0, "end": 11.0}]


def test_apply_margins_clamps_bounds():
    intervals = [{"start": 0.5, "end": 19.5}]
    result = apply_margins(intervals, 1.0, 1.0, 20.0)
    assert result == [{"start": 0.0, "end": 20.0}]


def test_apply_margins_no_merge_after_expansion():
    intervals = [{"start": 5.0, "end": 10.0}, {"start": 13.0, "end": 18.0}]
    result = apply_margins(intervals, 1.0, 1.0, 30.0)
    assert result == [{"start": 4.0, "end": 11.0}, {"start": 12.0, "end": 19.0}]


def test_apply_margins_merges_after_expansion():
    intervals = [{"start": 5.0, "end": 10.0}, {"start": 11.5, "end": 18.0}]
    result = apply_margins(intervals, 1.0, 1.0, 30.0)
    assert result == [{"start": 4.0, "end": 19.0}]


def test_apply_margins_empty_input():
    assert apply_margins([], 1.0, 1.0, 20.0) == []


def test_apply_margins_zero_margins_no_change():
    intervals = [{"start": 5.0, "end": 10.0}]
    result = apply_margins(intervals, 0.0, 0.0, 20.0)
    assert result == intervals


# ensure_keep_covers_captions


def test_ensure_keep_covers_captions_adds_missing_ranges():
    keep_intervals = [{"start": 0.0, "end": 2.0}]
    captions = [
        {"start": 1.2, "end": 1.6, "text": "inside"},
        {"start": 3.0, "end": 3.4, "text": "outside"},
    ]

    result = ensure_keep_covers_captions(keep_intervals, captions, duration_sec=5.0)

    assert result == [{"start": 0.0, "end": 2.0}, {"start": 3.0, "end": 3.4}]


def test_ensure_keep_covers_captions_merges_overlaps():
    keep_intervals = [{"start": 5.0, "end": 6.0}]
    captions = [
        {"start": 5.5, "end": 6.5, "text": "overlap"},
        {"start": 6.5, "end": 7.0, "text": "touch"},
    ]

    result = ensure_keep_covers_captions(keep_intervals, captions, duration_sec=10.0)

    assert result == [{"start": 5.0, "end": 7.0}]


# enforce_min_keep_duration


def test_enforce_min_keep_duration_expands_short_intervals():
    keep_intervals = [{"start": 5.0, "end": 5.2}, {"start": 9.8, "end": 10.0}]

    result = enforce_min_keep_duration(keep_intervals, min_keep=1.0, duration_sec=10.0)

    assert result == [{"start": 4.6, "end": 5.6}, {"start": 9.0, "end": 10.0}]


def test_enforce_min_keep_duration_merges_after_expansion():
    keep_intervals = [{"start": 5.0, "end": 5.2}, {"start": 5.9, "end": 6.1}]

    result = enforce_min_keep_duration(keep_intervals, min_keep=1.0, duration_sec=10.0)

    assert result == [{"start": 4.6, "end": 6.5}]


# apply_margins integration


def test_apply_margins_real_intervals_no_merge():
    intervals = [
        {"start": 5.903, "end": 87.43},
        {"start": 89.857, "end": 109.502},
    ]
    result = apply_margins(intervals, 1.0, 1.0, 109.502)

    assert len(result) == 2
    assert result[0] == pytest.approx({"start": 4.903, "end": 88.43})
    assert result[1] == pytest.approx({"start": 88.857, "end": 109.502})


def test_apply_margins_real_intervals_merge_with_larger_padding():
    intervals = [
        {"start": 5.903, "end": 87.43},
        {"start": 89.857, "end": 109.502},
    ]
    result = apply_margins(intervals, 2.0, 2.0, 109.502)

    assert len(result) == 1
    assert result[0] == pytest.approx({"start": 3.903, "end": 109.502})
