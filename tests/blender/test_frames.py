"""Host-side tests for the pure frame helpers (no bpy, no Blender)."""

from __future__ import annotations

from nagare_clip.blender.frames import clamp_frames, retimed_frame_count


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


class TestRetimedFrameCount:
    def test_exact_quotient(self):
        assert retimed_frame_count(120, 2.0) == 60

    def test_speed_one_is_identity(self):
        assert retimed_frame_count(133, 1.0) == 133

    def test_slow_motion_lengthens(self):
        assert retimed_frame_count(60, 0.5) == 120

    def test_rounds_down_below_half(self):
        assert retimed_frame_count(131, 8.0) == 16  # 16.375

    def test_rounds_up_above_half(self):
        assert retimed_frame_count(133, 8.0) == 17  # 16.625

    def test_half_rounds_away_from_zero_not_to_even(self):
        """Blender rounds .5 away from zero; Python's round() rounds to even.

        The real water_pump_3 cases: these five strips came out one frame
        longer than the placement loop predicted, so the cursor under-advanced
        and the next strip was shunted off channel 1 by the overlap.
        """
        assert retimed_frame_count(132, 8.0) == 17  # round() -> 16
        assert retimed_frame_count(308, 8.0) == 39  # round() -> 38
        assert retimed_frame_count(196, 8.0) == 25  # round() -> 24
        # the other side of half-to-even: these already agreed
        assert retimed_frame_count(140, 8.0) == 18
        assert retimed_frame_count(204, 8.0) == 26

    def test_never_shorter_than_one_frame(self):
        assert retimed_frame_count(1, 8.0) == 1
