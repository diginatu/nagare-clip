"""Run inside Blender: reset_scene + set_preview_proxy_size, print every
sequencer space's proxy_render_size as JSON on the last line."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import bpy  # noqa: E402

from nagare_clip.blender.scene import reset_scene, set_preview_proxy_size  # noqa: E402

size = int(sys.argv[sys.argv.index("--") + 1])
reset_scene()
set_preview_proxy_size(size)
sizes = [
    space.proxy_render_size
    for screen in bpy.data.screens
    for area in screen.areas
    for space in area.spaces
    if space.type == "SEQUENCE_EDITOR"
]
print("RESULT " + json.dumps(sizes))
