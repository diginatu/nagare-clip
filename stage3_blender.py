#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path

import bpy


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


def sec_to_frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def reset_scene() -> bpy.types.Scene:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.sequence_editor_create()
    return scene


def load_source_metadata(source_path: Path) -> tuple[float, int, int]:
    clip = bpy.data.movieclips.load(str(source_path))
    fps = float(clip.fps) if clip.fps and clip.fps > 0 else 30.0
    width, height = clip.size
    bpy.data.movieclips.remove(clip)
    return fps, int(width), int(height)


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

    timeline_cursor = 1
    sequence_editor = scene.sequence_editor
    sequence_collection = getattr(sequence_editor, "sequences", None)
    if sequence_collection is None:
        sequence_collection = sequence_editor.strips
    effective_fps = scene.render.fps / scene.render.fps_base

    for idx, interval in enumerate(keep_intervals, start=1):
        start_sec = float(interval["start"])
        end_sec = float(interval["end"])
        if end_sec <= start_sec:
            continue

        src_start_frame = max(0, sec_to_frames(start_sec, effective_fps))
        src_end_frame = max(src_start_frame + 1, sec_to_frames(end_sec, effective_fps))

        strip = sequence_collection.new_movie(
            name=f"keep_{idx:04d}",
            filepath=str(source_path),
            channel=1,
            frame_start=timeline_cursor,
        )

        full_duration = max(1, int(strip.frame_duration))
        bounded_start = min(src_start_frame, full_duration - 1)
        bounded_end = min(max(src_end_frame, bounded_start + 1), full_duration)
        keep_frame_count = bounded_end - bounded_start
        frame_offset_start = bounded_start
        frame_offset_end = full_duration - bounded_end

        strip.frame_offset_start = frame_offset_start
        strip.frame_offset_end = frame_offset_end

        try:
            strip.frame_final_start = timeline_cursor
            strip.frame_final_end = timeline_cursor + keep_frame_count
        except AttributeError:
            pass

        timeline_cursor += keep_frame_count

    scene.frame_end = max(scene.frame_start, timeline_cursor - 1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path))


if __name__ == "__main__":
    main()
