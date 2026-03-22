"""Tests for caption frame computation (no Blender required)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub bpy so we can import timeline without Blender
sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.timeline import build_timeline_map, place_captions


def test_adjacent_captions_no_frame_overlap():
    """Captions sharing a boundary must not produce overlapping frames."""
    fps = 59.94  # NTSC fps triggers rounding overlap at boundary 3.46s

    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 10.0}],
        effective_fps=fps,
        source_fps=fps,
    )

    captions = [
        {"start": 1.46, "end": 3.46, "text": "A"},
        {"start": 3.46, "end": 5.46, "text": "B"},
    ]

    # Collect frame_start and length from mock strips
    placed = []
    seq = MagicMock()

    def capture_effect(**kwargs):
        strip = MagicMock()
        placed.append(kwargs)
        return strip

    seq.new_effect = capture_effect

    place_captions(captions, tl_map, fps, seq)

    assert len(placed) == 2, f"Expected 2 caption strips, got {len(placed)}"

    end_a = placed[0]["frame_start"] + placed[0]["length"]
    start_b = placed[1]["frame_start"]
    assert end_a <= start_b, (
        f"Caption overlap: A ends at frame {end_a}, B starts at frame {start_b}"
    )


class _AttrTracker:
    """Records which attributes are set via assignment."""

    def __init__(self):
        object.__setattr__(self, "_assigned", {})
        object.__setattr__(self, "location", [0.0, 0.0])

    def __setattr__(self, name, value):
        self._assigned[name] = value

    def __getattr__(self, name):
        try:
            return self._assigned[name]
        except KeyError:
            raise AttributeError(name)


def _place_single_caption(caption_style=None):
    """Helper: place one caption and return the tracker strip."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 10.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    captions = [{"start": 1.0, "end": 3.0, "text": "hello"}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_captions(captions, tl_map, fps, seq, caption_style=caption_style)
    return strip


def test_new_style_options_applied_when_present():
    """New caption style options are set on the strip when provided."""
    style = {
        "use_shadow": True,
        "wrap_width": 0.8,
        "use_outline": True,
        "outline_color": [1.0, 0.0, 0.0, 1.0],
        "outline_width": 2.5,
        "use_box": True,
        "box_color": [0.0, 0.0, 0.0, 0.7],
    }
    strip = _place_single_caption(caption_style=style)
    assert strip.use_shadow is True
    assert strip.wrap_width == 0.8
    assert strip.use_outline is True
    assert strip.outline_color == [1.0, 0.0, 0.0, 1.0]
    assert strip.outline_width == 2.5
    assert strip.use_box is True
    assert strip.box_color == [0.0, 0.0, 0.0, 0.7]


def test_new_style_options_not_set_when_absent():
    """New caption style options are NOT touched when not in config."""
    strip = _place_single_caption(caption_style={"font_size": 50})
    optional_attrs = (
        "use_shadow",
        "wrap_width",
        "use_outline",
        "outline_color",
        "outline_width",
        "use_box",
        "box_color",
    )
    for attr in optional_attrs:
        assert attr not in strip._assigned, (
            f"{attr} should not be set when absent from caption_style"
        )
