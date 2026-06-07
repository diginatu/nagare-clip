"""Speed-mark wiring: style resolution + enabled gate in blender_cli."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.blender_cli import resolve_speed_mark_style


def test_resolve_speed_mark_style_merges_caption_then_overrides():
    caption_style = {"font_size": 50, "alignment_x": "CENTER", "location_y": 0.05}
    speed_mark_cfg = {
        "enabled": True,
        "template": "x{factor}",
        "font_size": 35,
        "alignment_x": "RIGHT",
        "location_y": 0.95,
    }
    style = resolve_speed_mark_style(caption_style, speed_mark_cfg)
    # caption defaults present unless overridden
    assert style["font_size"] == 35       # override wins
    assert style["alignment_x"] == "RIGHT"  # override wins
    assert style["location_y"] == 0.95    # override wins
    # non-style keys must NOT leak into the strip style
    assert "enabled" not in style
    assert "template" not in style
