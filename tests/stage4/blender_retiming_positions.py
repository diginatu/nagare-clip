"""Blender-side regression test for retiming strip positions.

Run inside Blender:
  blender --background --python tests/stage4/blender_retiming_positions.py -- <video> <output_json>

Reproduces the Blender 5.1 retiming corruption that requires several
retimed strips at large source-frame offsets to manifest (the shared
retiming_keys C-pointer bug). Emits each MOVIE strip's actual visible
start (content_start + left_handle_offset) alongside the timeline position
build_timeline_map() expects, so the pytest wrapper can assert they match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(_SRC))

import bpy

from nagare_clip.stage4.scene import reset_scene
from nagare_clip.stage4.timeline import build_timeline_map, place_strips


def _scenario() -> list[dict]:
    """Large source-time clips with four big speed spans interspersed.

    The large offsets (hundreds of seconds * 60fps) are what trigger the
    retiming position corruption; small/short scenarios do not.
    """
    intervals: list[dict] = []
    specials = {
        3: (284.695, 421.397, 2.0),
        8: (427.087, 518.029, 1.8),
        16: (716.111, 815.704, 2.5),
        22: (855.728, 1122.897, 2.5),
    }
    t = 250.0
    i = 0
    while t < 1140:
        if i in specials:
            s, e, f = specials[i]
            intervals.append({"start": s, "end": e, "speed_factor": f})
            t = e + 1.0
        else:
            intervals.append({"start": round(t, 3), "end": round(t + 1.5, 3)})
            t += 3.0
        i += 1
    return intervals


def main() -> None:
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("Usage: blender --background --python <script> -- <video> <output_json>")
    user_args = argv[argv.index("--") + 1:]
    video_path = user_args[0]
    output_json = user_args[1]

    scene = reset_scene()

    clip = bpy.data.movieclips.load(video_path)
    fps = float(clip.fps) if clip.fps and clip.fps > 0 else 60.0
    bpy.data.movieclips.remove(clip)

    fps_int = max(1, int(round(fps)))
    # Use a fractional fps_base like real footage (60 / 59.9).
    scene.render.fps = fps_int
    scene.render.fps_base = fps_int / (fps_int - 0.1)

    sequence_editor = scene.sequence_editor
    seq_col = getattr(sequence_editor, "sequences", None)
    if seq_col is None:
        seq_col = sequence_editor.strips
    effective_fps = scene.render.fps / scene.render.fps_base

    intervals = _scenario()
    tl_map = build_timeline_map(intervals, effective_fps, fps, start_cursor=1)
    place_strips(intervals, video_path, seq_col, effective_fps)

    actual = {
        s.name: int(s.content_start + s.left_handle_offset)
        for s in seq_col
        if s.type == "MOVIE"
    }
    strips = []
    for i, entry in enumerate(tl_map, 1):
        name = f"keep_{i:04d}"
        strips.append(
            {
                "name": name,
                "expected_start": int(entry["tl_start"]),
                "actual_start": actual.get(name),
                "speed_factor": entry.get("speed_factor", 1.0),
            }
        )

    Path(output_json).write_text(
        json.dumps({"strip_count": len(actual), "strips": strips}, indent=2)
    )


main()
