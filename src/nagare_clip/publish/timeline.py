"""Where a source moment ends up in the finished video.

Chapter timestamps need the source-time -> finished-timeline mapping, and that
mapping exists nowhere else in the pipeline: the intervals stage knows which
spans survive, the blender stage knows how ``speed_ranges`` compress them, and
only the two together say what "5:00 into the video" points at.

This module reproduces the blender stage's concatenation (sources in order,
keep intervals back to back, each split at its speed boundaries by the shared
``split_intervals_by_speed``) in **seconds** rather than frames.  The blender
placement rounds each interval to whole frames, so a timestamp here can differ
from the rendered timeline by a fraction of a second per interval — invisible
at the ``M:SS`` resolution a chapter list is written in, and worth not having
to know the source's frame rate to compute.

Pure: no I/O, no bpy (``split_intervals_by_speed`` lives in ``blender.frames``
for exactly that reason).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nagare_clip.blender.frames import split_intervals_by_speed


@dataclass(frozen=True)
class Placement:
    """One kept (and uniformly sped) span, and where it lands in the output."""

    stem: str
    src_start: float  # source seconds
    src_end: float
    tl_start: float  # finished-timeline seconds
    tl_end: float
    speed: float


def build_placements(sources: Sequence[tuple[str, dict[str, Any]]]) -> list[Placement]:
    """Lay every source's keep intervals end to end, in the given order.

    *sources* is ``(stem, intervals_json_data)`` in the same order the blender
    stage receives them, since that order is what the finished timeline is.
    """
    placements: list[Placement] = []
    cursor = 0.0
    for stem, data in sources:
        intervals = split_intervals_by_speed(
            data.get("keep_intervals", []) or [],
            data.get("speed_ranges", []) or [],
        )
        for iv in intervals:
            start = float(iv["start"])
            end = float(iv["end"])
            if end <= start:
                continue
            speed = float(iv.get("speed_factor", 1.0)) or 1.0
            length = (end - start) / speed
            placements.append(
                Placement(
                    stem=stem,
                    src_start=start,
                    src_end=end,
                    tl_start=cursor,
                    tl_end=cursor + length,
                    speed=speed,
                )
            )
            cursor += length
    return placements


def total_duration(placements: Sequence[Placement]) -> float:
    """Length of the finished video, in seconds."""
    return placements[-1].tl_end if placements else 0.0


def first_surviving_time(
    placements: Sequence[Placement],
    stem: str,
    start: float | None,
    end: float | None,
) -> float | None:
    """Finished-timeline seconds of the first surviving moment of a source span.

    A part's opening seconds are routinely cut (silence detection reaches them,
    keep margins do not reach back far enough), so the timestamp is taken at
    the earliest moment of ``[start, end]`` that actually survived rather than
    at the part's nominal start.  ``None`` when the whole span was cut — a part
    that is not in the finished video has no chapter.
    """
    if start is None or end is None:
        return None
    best: float | None = None
    for p in placements:
        if p.stem != stem or p.src_start >= end or p.src_end <= start:
            continue
        moment = max(start, p.src_start)
        tl = p.tl_start + (moment - p.src_start) / p.speed
        if best is None or tl < best:
            best = tl
    return best
