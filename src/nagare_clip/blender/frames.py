"""Pure placement helpers for the VSE layout (no bpy import — host-testable).

``split_intervals_by_speed`` lives here rather than beside the strip-placement
code that uses it because the ``publish`` stage needs the same speed-aware
arithmetic to map a source moment onto the finished timeline, and it runs in
the pipeline process, where ``bpy`` does not exist.  ``blender.timeline``
re-exports it, so the placement loop and the chapter timestamps can never
disagree about what a speed range does.
"""

from __future__ import annotations


def clamp_frames(src_start: int, src_end: int, full_duration: int) -> tuple[int, int, bool]:
    """Clamp a strip's source frame range to the clip length.

    Returns ``(bounded_start, bounded_end, negligible)``.  *negligible* is
    True when the start is unchanged and the end overshoots the clip by at
    most one frame — the sec->frame rounding artifact at a video's tail,
    which merits a debug line rather than a WARNING.
    """
    bounded_start = min(src_start, full_duration - 1)
    bounded_end = min(max(src_end, bounded_start + 1), full_duration)
    negligible = bounded_start == src_start and 0 <= src_end - bounded_end <= 1
    return bounded_start, bounded_end, negligible


def split_intervals_by_speed(keep_intervals: list, speed_ranges: list) -> list:
    """Split keep intervals at speed-range boundaries.

    ``speed_ranges`` is the top-level array from the intervals JSON, each item
    ``{"start", "end", "factor"}``. A speed range may cover an arbitrary
    sub-range of a keep interval (or span several), so each keep interval is
    cut at every speed boundary that falls strictly inside it. Each resulting
    sub-interval carries ``speed_factor`` equal to the factor of the speed
    range covering its midpoint (omitted when the factor is 1.0 / uncovered),
    matching the ``interval.get("speed_factor", 1.0)`` default used downstream.

    Returns a fresh list of dicts; input dicts are never mutated.
    """
    if not speed_ranges:
        return [dict(iv) for iv in keep_intervals]

    result: list = []
    for iv in keep_intervals:
        start = float(iv["start"])
        end = float(iv["end"])
        boundaries = {start, end}
        for sr in speed_ranges:
            for edge in (float(sr["start"]), float(sr["end"])):
                if start < edge < end:
                    boundaries.add(edge)
        points = sorted(boundaries)
        for a, b in zip(points, points[1:]):
            seg = dict(iv)
            seg.pop("speed_factor", None)
            seg["start"] = a
            seg["end"] = b
            mid = (a + b) / 2.0
            for sr in speed_ranges:
                if float(sr["start"]) <= mid < float(sr["end"]):
                    factor = float(sr["factor"])
                    if factor != 1.0:
                        seg["speed_factor"] = factor
                    break
            result.append(seg)
    return result
