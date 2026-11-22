"""The ``.blend``'s real length against the timeline the publish tools compute.

The publish tools never read the ``.blend``: every chapter and still time is
computed from the intervals and ``intervals/timeline.json``
(`publish.timeline`).  A user who opens the blender stage's ``.blend`` and
trims it by hand changes the real video without the tools knowing, and every
time after the trim is then late — once by up to ~56 s, unnoticed until the
user watched the upload candidate.  This module measures the ``.blend``'s scene
frame range (what Blender renders) so the tools can say so.

The comparison only warns, from `MISMATCH_SECONDS` up: the blender stage rounds
each interval to whole frames, so an untouched ``.blend`` differs from the
computed timeline by a fraction of a second (see `publish.timeline`).

Blender is a host binary here, as for the blender stage itself; the file is
zstd-compressed and its layout is Blender's own, so headless Blender is what
reads it (about half a second).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.pipeline.external import run_capture
from nagare_clip.publish.chapters import format_timestamp

MISMATCH_SECONDS = 1.0
MARKER = "NAGARE_BLEND_INFO "

_SCRIPT = (
    "import bpy, json; s = bpy.context.scene; r = s.render; "
    f"print({MARKER!r} + json.dumps({{'fps': r.fps, 'fps_base': r.fps_base, "
    "'frame_start': s.frame_start, 'frame_end': s.frame_end}))"
)


@dataclass(frozen=True)
class BlendRange:
    """The scene's frame rate and frame range (``frame_end`` included)."""

    fps: int
    fps_base: float
    frame_start: int
    frame_end: int

    @property
    def seconds(self) -> float:
        return (self.frame_end - self.frame_start + 1) * self.fps_base / self.fps


def blend_info_cmd(blend: Path) -> list[str]:
    return [
        "blender",
        "--background",
        "--factory-startup",
        str(blend),
        "--python-exit-code",
        "1",
        "--python-expr",
        _SCRIPT,
    ]


def parse_blend_info(stdout: str) -> BlendRange:
    """The marked line of `blend_info_cmd`'s output; `ValueError` if unusable."""
    for line in stdout.splitlines():
        if not line.startswith(MARKER):
            continue
        try:
            data = json.loads(line[len(MARKER) :])
            rng = BlendRange(
                int(data["fps"]),
                float(data["fps_base"]),
                int(data["frame_start"]),
                int(data["frame_end"]),
            )
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"unreadable scene info from Blender: {line!r}") from e
        if rng.fps <= 0 or rng.fps_base <= 0 or rng.frame_end < rng.frame_start:
            raise ValueError(f"unusable scene info from Blender: {line!r}")
        return rng
    raise ValueError("Blender printed no scene info")


def read_blend_range(blend: Path, run: Callable[[list[str]], str] = run_capture) -> BlendRange:
    """Open *blend* in headless Blender and read its scene's frame range."""
    return parse_blend_info(run(blend_info_cmd(blend)))


def _length(seconds: float) -> str:
    return f"{format_timestamp(seconds)} ({seconds:.1f} s)"


def mismatch_warning(computed: float, blend_seconds: float, blend: Path) -> str | None:
    """The warning when the ``.blend`` and the computed timeline differ by
    `MISMATCH_SECONDS` or more; ``None`` otherwise."""
    diff = blend_seconds - computed
    if abs(diff) < MISMATCH_SECONDS:
        return None
    way = "shorter" if diff < 0 else "longer"
    return "\n".join(
        [
            "the .blend's length differs from the timeline these times are computed from",
            f"  computed from the intervals: {_length(computed)}",
            f"  .blend ({blend}): {_length(blend_seconds)}",
            f"  the .blend is {_length(abs(diff))} {way}",
            "  The .blend was probably edited by hand in Blender after the blender stage "
            "wrote it. These tools cannot see that edit, so chapter times and the times of "
            "thumbnail stills can be off from the real video (more so later in the video).",
            "  Fix: carry the hand edit back into the pipeline (the director's edit) and "
            "re-run through the blender stage, or check every chapter's position in the "
            "rendered video before uploading.",
        ]
    )
