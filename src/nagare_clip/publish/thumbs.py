"""Which source moments get a still, so a human picks from a shortlist.

The director already decided where the payoffs are — it wrote an ``overlay``
caption at each one, a ``keep`` around a silent event worth watching, and a
``timelapse`` over a stretch of work — so the shortlist is read off its ops
rather than guessed from the timeline.

Pure selection only.  Frame extraction runs through the whisperx Docker image
in the pipeline adapter (same batching as ``gap_context``), which hands the
results back as :class:`ThumbShot`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only: importing the director here would drag the
    # LLM transport into the render stage, which must never load it.
    from nagare_clip.director.director_llm import DirectorOp

# Kinds, most interesting first: an overlay carries the director's own words
# for the moment, a timelapse boundary shows the before/after of the work, and
# a keep is a silent event whose payoff is somewhere in the middle.
KIND_PRIORITY = ("overlay", "timelapse-start", "timelapse-end", "keep")


@dataclass(frozen=True)
class ThumbCandidate:
    stem: str
    time: float  # source seconds
    kind: str
    label: str  # the director's caption or note, shown to the human


@dataclass(frozen=True)
class ThumbShot:
    """A candidate whose frame was actually written to disk."""

    stem: str
    time: float
    kind: str
    label: str
    path: str  # relative to the publish stage dir


def frame_relpath(stem: str, t: float) -> str:
    """Frame path relative to the publish stage dir."""
    return f"frames/{stem}/{t:.3f}.jpg"


def _span(
    seg_times: Sequence[tuple[float | None, float | None]], line: int
) -> tuple[float, float] | None:
    if not (1 <= line <= len(seg_times)):
        return None
    start, end = seg_times[line - 1]
    if start is None or end is None:
        return None
    return float(start), float(end)


def _label(op: DirectorOp) -> str:
    text = (op.text or "").strip()
    return text or (op.note or "").strip()


def select_candidates(
    stem: str,
    ops: Sequence[DirectorOp],
    seg_times: Sequence[tuple[float | None, float | None]],
) -> list[ThumbCandidate]:
    """The payoff moments of one video, in time order.

    An overlay is sampled mid-way through the line it is anchored to (the
    caption is on screen across that line, and a line's first frame is often
    still the previous shot); a keep mid-way through the whole event it
    rescued; a timelapse at both ends of its range, which is where the
    before/after of the work shows.

    Moments that coincide are collapsed — they describe one frame, and the
    extracted file would collide anyway — keeping the highest-priority kind,
    whose label is the more useful one to show.
    """
    found: list[ThumbCandidate] = []
    for op in ops:
        first = _span(seg_times, op.lines[0])
        last = _span(seg_times, op.lines[1])
        if first is None or last is None:
            continue
        if op.type == "overlay":
            found.append(ThumbCandidate(stem, _mid(first), "overlay", _label(op)))
        elif op.type == "keep":
            found.append(ThumbCandidate(stem, (first[0] + last[1]) / 2.0, "keep", _label(op)))
        elif op.type == "timelapse":
            found.append(ThumbCandidate(stem, first[0], "timelapse-start", _label(op)))
            found.append(ThumbCandidate(stem, last[1], "timelapse-end", _label(op)))

    by_time: dict[str, ThumbCandidate] = {}
    for cand in sorted(found, key=lambda c: _priority(c.kind)):
        key = f"{cand.time:.3f}"  # the key the extracted frame is filed under
        by_time.setdefault(key, cand)
    return sorted(by_time.values(), key=lambda c: c.time)


def cap_candidates(candidates: Sequence[ThumbCandidate], limit: int) -> list[ThumbCandidate]:
    """At most *limit* candidates (``0`` = no limit), keeping the best kinds.

    Every still costs an ffmpeg seek and a place on a list a human has to look
    through, so a long video's keeps give way to its overlays.  Survivors go
    back into ``(stem, time)`` order — the shortlist reads as a walk through
    the video.
    """
    if limit <= 0 or len(candidates) <= limit:
        return list(candidates)
    ranked = sorted(candidates, key=lambda c: (_priority(c.kind), c.stem, c.time))[:limit]
    return sorted(ranked, key=lambda c: (c.stem, c.time))


def _mid(span: tuple[float, float]) -> float:
    return (span[0] + span[1]) / 2.0


def _priority(kind: str) -> int:
    return KIND_PRIORITY.index(kind) if kind in KIND_PRIORITY else len(KIND_PRIORITY)
