"""publish/timeline: source seconds -> finished-timeline seconds.

The mapping mirrors the blender stage's placement (keep intervals concatenated
in source order, each speed range compressing its span), which is the only
place the finished-timeline position of a source moment exists.
"""

from __future__ import annotations

import subprocess
import sys

from nagare_clip.publish.timeline import (
    build_placements,
    first_surviving_time,
    total_duration,
)


def test_import_does_not_require_bpy():
    """The publish stage runs in the pipeline process, where bpy does not exist.

    It reuses the blender stage's speed-splitting helper, so that helper must
    live in a bpy-free module -- guarded here in a fresh interpreter because
    other tests stub ``sys.modules["bpy"]`` and would mask a real bpy import.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, nagare_clip.publish.timeline; assert 'bpy' not in sys.modules",
        ],
        check=True,
    )


def test_single_source_concatenates_keeps():
    data = {"keep_intervals": [{"start": 2.0, "end": 5.0}, {"start": 9.0, "end": 10.0}]}
    placements = build_placements([("a", data)])
    assert [(p.tl_start, p.tl_end) for p in placements] == [(0.0, 3.0), (3.0, 4.0)]
    assert [p.src_start for p in placements] == [2.0, 9.0]
    assert total_duration(placements) == 4.0


def test_second_source_starts_where_the_first_ended():
    a = {"keep_intervals": [{"start": 0.0, "end": 4.0}]}
    b = {"keep_intervals": [{"start": 100.0, "end": 106.0}]}
    placements = build_placements([("a", a), ("b", b)])
    assert [(p.stem, p.tl_start, p.tl_end) for p in placements] == [
        ("a", 0.0, 4.0),
        ("b", 4.0, 10.0),
    ]


def test_speed_range_compresses_the_timeline():
    data = {
        "keep_intervals": [{"start": 0.0, "end": 40.0}],
        "speed_ranges": [{"start": 0.0, "end": 40.0, "factor": 8.0}],
    }
    placements = build_placements([("a", data)])
    assert len(placements) == 1
    assert placements[0].speed == 8.0
    assert placements[0].tl_end == 5.0


def test_partial_speed_range_splits_the_interval():
    data = {
        "keep_intervals": [{"start": 0.0, "end": 20.0}],
        "speed_ranges": [{"start": 10.0, "end": 20.0, "factor": 4.0}],
    }
    placements = build_placements([("a", data)])
    assert [(p.src_start, p.src_end, p.speed) for p in placements] == [
        (0.0, 10.0, 1.0),
        (10.0, 20.0, 4.0),
    ]
    # 10s at 1x then 10s at 4x -> 12.5s of finished video
    assert total_duration(placements) == 12.5


def test_zero_length_intervals_are_skipped():
    data = {"keep_intervals": [{"start": 3.0, "end": 3.0}, {"start": 4.0, "end": 5.0}]}
    placements = build_placements([("a", data)])
    assert [(p.tl_start, p.tl_end) for p in placements] == [(0.0, 1.0)]


def test_total_duration_of_nothing_is_zero():
    assert total_duration([]) == 0.0


def test_first_surviving_time_maps_a_kept_moment():
    data = {"keep_intervals": [{"start": 10.0, "end": 20.0}]}
    placements = build_placements([("a", data)])
    assert first_surviving_time(placements, "a", 12.0, 18.0) == 2.0


def test_first_surviving_time_snaps_a_cut_part_opening_forward():
    """A part's first seconds are routinely cut; the chapter belongs at the
    first moment of that part that actually survives."""
    data = {"keep_intervals": [{"start": 0.0, "end": 5.0}, {"start": 12.0, "end": 20.0}]}
    placements = build_placements([("a", data)])
    # part spans 8-20s; 8-12 was cut, so the chapter lands where 12.0 lands
    assert first_surviving_time(placements, "a", 8.0, 20.0) == 5.0


def test_first_surviving_time_of_a_fully_cut_part_is_none():
    data = {"keep_intervals": [{"start": 0.0, "end": 5.0}, {"start": 30.0, "end": 40.0}]}
    placements = build_placements([("a", data)])
    assert first_surviving_time(placements, "a", 10.0, 20.0) is None


def test_first_surviving_time_ignores_other_sources():
    a = {"keep_intervals": [{"start": 0.0, "end": 10.0}]}
    b = {"keep_intervals": [{"start": 0.0, "end": 10.0}]}
    placements = build_placements([("a", a), ("b", b)])
    assert first_surviving_time(placements, "b", 1.0, 2.0) == 11.0


def test_first_surviving_time_inside_a_timelapse_is_compressed():
    data = {
        "keep_intervals": [{"start": 0.0, "end": 120.0}],
        "speed_ranges": [{"start": 0.0, "end": 120.0, "factor": 8.0}],
    }
    placements = build_placements([("a", data)])
    assert first_surviving_time(placements, "a", 80.0, 100.0) == 10.0


def test_first_surviving_time_without_times_is_none():
    placements = build_placements([("a", {"keep_intervals": [{"start": 0.0, "end": 5.0}]})])
    assert first_surviving_time(placements, "a", None, 3.0) is None
    assert first_surviving_time(placements, "a", 1.0, None) is None
