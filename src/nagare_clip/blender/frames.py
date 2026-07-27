"""Pure frame-range helpers for the VSE layout (no bpy import — host-testable)."""

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
