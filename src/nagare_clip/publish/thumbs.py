"""Thumbnail frame candidates: the moments the director marked as payoffs.

A thumbnail wants the frame the video is *about*, and the director already
said where those are: an ``overlay`` marks a turning point, a ``keep`` rescues
something worth watching in silence, and a ``timelapse`` brackets a stretch of
work whose start and end states are the before/after shot. This module turns
those ops into a shortlist of source timestamps (pure); the pipeline adapter
extracts the frames with ffmpeg in the whisperx image, exactly as gap_context
does.

Every candidate is snapped onto surviving footage — an op whose span was cut
(guided_edit drops blocked ops, and silence detection cuts around them) would
otherwise point at a frame that is not in the finished video at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.publish.timeline import Placed, first_surviving

# Two candidates landing within this many seconds of each other are the same
# shot for thumbnail purposes; the later one is dropped.
MIN_SPACING = 2.0

# A span's end time is exclusive on the timeline (a keep interval ending at
# 40.0s does not place a frame AT 40.0s), and the frame on the boundary shows
# whatever comes next anyway. The same reason gap_context insets its frames.
_END_INSET = 0.2


@dataclass(frozen=True)
class ThumbCandidate:
    stem: str
    source_time: float  # where to seek in the SOURCE file
    timeline_time: float  # where it lands in the finished cut (for review)
    reason: str  # which director op put it on the shortlist

    @property
    def relpath(self) -> str:
        """Frame path relative to the publish stage dir."""
        return f"frames/{self.stem}/{self.source_time:.3f}.jpg"


def _line_span(
    op: DirectorOp, seg_times: list[tuple[float | None, float | None]]
) -> tuple[float, float] | None:
    """Source ``(start, end)`` of an op's 1-based inclusive line range."""
    a, b = op.lines
    if not (1 <= a <= b <= len(seg_times)):
        return None
    start = seg_times[a - 1][0]
    end = seg_times[b - 1][1]
    if start is None or end is None or end < start:
        return None
    return start, end


def _op_moments(
    op: DirectorOp, seg_times: list[tuple[float | None, float | None]]
) -> list[tuple[float, str]]:
    """Candidate source times for one op, with the reason each was picked."""
    span = _line_span(op, seg_times)
    if span is None:
        return []
    start, end = span
    if op.type == "overlay":
        label = f"overlay: {op.text}" if op.text else "overlay"
        return [(start, label)]
    if op.type == "keep":
        # The middle of a rescued silence is where the event is playing out;
        # its edges are the speech on either side of it.
        return [((start + end) / 2.0, f"keep: {op.note}" if op.note else "keep")]
    if op.type == "timelapse":
        # Before and after: the two frames a "what changed" thumbnail wants.
        return [(start, "timelapse start"), (max(start, end - _END_INSET), "timelapse end")]
    return []


def _rank(reason: str) -> int:
    """How much a moment says about itself (lower wins a collision).

    An overlay names the payoff outright, a keep says something happens here,
    a timelapse boundary is only a before/after state.
    """
    return 0 if reason.startswith("overlay") else 1 if reason.startswith("keep") else 2


def select_candidates(
    ops: list[DirectorOp],
    seg_times: list[tuple[float | None, float | None]],
    placed: list[Placed],
    stem: str,
    *,
    min_spacing: float = MIN_SPACING,
) -> list[ThumbCandidate]:
    """Shortlist for one source, in finished-timeline order.

    A moment on cut footage moves forward to the first surviving frame of its
    own op span (the same rule the intervals stage uses for an overlay anchor);
    an op with no surviving footage at all drops out.
    """
    found: list[ThumbCandidate] = []
    for op in ops:
        span = _line_span(op, seg_times)
        for moment, reason in _op_moments(op, seg_times):
            limit = span[1] if span else None
            hit = first_surviving(placed, stem, moment, limit)
            if hit is None:
                continue
            found.append(
                ThumbCandidate(stem=stem, source_time=hit[0], timeline_time=hit[1], reason=reason)
            )
    found.sort(key=lambda c: c.timeline_time)
    out: list[ThumbCandidate] = []
    for c in found:
        if out and c.timeline_time - out[-1].timeline_time < min_spacing:
            # Same shot: keep whichever of the two says more about why it is a
            # payoff. Inside a timelapse a 0.2s inset is 0.025s of finished
            # video, so an overlay and a timelapse boundary really do collide —
            # and the caption is the reason the human would pick the frame.
            if _rank(c.reason) < _rank(out[-1].reason):
                out[-1] = c
            continue
        out.append(c)
    return out


def limit_candidates(candidates: list[ThumbCandidate], max_frames: int) -> list[ThumbCandidate]:
    """Thin an over-long shortlist to *max_frames*, evenly across the video.

    Truncating instead would hand the human every payoff from the first source
    and none from the last. ``max_frames <= 0`` means no limit.
    """
    if max_frames <= 0 or len(candidates) <= max_frames:
        return list(candidates)
    step = len(candidates) / max_frames
    return [candidates[int(i * step)] for i in range(max_frames)]
