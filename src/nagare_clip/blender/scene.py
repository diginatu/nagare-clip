"""Blender scene setup and source metadata extraction."""

from __future__ import annotations

from pathlib import Path

import bpy

# Last resort when a source carries no usable fps. Not a config key: an fps the
# project actually wants is `blender.render.fps`, which overrides the scene
# regardless of what was measured here.
FALLBACK_FPS = 30.0


def reset_scene() -> bpy.types.Scene:
    bpy.ops.wm.read_factory_settings(use_empty=False)
    bpy.ops.wm.read_homefile(app_template="Video_Editing")
    scene = bpy.context.scene
    scene.sequence_editor_create()
    return scene


def set_preview_proxy_size(size: int) -> None:
    """Make every sequencer space preview through the ``proxy_<size>`` proxy.

    The Video_Editing template previews at PROXY_100, so any other
    ``blender.proxy_size`` would look for a proxy that was never built and
    decode the original instead.
    """
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type == "SEQUENCE_EDITOR":
                    space.proxy_render_size = f"PROXY_{size}"


def load_source_metadata(source_path: Path) -> tuple[float, int, int]:
    clip = bpy.data.movieclips.load(str(source_path))
    fps = float(clip.fps) if clip.fps and clip.fps > 0 else FALLBACK_FPS
    width, height = clip.size
    bpy.data.movieclips.remove(clip)
    return fps, int(width), int(height)
