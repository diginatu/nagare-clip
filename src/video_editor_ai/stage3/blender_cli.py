"""Stage 3 CLI entry point: build rough-cut VSE layout in Blender."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Blender runs this file in its own Python environment where the project
# package is not installed.  Insert the src/ directory so that
# ``video_editor_ai`` is importable regardless.
_SRC = Path(__file__).resolve().parent.parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bpy

from video_editor_ai.stage3.scene import load_source_metadata, reset_scene
from video_editor_ai.stage3.timeline import (
    build_timeline_map,
    place_captions,
    place_strips,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def parse_blender_args(argv: list[str]) -> argparse.Namespace:
    if "--" not in argv:
        raise ValueError("Expected '--' before script arguments.")

    user_args = argv[argv.index("--") + 1 :]
    parser = argparse.ArgumentParser(
        description="Build rough-cut VSE layout from keep intervals."
    )
    parser.add_argument("--source", required=True, help="Source video file path")
    parser.add_argument("--intervals", required=True, help="Intervals JSON path")
    parser.add_argument("--output", required=True, help="Output .blend path")
    return parser.parse_args(user_args)


def main() -> None:
    args = parse_blender_args(sys.argv)

    source_path = Path(args.source).expanduser().resolve()
    intervals_path = Path(args.intervals).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    with intervals_path.open("r", encoding="utf-8") as f:
        intervals_data = json.load(f)

    keep_intervals = intervals_data.get("keep_intervals", [])
    scene = reset_scene()

    source_fps, source_width, source_height = load_source_metadata(source_path)
    fps_int = max(1, int(round(source_fps)))
    fps_base = fps_int / source_fps

    scene.render.fps = fps_int
    scene.render.fps_base = fps_base
    scene.render.resolution_x = source_width
    scene.render.resolution_y = source_height
    scene.frame_start = 1

    sequence_editor = scene.sequence_editor
    sequence_collection = getattr(sequence_editor, "sequences", None)
    if sequence_collection is None:
        sequence_collection = sequence_editor.strips
    effective_fps = scene.render.fps / scene.render.fps_base

    timeline_cursor = place_strips(
        keep_intervals,
        str(source_path),
        sequence_collection,
        effective_fps,
    )

    for s in sequence_collection:
        s.select = False

    min_strip_frame = min((s.frame_start for s in sequence_collection), default=1)
    scene.frame_start = int(min(1, min_strip_frame))
    scene.frame_end = max(scene.frame_start, timeline_cursor - 1)

    captions = intervals_data.get("captions", [])
    if captions:
        tl_map = build_timeline_map(keep_intervals, effective_fps, source_fps)
        place_captions(captions, tl_map, effective_fps, sequence_collection)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    logging.info(
        "Done: %d strip(s) placed, scene ends at frame %d",
        len(keep_intervals),
        scene.frame_end,
    )
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path))


if __name__ == "__main__":
    main()
