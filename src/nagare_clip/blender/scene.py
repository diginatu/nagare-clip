"""Blender scene setup and source metadata extraction."""

from __future__ import annotations

from pathlib import Path

import bpy


def reset_scene() -> bpy.types.Scene:
    bpy.ops.wm.read_factory_settings(use_empty=False)
    bpy.ops.wm.read_homefile(app_template="Video_Editing")
    scene = bpy.context.scene
    scene.sequence_editor_create()
    return scene


def load_source_metadata(
    source_path: Path, *, default_fps: float = 30.0
) -> tuple[float, int, int]:
    clip = bpy.data.movieclips.load(str(source_path))
    fps = float(clip.fps) if clip.fps and clip.fps > 0 else default_fps
    width, height = clip.size
    bpy.data.movieclips.remove(clip)
    return fps, int(width), int(height)
