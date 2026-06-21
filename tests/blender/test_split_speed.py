"""Tests for split_intervals_by_speed: splitting keep intervals at top-level
speed-range boundaries so each sub-interval has a single uniform speed_factor.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub bpy so we can import timeline without Blender
sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.blender.timeline import split_intervals_by_speed


def test_no_speed_ranges_returns_intervals_unchanged():
    keep = [{"start": 0.0, "end": 4.0}, {"start": 5.0, "end": 6.0}]
    result = split_intervals_by_speed(keep, [])
    assert result == keep
    # Must be copies, not the same objects (so callers can mutate safely).
    assert result[0] is not keep[0]


def test_speed_range_covering_whole_interval_sets_factor():
    keep = [{"start": 0.0, "end": 4.0}]
    speed = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    result = split_intervals_by_speed(keep, speed)
    assert result == [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}]


def test_speed_range_covers_only_part_of_interval_splits_it():
    """The key new behavior: a partial speed range splits the interval."""
    keep = [{"start": 0.0, "end": 10.0}]
    speed = [{"start": 4.0, "end": 6.0, "factor": 2.0}]
    result = split_intervals_by_speed(keep, speed)
    assert result == [
        {"start": 0.0, "end": 4.0},
        {"start": 4.0, "end": 6.0, "speed_factor": 2.0},
        {"start": 6.0, "end": 10.0},
    ]


def test_speed_range_at_start_of_interval():
    keep = [{"start": 0.0, "end": 10.0}]
    speed = [{"start": 0.0, "end": 4.0, "factor": 3.0}]
    result = split_intervals_by_speed(keep, speed)
    assert result == [
        {"start": 0.0, "end": 4.0, "speed_factor": 3.0},
        {"start": 4.0, "end": 10.0},
    ]


def test_speed_range_spanning_multiple_keep_intervals():
    """A single speed range overlapping two keep intervals (with a cut between)
    applies its factor to the covered sub-range of each."""
    keep = [{"start": 0.0, "end": 4.0}, {"start": 6.0, "end": 10.0}]
    speed = [{"start": 2.0, "end": 8.0, "factor": 2.0}]
    result = split_intervals_by_speed(keep, speed)
    assert result == [
        {"start": 0.0, "end": 2.0},
        {"start": 2.0, "end": 4.0, "speed_factor": 2.0},
        {"start": 6.0, "end": 8.0, "speed_factor": 2.0},
        {"start": 8.0, "end": 10.0},
    ]


def test_two_speed_ranges_in_one_interval():
    keep = [{"start": 0.0, "end": 10.0}]
    speed = [
        {"start": 1.0, "end": 3.0, "factor": 2.0},
        {"start": 5.0, "end": 7.0, "factor": 0.5},
    ]
    result = split_intervals_by_speed(keep, speed)
    assert result == [
        {"start": 0.0, "end": 1.0},
        {"start": 1.0, "end": 3.0, "speed_factor": 2.0},
        {"start": 3.0, "end": 5.0},
        {"start": 5.0, "end": 7.0, "speed_factor": 0.5},
        {"start": 7.0, "end": 10.0},
    ]


def test_speed_range_outside_keep_intervals_ignored():
    """A speed range that falls on cut content (no overlapping keep interval)
    produces no segment."""
    keep = [{"start": 0.0, "end": 4.0}]
    speed = [{"start": 10.0, "end": 12.0, "factor": 2.0}]
    result = split_intervals_by_speed(keep, speed)
    assert result == [{"start": 0.0, "end": 4.0}]
