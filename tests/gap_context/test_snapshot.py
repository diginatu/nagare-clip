from nagare_clip.gap_context.snapshot import frame_relpath, frame_times, select_gaps


def test_select_gaps_keeps_only_spans_at_or_above_min_gap():
    ranges = [(1.0, 2.0), (10.0, 13.0), (20.0, 32.0)]
    assert select_gaps(ranges, 3.0) == [(10.0, 13.0), (20.0, 32.0)]


def test_select_gaps_sorts_by_start():
    assert select_gaps([(20.0, 30.0), (5.0, 10.0)], 3.0) == [(5.0, 10.0), (20.0, 30.0)]


def test_select_gaps_empty_when_none_long_enough():
    assert select_gaps([(1.0, 2.0)], 3.0) == []


def test_frame_times_start_mid_end_inside_the_span():
    # 0.2s inset keeps the frames off the boundary (where the previous/next
    # word may still be on screen).
    assert frame_times(10.0, 20.0) == [10.2, 15.0, 19.8]


def test_frame_times_dedupes_on_a_short_span():
    # A 3.0s span: start 3.2 / mid 4.5 / end 5.8 are all distinct...
    assert len(frame_times(3.0, 6.0)) == 3
    # ...but a span so short the inset frames collide yields fewer, still >= 1.
    times = frame_times(10.0, 10.3)
    assert times == [10.15]


def test_frame_relpath_is_stable_and_stem_scoped():
    assert frame_relpath("talk1", 12.6) == "frames/talk1/12.600.jpg"


class TestSsimHelpers:
    def test_ssim_relpath(self):
        from nagare_clip.gap_context.snapshot import ssim_relpath

        # A fourth (pair-index) arg disambiguates multiple consecutive-frame
        # SSIM comparisons within the same gap.
        assert ssim_relpath("v", 108.4, 119.25, 0) == "frames/v/ssim_108.400-119.250_0.txt"
        assert ssim_relpath("v", 108.4, 119.25, 1) == "frames/v/ssim_108.400-119.250_1.txt"

    def test_parse_ssim_stats_reads_all_score(self):
        from nagare_clip.gap_context.snapshot import parse_ssim_stats

        line = "n:1 Y:0.994828 U:0.998750 V:0.998691 All:0.996132 (24.123456)\n"
        assert parse_ssim_stats(line) == 0.996132

    def test_parse_ssim_stats_garbage_is_none(self):
        from nagare_clip.gap_context.snapshot import parse_ssim_stats

        assert parse_ssim_stats("") is None
        assert parse_ssim_stats("no scores here") is None
        assert parse_ssim_stats("All:notanumber") is None
