"""Pure helpers: which silent gaps get snapshots, and at which timestamps.

No I/O, no Docker — the pipeline adapter runs ffmpeg with what these return.
"""

from __future__ import annotations

# Keep frames off the exact boundary: the previous/next word may still be
# on screen (and WhisperX often stretches a word across the pause edge).
_INSET = 0.2


def select_gaps(
    ranges: list[tuple[float, float]], min_gap: float
) -> list[tuple[float, float]]:
    """Silent spans at least *min_gap* seconds long, sorted by start."""
    return sorted((s, e) for s, e in ranges if e - s >= min_gap)


def frame_times(start: float, end: float) -> list[float]:
    """Up to 3 timestamps inside the span: start+inset, midpoint, end-inset.

    Rounded to milliseconds and de-duplicated (a very short span collapses to
    a single midpoint frame), so the caller always gets at least one time.
    """
    mid = round((start + end) / 2, 3)
    start_inset = round(start + _INSET, 3)
    end_inset = round(end - _INSET, 3)
    # If inset frames would be inverted, span is too short for insets; use mid only
    if start_inset > end_inset:
        return [mid]
    candidates = [start_inset, mid, end_inset]
    out: list[float] = []
    for t in candidates:
        if start <= t <= end and t not in out:
            out.append(t)
    return sorted(out) if out else [mid]


def frame_relpath(stem: str, t: float) -> str:
    """Frame path relative to the gap_context stage dir."""
    return f"frames/{stem}/{t:.3f}.jpg"
