"""Blender-side integration test for place_strips().

Run inside Blender:
  blender --background --python tests/stage3/blender_place_strips.py -- <video> <output_json>

Produces a JSON file with strip data for the pytest wrapper to assert on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make src/ importable
_SRC = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(_SRC))

import bpy

from nagare_clip.stage3.scene import reset_scene
from nagare_clip.stage3.timeline import place_strips


def main() -> None:
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("Usage: blender --background --python <script> -- <video> <output_json>")
    user_args = argv[argv.index("--") + 1 :]
    video_path = user_args[0]
    output_json = user_args[1]

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

    # Three intervals to test with
    intervals = [
        {"start": 0.0, "end": 1.0},
        {"start": 2.0, "end": 3.0},
        {"start": 4.0, "end": 5.0},
    ]

    cursor = place_strips(intervals, video_path, seq_col, effective_fps)

    # Collect strip info
    strips = []
    for s in seq_col:
        strips.append({
            "name": s.name,
            "type": s.type,
            "channel": s.channel,
            "frame_start": s.frame_start,
            "frame_offset_start": s.frame_offset_start,
            "frame_offset_end": s.frame_offset_end,
            "frame_final_duration": s.frame_final_duration,
            "mute": s.mute,
        })

    result = {
        "cursor": cursor,
        "effective_fps": effective_fps,
        "strip_count": len(strips),
        "strips": strips,
    }

    Path(output_json).write_text(json.dumps(result, indent=2))


main()
