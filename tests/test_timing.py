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

    def test_sub_second_silence_does_not_split_the_bracket(self):
        # The split exists to flag a LONG internal silence the editor will drop.
        # A few tenths of a second is breath, not dead air: it was splitting
        # 77 of the corpus's 312 silence figures into noise.  Unsplit, the
        # breath stays part of the span (4.2 speech + 0.3 silence = 4.5).
        assert format_dur_gap(4.2, 0.8, 0.3) == "[4.5s, gap 0.8s]"
        assert format_dur_gap(4.2, None, 0.9) == "[5.1s]"

    def test_silence_boundary_is_one_second(self):
        # Either side of the threshold, so the constant cannot drift silently.
        # The SPAN is continuous across it — 4.2+0.99 renders as 5.2s, and one
        # hundredth later the same 5.2 seconds render as 4.2 speech + 1.0
        # silence.  Only the presentation changes at the boundary, never the
        # amount of time the bracket claims the line occupies.
        assert format_dur_gap(4.2, None, 0.99) == "[5.2s]"
        assert format_dur_gap(4.2, None, 1.0) == "[4.2s speech, 1.0s silence]"

    def test_degenerate_alignment_line_renders_no_speech_figure(self):
        # WhisperX alignment failure gives every word exactly 0.020s, the span
        # lands inside an audio_silence cut, and the caller's
        # max(raw - sil, 0) collapses to 0.0.  The old 0.05s de facto cut-off
        # still took the split branch and printed "0.0s speech, 0.1s silence".
        assert "speech" not in format_dur_gap(0.0, 8.0, 0.14)

    def test_sub_threshold_silence_is_folded_back_into_the_duration(self):
        # The caller has already subtracted the silence (director_llm's
        # `dur = max(raw - sil, 0.0)`), so *dur* is speech-only on arrival.
        # Not splitting is only half the job: printing the speech-only figure
        # as if it were the line's duration under-reports every line the
        # subtraction touched, and on the 8 degenerate-alignment lines it
        # reports a line that exists as lasting no time at all.  Below the
        # threshold the two halves are one span again.
        assert format_dur_gap(0.0, None, 0.14) == "[0.1s]"
        assert format_dur_gap(0.0, 8.0, 0.30) == "[0.3s, gap 8.0s]"
        # Above it the split still wins, and the speech figure stays raw.
        assert format_dur_gap(13.0, None, 62.9) == "[13.0s speech, 62.9s silence]"
        # An absent silence is not a zero one: nothing to fold, nothing added.
        assert format_dur_gap(4.2, None, None) == "[4.2s]"
