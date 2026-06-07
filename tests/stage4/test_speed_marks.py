"""Tests for place_speed_marks() — auto TEXT badge for <speed> regions."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.timeline import build_timeline_map, place_speed_marks


def _seq_with_capture():
    """Return (sequence_collection_mock, captured_kwargs_list)."""
    captured: list = []
    seq = MagicMock()

    def capture_effect(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    seq.new_effect = capture_effect
    return seq, captured


class _AttrTracker:
    """Records attributes set via assignment; has a real list 'location'."""

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


def test_speed_mark_creates_text_strip_on_channel_5():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert len(captured) == 1
    kw = captured[0]
    assert kw["type"] == "TEXT"
    assert kw["channel"] == 5


def test_speed_mark_factor_formatted_one_decimal():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert strip.text == "x2.0"


def test_speed_mark_custom_template():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 1.5}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 1.5}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="⏩{factor}x",
        mark_style={},
        channel=5,
    )
    assert strip.text == "⏩1.5x"


def test_speed_mark_scales_offsets_inside_sped_up_interval():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    # Mark covers 1.0-3.0s of source; speed 2.0 halves offsets/length.
    speed_ranges = [{"start": 1.0, "end": 3.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert len(captured) == 1
    kw = captured[0]
    # 1.0s/2.0 * 30 = 15 frames offset from tl_start=1
    assert kw["frame_start"] == 1 + 15
    # (3.0-1.0)/2.0 * 30 = 30 frames
    assert kw["length"] == 30


def test_speed_mark_spanning_multiple_keep_intervals():
    fps = 30.0
    tl_map = build_timeline_map(
        [
            {"start": 0.0, "end": 2.0, "speed_factor": 2.0},
            {"start": 4.0, "end": 6.0, "speed_factor": 2.0},
        ],
        effective_fps=fps,
        source_fps=fps,
    )
    # Each interval is 2.0s/2.0 = 1.0s = 30 frames on timeline.
    # interval 1: tl 1-31; interval 2: tl 31-61 (contiguous).
    speed_ranges = [{"start": 0.0, "end": 6.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert len(captured) == 1
    kw = captured[0]
    assert kw["frame_start"] == 1
    assert kw["length"] == 60


def test_speed_mark_outside_any_keep_interval_is_skipped():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0}], effective_fps=fps, source_fps=fps
    )
    speed_ranges = [{"start": 10.0, "end": 11.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert captured == []


def test_speed_mark_style_overrides_applied():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={"font_size": 40, "alignment_x": "RIGHT", "location_x": 0.9},
        channel=5,
    )
    assert strip.font_size == 40
    assert strip.alignment_x == "RIGHT"
    assert strip.location[0] == 0.9


def test_speed_mark_color_applied_when_present():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={"color": [1.0, 0.0, 0.0, 1.0]},
        channel=5,
    )
    assert strip.color == [1.0, 0.0, 0.0, 1.0]


def test_speed_mark_color_not_set_when_absent():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={"font_size": 40},
        channel=5,
    )
    assert "color" not in strip._assigned
