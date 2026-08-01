"""Source seconds -> finished-timeline seconds (the mapping only publish has)."""

from __future__ import annotations

from nagare_clip.publish.timeline import (
    build_edit_map,
    first_surviving,
    to_timeline,
    total_duration,
)


def _data(keeps, speeds=None):
    out = {"keep_intervals": [{"start": s, "end": e} for s, e in keeps]}
    if speeds:
        out["speed_ranges"] = [{"start": s, "end": e, "factor": f} for s, e, f in speeds]
    return out


def test_cuts_compress_the_timeline():
    placed = build_edit_map([("a", _data([(0.0, 10.0), (20.0, 30.0)]))])
    assert [(p.tl_start, p.tl_end) for p in placed] == [(0.0, 10.0), (10.0, 20.0)]
    # the 10s hole between the keeps is gone from the finished cut
    assert to_timeline(placed, "a", 25.0) == 15.0
    assert to_timeline(placed, "a", 15.0) is None  # cut footage has no position
    assert total_duration(placed) == 20.0


def test_sources_concatenate_in_order():
    placed = build_edit_map([("a", _data([(0.0, 5.0)])), ("b", _data([(100.0, 105.0)]))])
    assert to_timeline(placed, "b", 100.0) == 5.0
    assert total_duration(placed) == 10.0


def test_speed_range_compresses_its_span():
    # 0-10s at 4x lands in 2.5s of output; the rest plays at 1x after it.
    placed = build_edit_map([("a", _data([(0.0, 20.0)], [(0.0, 10.0, 4.0)]))])
    assert [(p.src_start, p.src_end, p.speed) for p in placed] == [
        (0.0, 10.0, 4.0),
        (10.0, 20.0, 1.0),
    ]
    assert to_timeline(placed, "a", 10.0) == 2.5
    assert to_timeline(placed, "a", 20.0) == 12.5
    assert total_duration(placed) == 12.5


def test_first_surviving_moves_forward_onto_kept_footage():
    placed = build_edit_map([("a", _data([(10.0, 20.0)]))])
    # a part starting in cut footage resolves to the first kept moment
    assert first_surviving(placed, "a", 5.0, 30.0) == (10.0, 0.0)
    # already inside a keep: unchanged
    assert first_surviving(placed, "a", 12.0, 30.0) == (12.0, 2.0)
    # nothing of the span survived
    assert first_surviving(placed, "a", 21.0, 25.0) is None
    assert first_surviving(placed, "b", 12.0, 30.0) is None


def test_zero_length_and_missing_intervals_are_ignored():
    placed = build_edit_map([("a", _data([(5.0, 5.0)])), ("b", {})])
    assert placed == []
    assert total_duration(placed) == 0.0
