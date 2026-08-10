"""Blender-side integration test for place_strips().

Run inside Blender:
  blender --background --python tests/blender/blender_place_strips.py -- <video> <output_json>

Produces a JSON file with strip data for the pytest wrapper to assert on.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Make src/ importable
_SRC = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(_SRC))

import bpy

from nagare_clip.blender import timeline as timeline_mod
from nagare_clip.blender.scene import reset_scene
from nagare_clip.blender.timeline import place_strips


class _WarningCollector(logging.Handler):
    """Collect WARNING-level messages so the pytest wrapper can assert on them."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def run_case(video_path: str, intervals: list[dict]) -> dict:
    """Place *intervals* in a fresh scene and report the resulting strips."""
    scene = reset_scene()

    # Set up scene from Video_Editing template (provides SEQUENCE_EDITOR area)
    clip = bpy.data.movieclips.load(video_path)
    fps = float(clip.fps) if clip.fps and clip.fps > 0 else 30.0
    bpy.data.movieclips.remove(clip)

    fps_int = max(1, int(round(fps)))
    scene.render.fps = fps_int
    scene.render.fps_base = fps_int / fps

    sequence_editor = scene.sequence_editor
    seq_col = getattr(sequence_editor, "sequences", None)
    if seq_col is None:
        seq_col = sequence_editor.strips
    effective_fps = scene.render.fps / scene.render.fps_base

    collector = _WarningCollector()
    root_logger = logging.getLogger()
    root_logger.addHandler(collector)
    try:
        cursor = place_strips(intervals, video_path, seq_col, effective_fps)
    finally:
        root_logger.removeHandler(collector)

    # Collect strip info
    strips = []
    for s in seq_col:
        strip_info = {
            "name": s.name,
            "type": s.type,
            "channel": s.channel,
            "frame_start": s.content_start,
            "frame_offset_start": s.left_handle_offset,
            "frame_offset_end": s.right_handle_offset,
            "frame_final_duration": s.duration,
            "mute": s.mute,
            # Names of the strips this one is connected to (video<->its audio).
            "connections": sorted(c.name for c in s.connections),
        }
        if s.type == "SPEED":
            strip_info["speed_factor"] = getattr(s, "speed_factor", None)
            strip_info["use_default_fade"] = getattr(s, "use_default_fade", None)
        if s.type == "SOUND":
            strip_info["pitch_correction"] = getattr(s, "pitch_correction", None)
        strips.append(strip_info)

    return {
        "cursor": cursor,
        "effective_fps": effective_fps,
        "strip_count": len(strips),
        "strips": strips,
        "warnings": collector.messages,
    }


def main() -> None:
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("Usage: blender --background --python <script> -- <video> <output_json>")
    user_args = argv[argv.index("--") + 1 :]
    video_path = user_args[0]
    output_json = user_args[1]

    # Four intervals to test with; the 4th has speed_factor=2.0.
    result = run_case(
        video_path,
        [
            {"start": 0.0, "end": 1.0},
            {"start": 2.0, "end": 3.0},
            {"start": 4.0, "end": 5.0},
            {"start": 6.0, "end": 7.0, "speed_factor": 2.0},
        ],
    )

    # A count that is not a power of two: strip allocation must produce exactly
    # one pair per interval, with no spare strips left behind.
    result["odd"] = run_case(
        video_path,
        [{"start": float(i), "end": i + 0.5} for i in range(5)],
    )

    # A zero-length interval places nothing but still consumes its index.
    result["degenerate"] = run_case(
        video_path,
        [
            {"start": 0.0, "end": 1.0},
            {"start": 5.0, "end": 5.0},
            {"start": 6.0, "end": 7.0},
        ],
    )

    # No intervals at all: no strips, and the templates are still cleaned up.
    result["empty"] = run_case(video_path, [])

    # A retimed length that lands exactly on .5 frames: 4.4s * 30fps = 132
    # source frames, 132 / 8 = 16.5.  Blender rounds that half away from zero
    # (17); Python's round() would say 16 and under-advance the cursor, leaving
    # the next strip overlapping and shunted off channel 1.
    result["half_round"] = run_case(
        video_path,
        [
            {"start": 0.0, "end": 4.4, "speed_factor": 8.0},
            {"start": 5.0, "end": 6.0},
        ],
    )

    # Simulate any *other* cause of a cursor that under-advances (a Blender
    # duration this code cannot predict) by under-reporting every strip's
    # length by one frame.  Every strip after the first then overlaps its
    # predecessor, and the placement must say so instead of running clean.
    original_strip_duration = timeline_mod._strip_duration
    timeline_mod._strip_duration = lambda strip: max(1, original_strip_duration(strip) - 1)
    try:
        result["displaced"] = run_case(
            video_path,
            [{"start": float(i), "end": i + 1.0} for i in range(3)],
        )
    finally:
        timeline_mod._strip_duration = original_strip_duration

    Path(output_json).write_text(json.dumps(result, indent=2))


main()
