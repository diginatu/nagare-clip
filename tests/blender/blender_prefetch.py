"""Run inside Blender: reset_scene + set_prefetch, save, reopen, print
SequenceEditor.use_prefetch as JSON on the last line."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import bpy  # noqa: E402

from nagare_clip.blender.scene import reset_scene, set_prefetch  # noqa: E402

enabled = sys.argv[sys.argv.index("--") + 1] == "1"
scene = reset_scene()
set_prefetch(scene, enabled)
path = str(Path(tempfile.mkdtemp()) / "prefetch.blend")
bpy.ops.wm.save_as_mainfile(filepath=path)
bpy.ops.wm.open_mainfile(filepath=path)
print("RESULT " + json.dumps(bpy.context.scene.sequence_editor.use_prefetch))
