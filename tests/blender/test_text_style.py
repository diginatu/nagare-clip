"""Tests for apply_text_style() — generic caption_style passthrough.

Any Blender TextStrip attribute (shadow_color, shadow_offset, shadow_blur,
box_margin, ...) should be settable from config without per-key code, across
captions, overlays and speed marks. The five layout keys with per-call defaults
(font_size, alignment_x, anchor_y, location_x, location_y) are applied by each
placement function and must NOT be touched by the generic passthrough.
"""

from __future__ import annotations

import logging
import sys
from unittest import mock
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.blender.timeline import (  # noqa: E402
    OVERLAY_CHANNEL,
    SPEED_MARK_CHANNEL,
    apply_text_style,
    build_timeline_map,
    place_captions,
    place_overlays,
    place_speed_marks,
)


class FakeStrip:
    """Faithful stand-in for a Blender TextStrip.

    Only the predeclared attributes "exist" (so ``hasattr`` mirrors RNA);
    assigning an unknown attribute raises ``AttributeError`` like RNA does.
    ``location`` is a real indexable list.
    """

    _VALID = frozenset(
        {
            "text",
            "font",
            "font_size",
            "alignment_x",
            "anchor_y",
            "color",
            "use_shadow",
            "shadow_color",
            "shadow_offset",
            "shadow_blur",
            "shadow_angle",
            "wrap_width",
            "use_outline",
            "outline_color",
            "outline_width",
            "use_box",
            "box_color",
            "box_margin",
            "box_roundness",
        }
    )

    def __init__(self):
        object.__setattr__(self, "location", [0.0, 0.0])
        for name in self._VALID:
            object.__setattr__(self, name, None)

    def __setattr__(self, name, value):
        if name != "location" and name not in self._VALID:
            raise AttributeError(name)
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        # Only reached for names not set in __init__ (i.e. invalid attrs), so
        # hasattr() returns False for them — mirroring an unknown RNA property.
        raise AttributeError(name)


def _seq_returning(strip):
    seq = MagicMock()
    seq.new_effect = MagicMock(return_value=strip)
    return seq


def _simple_tl_map(fps: float = 30.0):
    return build_timeline_map(
        [{"start": 0.0, "end": 4.0}], effective_fps=fps, source_fps=fps
    )


# --- unit tests for the helper itself -------------------------------------


def test_apply_text_style_forwards_arbitrary_attrs():
    strip = FakeStrip()
    apply_text_style(
        strip,
        {
            "shadow_color": [0.0, 0.0, 0.0, 1.0],
            "shadow_offset": 0.02,
            "shadow_blur": 0.3,
        },
    )
    assert strip.shadow_color == [0.0, 0.0, 0.0, 1.0]
    assert strip.shadow_offset == 0.02
    assert strip.shadow_blur == 0.3


def test_apply_text_style_skips_mapped_layout_keys():
    strip = FakeStrip()
    apply_text_style(
        strip,
        {
            "font_size": 99,
            "alignment_x": "LEFT",
            "anchor_y": "TOP",
            "location_x": 0.1,
            "location_y": 0.2,
        },
    )
    # The helper must leave layout keys to the caller (unset here).
    assert strip.font_size is None
    assert strip.alignment_x is None
    assert strip.anchor_y is None
    assert strip.location == [0.0, 0.0]


def test_apply_text_style_warns_and_skips_unknown_key(caplog):
    strip = FakeStrip()
    with caplog.at_level(logging.WARNING):
        apply_text_style(strip, {"bogus_attr": 1, "shadow_blur": 0.5})
    assert strip.shadow_blur == 0.5
    assert any("bogus_attr" in rec.message for rec in caplog.records)


# --- integration: all three placement functions forward arbitrary attrs ----


def test_place_captions_forwards_shadow_attrs():
    fps = 30.0
    strip = FakeStrip()
    seq = _seq_returning(strip)
    place_captions(
        [{"start": 0.5, "end": 1.5, "text": "hi"}],
        _simple_tl_map(fps),
        effective_fps=fps,
        sequence_collection=seq,
        caption_style={"shadow_color": [1.0, 0.0, 0.0, 1.0], "shadow_offset": 0.05},
    )
    assert strip.shadow_color == [1.0, 0.0, 0.0, 1.0]
    assert strip.shadow_offset == 0.05


def test_place_overlays_forwards_shadow_attrs():
    fps = 30.0
    strip = FakeStrip()
    seq = _seq_returning(strip)
    place_overlays(
        [{"start": 0.5, "end": 1.5, "text": "hi"}],
        _simple_tl_map(fps),
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={"shadow_blur": 0.4, "box_margin": 0.1},
        channel=OVERLAY_CHANNEL,
    )
    assert strip.shadow_blur == 0.4
    assert strip.box_margin == 0.1


def test_place_speed_marks_forwards_shadow_attrs():
    fps = 30.0
    strip = FakeStrip()
    seq = _seq_returning(strip)
    place_speed_marks(
        [{"start": 0.5, "end": 1.5, "factor": 2.0}],
        _simple_tl_map(fps),
        effective_fps=fps,
        sequence_collection=seq,
        mark_style={"shadow_angle": 0.7, "shadow_color": [0.1, 0.1, 0.1, 1.0]},
        channel=SPEED_MARK_CHANNEL,
    )
    assert strip.shadow_angle == 0.7
    assert strip.shadow_color == [0.1, 0.1, 0.1, 1.0]


# --- font: absolute path, loaded as a VectorFont, hard error otherwise -------


def test_apply_text_style_loads_font_from_absolute_path(tmp_path):
    from nagare_clip.blender import timeline

    font_file = tmp_path / "MyFont.ttf"
    font_file.write_bytes(b"\x00")
    fake_vfont = object()
    strip = FakeStrip()
    with mock.patch.object(
        timeline.bpy.data.fonts, "load", return_value=fake_vfont
    ) as load:
        apply_text_style(strip, {"font": str(font_file)})
    load.assert_called_once_with(str(font_file), check_existing=True)
    assert strip.font is fake_vfont


def test_apply_text_style_font_rejects_relative_path():
    strip = FakeStrip()
    with pytest.raises(ValueError):
        apply_text_style(strip, {"font": "MyFont.ttf"})


def test_apply_text_style_font_missing_file_raises(tmp_path):
    strip = FakeStrip()
    missing = tmp_path / "nope.ttf"  # absolute but does not exist
    with pytest.raises(FileNotFoundError):
        apply_text_style(strip, {"font": str(missing)})


def test_place_captions_loads_font(tmp_path):
    from nagare_clip.blender import timeline

    font_file = tmp_path / "Cap.ttf"
    font_file.write_bytes(b"\x00")
    fake_vfont = object()
    fps = 30.0
    strip = FakeStrip()
    seq = _seq_returning(strip)
    with mock.patch.object(
        timeline.bpy.data.fonts, "load", return_value=fake_vfont
    ):
        place_captions(
            [{"start": 0.5, "end": 1.5, "text": "hi"}],
            _simple_tl_map(fps),
            effective_fps=fps,
            sequence_collection=seq,
            caption_style={"font": str(font_file)},
        )
    assert strip.font is fake_vfont


def test_existing_passthrough_keys_still_work():
    """Regression: keys formerly hand-listed (use_shadow/outline/box) still set."""
    fps = 30.0
    strip = FakeStrip()
    seq = _seq_returning(strip)
    place_captions(
        [{"start": 0.5, "end": 1.5, "text": "hi"}],
        _simple_tl_map(fps),
        effective_fps=fps,
        sequence_collection=seq,
        caption_style={
            "use_shadow": True,
            "use_outline": True,
            "outline_color": [0.0, 0.0, 0.0, 1.0],
            "outline_width": 0.5,
            "use_box": True,
            "box_color": [0.2, 0.2, 0.2, 0.8],
            "color": [1.0, 1.0, 1.0, 1.0],
            "wrap_width": 0.8,
        },
    )
    assert strip.use_shadow is True
    assert strip.use_outline is True
    assert strip.outline_color == [0.0, 0.0, 0.0, 1.0]
    assert strip.outline_width == 0.5
    assert strip.use_box is True
    assert strip.box_color == [0.2, 0.2, 0.2, 0.8]
    assert strip.color == [1.0, 1.0, 1.0, 1.0]
    assert strip.wrap_width == 0.8
