"""Run inside Blender: build one strip's proxy the way the UI's Rebuild Proxy does.

Usage: blender -b --factory-startup --python blender_build_proxy.py -- SRC SIZE
"""

import sys

import bpy

src, size = sys.argv[sys.argv.index("--") + 1 :][:2]
bpy.ops.wm.read_homefile(app_template="Video_Editing")
scene = bpy.context.scene
scene.sequence_editor_create()
strip = scene.sequence_editor.strips.new_movie(name="v", filepath=src, channel=1, frame_start=1)
strip.use_proxy = True
for n in (25, 50, 75, 100):
    setattr(strip.proxy, f"build_{n}", n == int(size))
strip.select = True
for window in bpy.context.window_manager.windows:
    for area in window.screen.areas:
        if area.type == "SEQUENCE_EDITOR":
            region = next(r for r in area.regions if r.type == "WINDOW")
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.sequencer.rebuild_proxy()
            sys.exit(0)
sys.exit("no SEQUENCE_EDITOR area")
