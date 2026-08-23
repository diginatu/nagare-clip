"""Tests for resolving the segment order from lines into source seconds.

`intervals` is the single conversion point: the plan is the authority on the
order up to and including this stage, `timeline.json` is the authority after it.
"""

from __future__ import annotations

from nagare_clip.intervals.manifest import build_manifest
from nagare_clip.order import Segment, TimelineSegment

# 5 lines; line k spans TIMES[k-1].
TIMES = {"a": [(0.5, 4.0), (5.0, 9.0), (12.0, 18.0), (20.0, 25.0), (30.0, 40.0)]}
DURATIONS = {"a": 44.0, "b": 60.0}


class TestWholeSource:
    def test_a_whole_source_segment_spans_the_whole_source(self):
        assert build_manifest([Segment("a", None)], TIMES, DURATIONS) == [
            TimelineSegment("a", 0.0, 44.0)
        ]

    def test_needs_no_segment_times_at_all(self):
        # The identity path must not depend on reading a transcript.
        assert build_manifest([Segment("b", None)], {}, DURATIONS) == [
            TimelineSegment("b", 0.0, 60.0)
        ]


class TestBoundaries:
    def test_a_segment_starts_where_its_previous_line_ended(self):
        # The silent gap before a segment's first line belongs to that segment,
        # so a moved segment takes its own run-up with it.
        out = build_manifest([Segment("a", (1, 2)), Segment("a", (3, 5))], TIMES, DURATIONS)
        assert out == [
            TimelineSegment("a", 0.0, 9.0, lines=(1, 2)),
            TimelineSegment("a", 9.0, 44.0, lines=(3, 5)),
        ]

    def test_the_first_segment_starts_at_zero_not_at_its_first_word(self):
        out = build_manifest([Segment("a", (1, 1))], {"a": TIMES["a"]}, {"a": 44.0})
        assert out[0].start == 0.0

    def test_the_last_segment_ends_at_the_source_duration(self):
        out = build_manifest([Segment("a", (5, 5))], TIMES, DURATIONS)
        assert out[0].end == 44.0

    def test_a_middle_segment_is_bounded_by_line_ends_on_both_sides(self):
        out = build_manifest([Segment("a", (3, 4))], TIMES, DURATIONS)
        assert out == [TimelineSegment("a", 9.0, 25.0, lines=(3, 4))]

    def test_the_segments_of_a_source_partition_its_seconds(self):
        segments = [Segment("a", (1, 2)), Segment("a", (3, 3)), Segment("a", (4, 5))]
        out = build_manifest(segments, TIMES, DURATIONS)
        by_line = sorted(out, key=lambda e: e.lines or (0, 0))
        assert by_line[0].start == 0.0
        assert by_line[-1].end == 44.0
        for earlier, later in zip(by_line, by_line[1:]):
            assert earlier.end == later.start


class TestPlaybackOrder:
    def test_entries_keep_the_order_they_were_given(self):
        segments = [Segment("a", (3, 5)), Segment("b", None), Segment("a", (1, 2))]
        out = build_manifest(segments, TIMES, DURATIONS)
        assert [(e.stem, e.lines) for e in out] == [("a", (3, 5)), ("b", None), ("a", (1, 2))]


class TestDegrading:
    def test_a_source_with_no_duration_is_skipped(self):
        out = build_manifest([Segment("a", None), Segment("z", None)], TIMES, DURATIONS)
        assert [e.stem for e in out] == ["a"]

    def test_an_unresolvable_boundary_degrades_the_whole_manifest(self):
        # Never a partial resolution: a source that cannot be split would
        # otherwise collapse into one entry and silently change the order.
        assert build_manifest([Segment("a", (2, 5))], {}, DURATIONS) == []

    def test_a_missing_line_time_degrades_the_whole_manifest(self):
        times = {"a": [(0.5, 4.0), (5.0, None), (12.0, 18.0), (20.0, 25.0), (30.0, 40.0)]}
        assert build_manifest([Segment("a", (3, 5))], times, DURATIONS) == []

    def test_a_boundary_past_the_known_lines_degrades(self):
        assert build_manifest([Segment("a", (9, 12))], TIMES, DURATIONS) == []

    def test_an_empty_segment_in_seconds_degrades(self):
        # Two lines sharing an end time would give the middle segment no length.
        times = {"a": [(0.5, 4.0), (5.0, 4.0), (12.0, 18.0)]}
        segments = [Segment("a", (1, 1)), Segment("a", (2, 2)), Segment("a", (3, 3))]
        assert build_manifest(segments, times, {"a": 20.0}) == []
