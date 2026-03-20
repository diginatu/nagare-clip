"""Stage 3 CLI entry point: build rough-cut VSE layout in Blender."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Blender runs this file in its own Python environment where the project
# package is not installed.  Insert the src/ directory so that
# ``nagare_clip`` is importable regardless.
_SRC = Path(__file__).resolve().parent.parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bpy

from nagare_clip.config import get_effective_config
from nagare_clip.stage3.scene import load_source_metadata, reset_scene
from nagare_clip.stage3.timeline import (
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
    parser.add_argument(
        "--source", required=True, action="append", dest="sources",
        help="Source video file path (repeat for multiple sources)"
    )
    parser.add_argument(
        "--intervals", required=True, action="append", dest="intervals_paths",
        help="Intervals JSON path (repeat to match each --source)"
    )
    parser.add_argument("--output", required=True, help="Output .blend path")
    parser.add_argument(
        "--config", dest="config_path", default=None, help="Path to YAML config file"
    )
    return parser.parse_args(user_args)


def main() -> None:
    args = parse_blender_args(sys.argv)

    config_path = Path(args.config_path) if args.config_path else None
    cfg = get_effective_config(config_path)

    sources = [Path(s).expanduser().resolve() for s in args.sources]
    intervals_paths = [Path(p).expanduser().resolve() for p in args.intervals_paths]
    output_path = Path(args.output).expanduser().resolve()

    if len(sources) != len(intervals_paths):
        raise ValueError(
            f"Number of --source ({len(sources)}) and --intervals "
            f"({len(intervals_paths)}) arguments must match."
        )

    # Load all intervals data upfront
    all_intervals_data = []
    for ivp in intervals_paths:
        with ivp.open("r", encoding="utf-8") as f:
            all_intervals_data.append(json.load(f))

    scene = reset_scene()

    # Use first source for scene metadata
    first_fps, first_width, first_height = load_source_metadata(
        sources[0], default_fps=cfg["stage3"]["default_fps"]
    )
    fps_int = max(1, int(round(first_fps)))
    fps_base = fps_int / first_fps

    scene.render.fps = fps_int
    scene.render.fps_base = fps_base
    scene.render.resolution_x = first_width
    scene.render.resolution_y = first_height
    scene.frame_start = 1

    sequence_editor = scene.sequence_editor
    sequence_collection = getattr(sequence_editor, "sequences", None)
    if sequence_collection is None:
        sequence_collection = sequence_editor.strips
    effective_fps = scene.render.fps / scene.render.fps_base

    # Warn if subsequent sources differ in resolution/FPS
    for i, src in enumerate(sources[1:], start=1):
        fps_i, w_i, h_i = load_source_metadata(
            src, default_fps=cfg["stage3"]["default_fps"]
        )
        if abs(fps_i - first_fps) > 0.01 or w_i != first_width or h_i != first_height:
            logging.warning(
                "Source %d (%s) differs from first source: fps=%.3f vs %.3f, "
                "resolution=%dx%d vs %dx%d",
                i + 1, src.name, fps_i, first_fps, w_i, h_i, first_width, first_height,
            )

    # Loop over (source, intervals) pairs, accumulating timeline position
    timeline_cursor = 1
    idx_offset = 0
    all_tl_maps = []
    all_captions = []

    for src_num, (source_path, intervals_data) in enumerate(
        zip(sources, all_intervals_data), start=1
    ):
        logging.info(
            "Source %d/%d: %s", src_num, len(sources), source_path.name
        )
        keep_intervals = intervals_data.get("keep_intervals", [])
        captions = intervals_data.get("captions", [])

        tl_map = build_timeline_map(
            keep_intervals, effective_fps, first_fps, start_cursor=timeline_cursor
        )
        all_tl_maps.extend(tl_map)
        all_captions.extend(captions)

        timeline_cursor = place_strips(
            keep_intervals,
            str(source_path),
            sequence_collection,
            effective_fps,
            start_cursor=timeline_cursor,
            idx_offset=idx_offset,
            source_num=src_num,
        )
        idx_offset += len(keep_intervals)

    for s in sequence_collection:
        s.select = False

    min_strip_frame = min((s.frame_start for s in sequence_collection), default=1)
    scene.frame_start = int(min(1, min_strip_frame))
    scene.frame_end = max(scene.frame_start, timeline_cursor - 1)

    if all_captions:
        place_captions(
            all_captions,
            all_tl_maps,
            effective_fps,
            sequence_collection,
            caption_style=cfg["stage3"]["caption_style"],
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_strips = sum(
        len(d.get("keep_intervals", [])) for d in all_intervals_data
    )
    logging.info(
        "Done: %d strip(s) across %d source(s), scene ends at frame %d",
        total_strips,
        len(sources),
        scene.frame_end,
    )
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path))


if __name__ == "__main__":
    main()
