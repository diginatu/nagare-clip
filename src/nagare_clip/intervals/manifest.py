"""Resolve the segment order from line numbers into source seconds.

The order is written in line numbers because that is the only coordinate the
plan, the summaries and the human conversation share; ``blender`` knows only
seconds.  ``intervals`` already reads the sentence_split JSON and produces
second-based output, so it is the **single conversion point** between the two:
the plan is the authority on the order up to and including this stage, and
``intervals/timeline.json`` is the authority after it.  Nothing downstream
re-derives a time from a line number.

A segment ``[a, b]`` runs from the moment line ``a-1`` ended to the moment line
``b`` ends, with the first segment of a source starting at ``0.0`` and the last
ending at the source's duration.  Ending the previous segment where its last
line ends gives each segment the silent gap that *precedes* its first line, so
``keep_pre_margin`` stays with the speech it belongs to and a moved segment
takes its own run-up with it.

A segment ending ``b~`` (:attr:`~nagare_clip.order.Segment.gap_end`) keeps the
silence after ``b`` instead: that boundary sits at
``max(end(b), start(b+1) - pre_margin)``, so the silence stays with the segment
it ends and line ``b+1`` still takes its own run-up with it.  Both segments of
one boundary read the same rule, so they cannot disagree.

Pure: no I/O, never raises.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from nagare_clip.order import Segment, TimelineSegment

logger = logging.getLogger(__name__)

SegTimes = Sequence[tuple[float | None, float | None]]


def build_manifest(
    segments: Sequence[Segment],
    times_by_stem: Mapping[str, SegTimes],
    durations_by_stem: Mapping[str, float],
    *,
    pre_margin: float = 0.0,
) -> list[TimelineSegment]:
    """The ordered manifest, or ``[]`` when the order cannot be resolved.

    A source with no known duration is skipped (its intervals JSON is missing,
    the way the cut report already tolerates).  An unresolvable *boundary*, by
    contrast, degrades the **whole** manifest: a source that cannot be split
    would otherwise collapse into a single entry and silently change the order,
    and the caller falls back to shooting order instead.

    *pre_margin* is ``intervals.keep_pre_margin``: how much of a ``b~``
    silence the line after it keeps as its run-up.
    """
    # Lines after which the silence belongs to the segment BEFORE the boundary.
    claimed = {
        (seg.stem, seg.lines[1]) for seg in segments if seg.lines is not None and seg.gap_end
    }
    out: list[TimelineSegment] = []
    for seg in segments:
        duration = durations_by_stem.get(seg.stem)
        if duration is None:
            logger.warning("order: no duration for %s; left out of the manifest", seg.stem)
            continue
        if seg.lines is None:
            out.append(TimelineSegment(seg.stem, 0.0, float(duration)))
            continue

        first, last = seg.lines
        times = times_by_stem.get(seg.stem) or []
        start = (
            0.0
            if first <= 1
            else _boundary(times, first - 1, seg.stem, (seg.stem, first - 1) in claimed, pre_margin)
        )
        end = (
            float(duration)
            if last >= len(times)
            else _boundary(times, last, seg.stem, seg.gap_end, pre_margin)
        )
        if start is None or end is None or end <= start:
            logger.warning(
                "order: cannot resolve %s lines %d-%d to seconds; falling back to shooting order",
                seg.stem,
                first,
                last,
            )
            return []
        out.append(TimelineSegment(seg.stem, start, end, lines=seg.lines, gap_end=seg.gap_end))
    return out


def _boundary(
    times: SegTimes, line: int, stem: str, gap_before: bool, pre_margin: float
) -> float | None:
    """Where the cut between *line* and the next one falls.

    *gap_before*: the silence after *line* stays with the segment ending on
    it, so the cut moves to the next line's run-up, never before *line* ends.
    """
    end = _line_end(times, line, stem)
    if end is None or not gap_before:
        return end
    nxt = times[line][0] if line < len(times) else None
    if nxt is None:
        return end
    return max(end, float(nxt) - max(pre_margin, 0.0))


def _line_end(times: SegTimes, line: int, stem: str) -> float | None:
    """When 1-based *line* ends, or ``None`` when that is not known."""
    if not 1 <= line <= len(times):
        logger.warning("order: %s has no line %d to take a boundary from", stem, line)
        return None
    end = times[line - 1][1]
    return None if end is None else float(end)
