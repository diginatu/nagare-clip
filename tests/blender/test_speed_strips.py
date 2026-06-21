"""Tests for speed_factor handling in build_timeline_map and place_captions.

place_strips() requires real Blender to exercise the duplicate-based
flow; its speed handling is verified in blender_place_strips.py /
test_timeline.py integration tests.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub bpy so we can import timeline without Blender
sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.blender.timeline import build_timeline_map, place_captions


def test_build_timeline_map_unsped_interval_unchanged():
    """An interval with no speed_factor produces the same frame count as before."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    assert len(tl_map) == 1
    entry = tl_map[0]
    assert entry["tl_end"] - entry["tl_start"] == 120  # 4s * 30fps
    # speed_factor defaults to 1.0
    assert entry.get("speed_factor", 1.0) == 1.0


def test_build_timeline_map_speed_factor_2_halves_frame_count():
    """speed_factor=2.0 halves the timeline duration."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    entry = tl_map[0]
    assert entry["tl_end"] - entry["tl_start"] == 60  # 4s * 30fps / 2.0
    assert entry["speed_factor"] == 2.0


def test_build_timeline_map_speed_factor_half_doubles_frame_count():
    """speed_factor=0.5 doubles the timeline duration."""
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 2.0, "speed_factor": 0.5}],
        effective_fps=fps,
        source_fps=fps,
    )
    entry = tl_map[0]
    assert entry["tl_end"] - entry["tl_start"] == 120  # 2s * 30fps / 0.5
    assert entry["speed_factor"] == 0.5


def test_build_timeline_map_speed_advances_cursor_correctly_for_subsequent_intervals():
    """The shortened timeline duration of a sped-up interval must affect the
    tl_start of the next interval."""
    fps = 30.0
    tl_map = build_timeline_map(
        [
            {"start": 0.0, "end": 4.0, "speed_factor": 2.0},
            {"start": 5.0, "end": 7.0},
        ],
        effective_fps=fps,
        source_fps=fps,
    )
    first_len = tl_map[0]["tl_end"] - tl_map[0]["tl_start"]
    assert first_len == 60
    # Second interval starts immediately after the first
    assert tl_map[1]["tl_start"] == tl_map[0]["tl_end"]


def _place_captions_with_speed(speed_factor: float):
    """Helper: place one caption inside a speed-modified interval and return
    the kwargs passed to seq.new_effect()."""
    fps = 30.0
    # 4s source interval at given speed
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": speed_factor}],
        effective_fps=fps,
        source_fps=fps,
    )
    # Caption from source 1.0-3.0 (2-second span at speed=1.0)
    captions = [{"start": 1.0, "end": 3.0, "text": "hello"}]
    placed = []
    seq = MagicMock()

    def capture_effect(**kwargs):
        placed.append(kwargs)
        return MagicMock()

    seq.new_effect = capture_effect
    place_captions(captions, tl_map, fps, seq)
    assert len(placed) == 1
    return placed[0]


def test_place_captions_unsped_offset_unchanged():
    """speed=1.0: caption from src 1.0 → tl_start at 1.0 * 30 = frame 30
    (timeline starts at frame 1; offset_start = 30 → tl_start = 31)."""
    kwargs = _place_captions_with_speed(1.0)
    # Caption source offset: 1.0s * 30fps = 30 frames from interval start
    # interval tl_start = 1; so caption tl_start = 1 + 30 = 31
    assert kwargs["frame_start"] == 31
    # Caption duration: 2.0s * 30fps = 60 frames
    assert kwargs["length"] == 60


def test_place_captions_speed_factor_2_halves_offset_and_length():
    """speed=2.0: caption source offset is halved on the timeline."""
    kwargs = _place_captions_with_speed(2.0)
    # Caption source offset: (1.0s / 2.0) * 30fps = 15 frames from interval start
    assert kwargs["frame_start"] == 1 + 15
    # Caption duration: (2.0s / 2.0) * 30fps = 30 frames
    assert kwargs["length"] == 30


def test_place_captions_speed_factor_half_doubles_offset_and_length():
    """speed=0.5: caption source offset is doubled on the timeline."""
    kwargs = _place_captions_with_speed(0.5)
    # Caption source offset: (1.0s / 0.5) * 30fps = 60 frames
    assert kwargs["frame_start"] == 1 + 60
    # Caption duration: (2.0s / 0.5) * 30fps = 120 frames
    assert kwargs["length"] == 120


def test_place_captions_spanning_speed_boundary_accumulates():
    """Caption spanning a speed-boundary split accumulates across both sub-intervals.

    When split_intervals_by_speed produces [0-5s, speed=1.0] + [5-10s, speed=2.0],
    a caption at [4-6s] must extend from its start in the first sub-interval to its
    end in the second — not be truncated at the first sub-interval's src_end.
    """
    fps = 30.0
    tl_map = build_timeline_map(
        [
            {"start": 0.0, "end": 5.0},
            {"start": 5.0, "end": 10.0, "speed_factor": 2.0},
        ],
        effective_fps=fps,
        source_fps=fps,
    )
    # tl_map[0]: tl [1, 151]  (5s * 30fps = 150 frames)
    # tl_map[1]: tl [151, 226] (5s/2 * 30fps = 75 frames)
    captions = [{"start": 4.0, "end": 6.0, "text": "boundary"}]
    placed: list = []
    seq = MagicMock()

    def capture(**kwargs):
        placed.append(kwargs)
        return MagicMock()

    seq.new_effect = capture
    place_captions(captions, tl_map, fps, seq)

    assert len(placed) == 1
    # Part in [0,5,speed=1]: clamped [4,5] → offset_start=120 frames → tl_start=121
    # Part in [5,10,speed=2]: clamped [5,6] → offset_end=15 frames  → tl_end=166
    assert placed[0]["frame_start"] == 121
    assert placed[0]["length"] == 45  # 166 - 121
