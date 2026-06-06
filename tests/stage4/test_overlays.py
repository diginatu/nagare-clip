"""Tests for place_overlays() — TEXT strip placement for <overlay> markers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.timeline import build_timeline_map, place_overlays


def _seq_with_capture():
    """Return (sequence_collection_mock, captured_kwargs_list)."""
    captured: list = []
    seq = MagicMock()

    def capture_effect(**kwargs):
        captured.append(kwargs)
        m = MagicMock()
        # Allow attribute assignment in place_overlays
        return m

    seq.new_effect = capture_effect
    return seq, captured


def _simple_tl_map(fps: float = 30.0):
    """One 4-second keep interval starting at source 0.0, timeline frame 1."""
    return build_timeline_map(
        [{"start": 0.0, "end": 4.0}], effective_fps=fps, source_fps=fps
    )


def test_overlay_within_keep_interval_creates_text_strip():
    fps = 30.0
    tl_map = _simple_tl_map(fps)
    overlays = [{"start": 1.0, "end": 3.0, "text": "Chapter 1"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    assert kw["type"] == "TEXT"
    assert kw["channel"] == 4
    assert kw["frame_start"] == 1 + 30  # 1.0s * 30fps offset within interval (tl_start=1)
    assert kw["length"] == 60           # 2.0s duration


class _AttrTracker:
    """Records which attributes are set via assignment; has real list 'location'."""

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


def test_overlay_text_assigned_to_strip():
    fps = 30.0
    tl_map = _simple_tl_map(fps)
    overlays = [{"start": 0.5, "end": 1.5, "text": "Hello"}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={"font_size": 70, "location_y": 0.95},
        channel=4,
    )
    assert seq.new_effect.call_count == 1
    assert strip.text == "Hello"
    assert strip.font_size == 70
    assert strip.location[1] == 0.95


def test_overlay_outside_any_keep_interval_is_skipped():
    fps = 30.0
    tl_map = _simple_tl_map(fps)  # covers source 0-4s
    overlays = [{"start": 10.0, "end": 11.0, "text": "Lost"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert captured == []


def test_overlay_inside_sped_up_interval_scales_offsets():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    overlays = [{"start": 1.0, "end": 3.0, "text": "Fast"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    # speed=2.0 halves offsets: 1.0s/2.0 * 30fps = 15 frames offset from tl_start=1
    assert kw["frame_start"] == 1 + 15
    # length: 2.0s/2.0 * 30fps = 30 frames
    assert kw["length"] == 30


def test_overlay_partial_overlap_clamps_to_interval():
    """Overlay extends beyond the keep interval; should clamp to the interval edges."""
    fps = 30.0
    tl_map = _simple_tl_map(fps)  # 0-4s
    overlays = [{"start": 3.0, "end": 6.0, "text": "Edge"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    # Clamped end = 4.0s → frame_start = 1 + 90, length = (4.0-3.0)*30 = 30
    assert kw["frame_start"] == 1 + 90
    assert kw["length"] == 30


def test_overlay_spanning_multiple_keep_intervals():
    """Overlay covering several keep intervals renders one contiguous strip."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 2.0}, {"start": 4.0, "end": 6.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    # interval 1: tl 1-61; interval 2: tl 61-121 (contiguous on timeline)
    overlays = [{"start": 1.0, "end": 5.0, "text": "Banner"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    # start = 1 + 30 (1.0s into interval 1); end = 61 + 30 (5.0s, 1.0s into interval 2)
    assert kw["frame_start"] == 31
    assert kw["length"] == 60


def test_empty_overlay_text_is_skipped():
    fps = 30.0
    tl_map = _simple_tl_map(fps)
    overlays = [{"start": 1.0, "end": 3.0, "text": "   "}]  # whitespace-only
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert captured == []
