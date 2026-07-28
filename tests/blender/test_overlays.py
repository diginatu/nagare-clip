"""Tests for place_overlays() — TEXT strip placement for <overlay/> markers.

An overlay is a point + a duration in *edited-timeline* seconds: the start is
mapped through the timeline map (speed-aware), the length is simply
``duration * fps`` output frames, so cuts and speed ranges inside the window
cannot shorten the time a viewer gets to read the text.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.blender.timeline import (
    OVERLAY_CHANNEL,
    build_timeline_map,
    place_overlays,
)


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
    return build_timeline_map([{"start": 0.0, "end": 4.0}], effective_fps=fps, source_fps=fps)


def _place(overlays, tl_map, fps=30.0):
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=OVERLAY_CHANNEL,
    )
    return captured


def test_overlay_length_comes_from_duration():
    fps = 30.0
    captured = _place([{"start": 1.0, "duration": 2.0, "text": "Chapter 1"}], _simple_tl_map(fps))
    assert len(captured) == 1
    kw = captured[0]
    assert kw["type"] == "TEXT"
    assert kw["channel"] == OVERLAY_CHANNEL
    assert kw["frame_start"] == 1 + 30  # 1.0s * 30fps offset within interval (tl_start=1)
    assert kw["length"] == 60  # 2.0s of timeline


def test_duration_is_not_shortened_by_a_cut_inside_the_window():
    """The source seconds 2.0-4.0 are cut; a 3-second overlay still reads for
    3 seconds on the edited timeline."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 2.0}, {"start": 4.0, "end": 6.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    captured = _place([{"start": 1.0, "duration": 3.0, "text": "Banner"}], tl_map)
    assert len(captured) == 1
    assert captured[0]["frame_start"] == 1 + 30
    assert captured[0]["length"] == 90


def test_duration_is_not_shortened_by_a_speed_range():
    """A 2x interval halves the *source* span the window covers, but the
    on-screen time is stated directly, so the strip is still duration long."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 10.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    captured = _place([{"start": 1.0, "duration": 2.0, "text": "Fast"}], tl_map)
    assert len(captured) == 1
    # Start offset is speed-scaled: 1.0s/2.0 * 30fps = 15 frames from tl_start=1
    assert captured[0]["frame_start"] == 1 + 15
    assert captured[0]["length"] == 60


def test_overlay_clamped_to_the_end_of_the_source_timeline():
    """A duration running past the source's last frame is clamped, so the text
    never bleeds over the next source's strips."""
    fps = 30.0
    tl_map = _simple_tl_map(fps)  # source 0-4s → timeline frames 1..121
    captured = _place([{"start": 3.0, "duration": 10.0, "text": "Edge"}], tl_map)
    assert len(captured) == 1
    assert captured[0]["frame_start"] == 1 + 90
    assert captured[0]["length"] == 30  # clamped from 300


def test_overlay_start_outside_any_keep_interval_is_skipped():
    fps = 30.0
    captured = _place([{"start": 10.0, "duration": 2.0, "text": "Lost"}], _simple_tl_map(fps))
    assert captured == []


def test_empty_overlay_text_is_skipped():
    fps = 30.0
    captured = _place([{"start": 1.0, "duration": 2.0, "text": "   "}], _simple_tl_map(fps))
    assert captured == []


def test_non_positive_duration_is_skipped():
    fps = 30.0
    captured = _place([{"start": 1.0, "duration": 0.0, "text": "Zero"}], _simple_tl_map(fps))
    assert captured == []


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


def _place_on_tracker(style):
    fps = 30.0
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_overlays(
        [{"start": 0.5, "duration": 1.0, "text": "Hello"}],
        _simple_tl_map(fps),
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style=style,
        channel=OVERLAY_CHANNEL,
    )
    return seq, strip


def test_overlay_text_assigned_to_strip():
    seq, strip = _place_on_tracker({"font_size": 70, "location_y": 0.95})
    assert seq.new_effect.call_count == 1
    assert strip.text == "Hello"
    assert strip.font_size == 70
    assert strip.location[1] == 0.95


def test_overlay_color_applied_when_present():
    _, strip = _place_on_tracker({"color": [0.0, 1.0, 0.5, 1.0]})
    assert strip.color == [0.0, 1.0, 0.5, 1.0]


def test_overlay_color_not_set_when_absent():
    _, strip = _place_on_tracker({"font_size": 70})
    assert "color" not in strip._assigned
