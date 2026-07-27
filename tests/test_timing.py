"""Tests for the pure timing helpers (no I/O, no network)."""

from __future__ import annotations

from nagare_clip.timing import format_dur_gap, segment_times


class TestSegmentTimes:
    def test_extracts_start_end_per_segment(self):
        data = {
            "segments": [
                {"start": 1.0, "end": 3.5, "text": "a"},
                {"start": 4.0, "end": 6.0, "text": "b"},
            ]
        }
        assert segment_times(data) == [(1.0, 3.5), (4.0, 6.0)]

    def test_missing_keys_become_none(self):
        data = {"segments": [{"text": "a"}, {"start": 2.0}]}
        assert segment_times(data) == [(None, None), (2.0, None)]

    def test_no_segments_key(self):
        assert segment_times({}) == []


class TestFormatDurGap:
    def test_dur_none_is_empty(self):
        assert format_dur_gap(None, 0.8) == ""

    def test_gap_none_shows_dur_only(self):
        assert format_dur_gap(4.2, None) == "[4.2s]"

    def test_dur_and_gap(self):
        assert format_dur_gap(4.24, 0.81) == "[4.2s, gap 0.8s]"

    def test_negligible_gap_omitted(self):
        # "gap 0.0s" is pure noise (contiguous lines/parts): a gap that would
        # render as 0.0s is omitted, same as no gap at all.
        assert format_dur_gap(4.2, 0.0) == "[4.2s]"
        assert format_dur_gap(4.2, 0.04) == "[4.2s]"

    def test_small_but_visible_gap_still_shown(self):
        assert format_dur_gap(4.2, 0.1) == "[4.2s, gap 0.1s]"

    def test_negative_gap_omitted(self):
        assert format_dur_gap(4.2, -0.5) == "[4.2s]"


class TestSpanSilence:
    def test_no_cuts_zero(self):
        from nagare_clip.timing import span_silence

        assert span_silence(0.0, 10.0, []) == 0.0

    def test_cut_clipped_to_span(self):
        from nagare_clip.timing import span_silence

        assert span_silence(5.0, 15.0, [(0.0, 8.0)]) == 3.0

    def test_overlapping_cuts_merged_before_summing(self):
        from nagare_clip.timing import span_silence

        assert span_silence(0.0, 10.0, [(1.0, 4.0), (3.0, 6.0)]) == 5.0

    def test_none_times_zero(self):
        from nagare_clip.timing import span_silence

        assert span_silence(None, 10.0, [(1.0, 2.0)]) == 0.0
        assert span_silence(1.0, None, [(1.0, 2.0)]) == 0.0

    def test_cut_outside_span_ignored(self):
        from nagare_clip.timing import span_silence

        assert span_silence(0.0, 5.0, [(6.0, 9.0)]) == 0.0


class TestSegmentSilences:
    def test_per_segment_mapping(self):
        from nagare_clip.timing import segment_silences

        seg_times = [(0.0, 10.0), (10.0, 20.0), (None, None)]
        cuts = [(8.0, 12.0)]
        assert segment_silences(seg_times, cuts) == [2.0, 2.0, 0.0]


class TestFormatDurGapSilence:
    def test_silence_renders_speech_silence_bracket(self):
        assert format_dur_gap(13.0, None, 62.9) == "[13.0s speech, 62.9s silence]"

    def test_silence_with_gap(self):
        assert format_dur_gap(13.0, 0.8, 62.9) == "[13.0s speech, 62.9s silence, gap 0.8s]"

    def test_negligible_silence_omitted(self):
        assert format_dur_gap(4.2, 0.8, 0.02) == "[4.2s, gap 0.8s]"
        assert format_dur_gap(4.2, None, None) == "[4.2s]"

    def test_dur_none_still_empty(self):
        assert format_dur_gap(None, None, 62.9) == ""
