"""Tests for the pure timing helpers (no I/O, no network)."""

from __future__ import annotations

from nagare_clip.timing import format_dur_gap, segment_times


class TestSegmentTimes:
    def test_extracts_start_end_per_segment(self):
        data = {"segments": [
            {"start": 1.0, "end": 3.5, "text": "a"},
            {"start": 4.0, "end": 6.0, "text": "b"},
        ]}
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

    def test_negative_gap_clamped_to_zero(self):
        assert format_dur_gap(4.2, -0.5) == "[4.2s, gap 0.0s]"
