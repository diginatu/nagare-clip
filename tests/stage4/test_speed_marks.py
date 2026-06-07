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
