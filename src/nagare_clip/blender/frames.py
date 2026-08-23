"""Pure placement helpers for the VSE layout (no bpy import — host-testable).

``split_intervals_by_speed`` lives here rather than beside the strip-placement
code that uses it because the ``publish`` stage needs the same speed-aware
arithmetic to map a source moment onto the finished timeline, and it runs in
the pipeline process, where ``bpy`` does not exist.  ``blender.timeline``
re-exports it, so the placement loop and the chapter timestamps can never
disagree about what a speed range does.

``slice_intervals_data``/``ordered_sources`` are here for the same reason: the
blender placement loop, ``publish.timeline`` and ``cut_report.metrics`` all have
to turn one ordered manifest plus each source's intervals JSON into the same
list of placeable segments, and two versions of that arithmetic would be two
versions of the finished video.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from nagare_clip.order import TimelineSegment


def retimed_frame_count(keep_frame_count: int, speed: float) -> int:
    """Frames a strip of *keep_frame_count* occupies once retimed to *speed*.

    Blender rounds a half frame **away from zero** (``round_fl_to_int``) while
    Python's ``round()`` rounds half to even, so ``132 / 8 = 16.5`` is 17
    frames in the scene and would be 16 here.  A prediction one frame short
    leaves the placement cursor inside the strip Blender actually built: the
    next strip overlaps its predecessor and Blender resolves that by moving it
    to a free channel — onto the channels reserved for the speed badge and the
    captions.  Shared by ``build_timeline_map`` and the placement loop so the
    timeline map and the strips cannot disagree about a length.
    """
    return max(1, math.floor(keep_frame_count / speed + 0.5))


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


#: The keys ``slice_intervals_data`` rebuilds; every other key is carried through.
_SLICED_KEYS = ("keep_intervals", "speed_ranges", "captions", "overlays")


def _clip_spans(spans: Sequence[Any], start: float, end: float) -> list[dict]:
    """Spans clipped to ``[start, end]``; a span outside it disappears.

    A boundary is introduced only where it falls strictly inside a span, so a
    window covering everything returns the input unchanged — which is what makes
    an identity order provably the pipeline's previous behaviour.  Keys other
    than ``start``/``end`` (``factor``, ``speed_factor``, anything hand-added)
    survive the split.
    """
    out: list[dict] = []
    for span in spans or []:
        a = max(float(span["start"]), start)
        b = min(float(span["end"]), end)
        if b <= a:
            continue
        clipped = dict(span)
        clipped["start"] = a
        clipped["end"] = b
        out.append(clipped)
    return out


def _anchored_in(items: Sequence[Any], start: float, end: float) -> list[dict]:
    """Items whose ``start`` falls in ``[start, end)``.

    Assignment is by anchor, not by overlap: a caption straddling a segment
    boundary would otherwise be placed in both segments, and ``place_captions``
    already clamps one against whatever timeline map it is given.
    """
    return [dict(item) for item in items or [] if start <= float(item["start"]) < end]


def slice_intervals_data(data: Mapping[str, Any], start: float, end: float) -> dict:
    """The part of one source's intervals JSON that plays in ``[start, end]``.

    Keep intervals and speed ranges are split at the boundary; captions and
    overlays go whole to the segment holding their start.  ``source_file`` and
    ``duration_sec`` are carried through unchanged — they describe the source,
    not the slice.  The input is never mutated.
    """
    out = {k: v for k, v in data.items() if k not in _SLICED_KEYS}
    out["keep_intervals"] = _clip_spans(data.get("keep_intervals", []), start, end)
    out["speed_ranges"] = _clip_spans(data.get("speed_ranges", []), start, end)
    out["captions"] = _anchored_in(data.get("captions", []), start, end)
    out["overlays"] = _anchored_in(data.get("overlays", []), start, end)
    return out


def placement_order(
    stems: Sequence[str], entries: Sequence[TimelineSegment]
) -> list[TimelineSegment]:
    """The manifest restricted to *stems*, or shooting order when there is none.

    No manifest means a project built before the order existed: each source
    plays whole, and the window is unbounded so a rounding tail at the end of a
    clip is never clipped off.  Restricting rather than reordering is what makes
    ``--source X`` build X's segments alone while each keeps its position.
    """
    if not entries:
        return [TimelineSegment(stem, 0.0, math.inf) for stem in stems]
    known = set(stems)
    return [entry for entry in entries if entry.stem in known]


def ordered_sources(
    entries: Sequence[TimelineSegment], data_by_stem: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, dict]]:
    """``(stem, sliced intervals data)`` per manifest entry, in playback order.

    This is the finished video, as the blender placement loop, the chapter
    timestamps and the cut report all have to see it.  An entry whose source has
    no readable intervals JSON is skipped, the way the report already skips one.
    """
    out: list[tuple[str, dict]] = []
    for entry in entries:
        data = data_by_stem.get(entry.stem)
        if data is None:
            continue
        out.append((entry.stem, slice_intervals_data(data, entry.start, entry.end)))
    return out
