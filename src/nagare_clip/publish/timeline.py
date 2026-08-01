"""Source time -> finished-timeline time, in seconds (pure, no bpy).

Chapters can only be produced here: a chapter timestamp is a *finished*
timeline position, and the mapping from source seconds to finished seconds
exists nowhere else in the pipeline except the blender stage's strip
placement — which runs inside Blender and writes only a ``.blend``.

This module mirrors ``blender_cli``'s placement arithmetic: intervals are
split at speed-range boundaries (``split_intervals_by_speed``, shared with
the blender stage), sources are concatenated in the order they were passed,
and each placed span occupies ``(src_end - src_start) / speed`` seconds of
output. The one deliberate difference is that blender rounds every span to
whole frames (``build_timeline_map``); here everything stays in seconds, so
a mapped position can differ from the rendered one by well under a frame per
placed span. Chapter timestamps are whole seconds and merged at a 10s floor,
so that drift is immaterial — and a seconds-based map keeps this stage free
of fps/bpy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nagare_clip.blender.frames import split_intervals_by_speed


@dataclass(frozen=True)
class Placed:
    """One kept span as it lands on the finished timeline."""

    stem: str
    src_start: float
    src_end: float
    tl_start: float
    tl_end: float
    speed: float


def build_edit_map(sources: list[tuple[str, dict[str, Any]]]) -> list[Placed]:
    """Placed spans for every source, concatenated in the given order.

    *sources* is ``(stem, intervals_json_data)`` in timeline order — the same
    order the blender stage receives its ``--source``/``--intervals`` pairs.
    """
    placed: list[Placed] = []
    cursor = 0.0
    for stem, data in sources:
        keep = data.get("keep_intervals") or []
        speeds = data.get("speed_ranges") or []
        for iv in split_intervals_by_speed(keep, speeds):
            src_start = float(iv["start"])
            src_end = float(iv["end"])
            if src_end <= src_start:
                continue
            speed = float(iv.get("speed_factor", 1.0)) or 1.0
            length = (src_end - src_start) / speed
            placed.append(
                Placed(
                    stem=stem,
                    src_start=src_start,
                    src_end=src_end,
                    tl_start=cursor,
                    tl_end=cursor + length,
                    speed=speed,
                )
            )
            cursor += length
    return placed


def total_duration(placed: list[Placed]) -> float:
    """Length of the finished timeline in seconds (0.0 when nothing survives)."""
    return placed[-1].tl_end if placed else 0.0


def to_timeline(placed: list[Placed], stem: str, t: float) -> float | None:
    """Finished-timeline seconds for source second *t*, or ``None`` if cut."""
    for p in placed:
        if p.stem == stem and p.src_start <= t <= p.src_end:
            return p.tl_start + (t - p.src_start) / p.speed
    return None


def first_surviving(
    placed: list[Placed], stem: str, start: float, end: float | None = None
) -> tuple[float, float] | None:
    """First surviving moment at or after *start* (optionally before *end*).

    Returns ``(source_time, timeline_time)``, or ``None`` when nothing between
    *start* and *end* survived the cut — which is exactly how a part that was
    cut in its entirety drops out of the chapter list. ``end=None`` searches to
    the end of the source.
    """
    for p in placed:
        if p.stem != stem or p.src_end <= start:
            continue
        if end is not None and p.src_start > end:
            break
        src = max(start, p.src_start)
        if end is not None and src > end:
            continue
        return src, p.tl_start + (src - p.src_start) / p.speed
    return None
