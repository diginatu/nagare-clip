"""Host-side tests for the pure frame-clamp helper (no bpy, no Blender)."""

from __future__ import annotations

from nagare_clip.blender.frames import clamp_frames


class TestClampFrames:
    def test_in_range_untouched(self):
        assert clamp_frames(10, 100, 200) == (10, 100, True)

    def test_one_frame_tail_overshoot_is_negligible(self):
        # the real water_pump_3 case: requested 18412-18641 vs 18640 frames
        assert clamp_frames(18412, 18641, 18640) == (18412, 18640, True)

    def test_multi_frame_overshoot_is_not_negligible(self):
        assert clamp_frames(10, 205, 200) == (10, 200, False)

    def test_start_clamp_is_not_negligible(self):
        bounded_start, bounded_end, negligible = clamp_frames(250, 260, 200)
        assert bounded_start == 199
        assert bounded_end == 200
        assert negligible is False

    def test_end_forced_after_start(self):
        # degenerate zero/negative-length request still yields >= 1 frame
        assert clamp_frames(50, 50, 200)[:2] == (50, 51)
