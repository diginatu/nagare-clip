"""Tests for the segment order: the finished video's playback order.

Pure module (no I/O beyond the manifest read/write helpers): a Segment is a
stem plus an optional line range, ``None`` meaning the whole source.
"""

from __future__ import annotations

import json

from nagare_clip.order import (
    MANIFEST_NAME,
    Segment,
    TimelineSegment,
    identity_segments,
    manifest_from_dict,
    manifest_to_dict,
    normalise,
    read_manifest,
    segment_label,
    segment_unit,
    segments_from_dict,
    segments_to_dict,
    validate_segments,
    write_manifest,
)


class TestIdentitySegments:
    def test_one_whole_source_segment_per_stem_in_order(self):
        # Shooting order expressed in the new form: no line counts needed, so
        # the fallback cannot fail for want of an input.
        assert identity_segments(["a", "b"]) == [Segment("a", None), Segment("b", None)]

    def test_no_stems_is_no_segments(self):
        assert identity_segments([]) == []


class TestNormalise:
    def test_full_range_collapses_to_whole_source(self):
        # Segment(stem, (1, N)) and Segment(stem, None) mean the same thing and
        # must be indistinguishable downstream.
        assert normalise([Segment("a", (1, 7))], {"a": 7}) == [Segment("a", None)]

    def test_partial_range_is_left_alone(self):
        assert normalise([Segment("a", (1, 4))], {"a": 7}) == [Segment("a", (1, 4))]

    def test_whole_source_stays_whole_source(self):
        assert normalise([Segment("a", None)], {"a": 7}) == [Segment("a", None)]

    def test_unknown_line_count_is_left_as_written(self):
        assert normalise([Segment("a", (1, 7))], {}) == [Segment("a", (1, 7))]


class TestValidateSegments:
    def test_a_full_partition_has_no_problems(self):
        segments = [Segment("a", (1, 30)), Segment("b", None), Segment("a", (31, 97))]
        assert validate_segments(segments, {"a": 97, "b": 12}) == []

    def test_a_missing_line_is_a_problem(self):
        problems = validate_segments([Segment("a", (1, 30)), Segment("a", (32, 97))], {"a": 97})
        assert len(problems) == 1
        assert "31" in problems[0]

    def test_a_repeated_line_is_a_problem(self):
        problems = validate_segments([Segment("a", (1, 30)), Segment("a", (30, 97))], {"a": 97})
        assert len(problems) == 1
        assert "30" in problems[0]

    def test_not_reaching_the_last_line_is_a_problem(self):
        problems = validate_segments([Segment("a", (1, 30))], {"a": 97})
        assert len(problems) == 1
        assert "97" in problems[0]

    def test_not_starting_at_line_one_is_a_problem(self):
        problems = validate_segments([Segment("a", (2, 97))], {"a": 97})
        assert len(problems) == 1
        assert "1" in problems[0]

    def test_a_source_with_no_segment_is_a_problem(self):
        # Deleting footage is the director's job; a plan that could drop a
        # source by omitting it would make a missing scene indistinguishable
        # from an editorial decision.
        problems = validate_segments([Segment("a", None)], {"a": 5, "b": 9})
        assert len(problems) == 1
        assert "b" in problems[0]

    def test_an_unknown_source_is_a_problem(self):
        problems = validate_segments([Segment("a", None), Segment("z", None)], {"a": 5})
        assert any("z" in p for p in problems)

    def test_a_reversed_range_is_a_problem(self):
        # Named as a reversal, not merely as the coverage hole it also leaves.
        problems = validate_segments([Segment("a", (5, 2))], {"a": 5})
        assert any("5-2" in p for p in problems)

    def test_a_range_past_the_last_line_is_a_problem(self):
        assert validate_segments([Segment("a", (1, 9))], {"a": 5}) != []

    def test_an_empty_order_is_a_problem(self):
        # With no sources either, nothing else can object to it.
        assert validate_segments([], {}) != []

    def test_reordering_is_not_a_problem(self):
        # The whole point: coverage is the contract, sequence is free.
        segments = [Segment("b", None), Segment("a", (31, 97)), Segment("a", (1, 30))]
        assert validate_segments(segments, {"a": 97, "b": 12}) == []


class TestSegmentsDict:
    def test_whole_source_omits_lines(self):
        assert segments_to_dict([Segment("a", None)]) == [{"stem": "a"}]

    def test_partial_carries_lines(self):
        assert segments_to_dict([Segment("a", (1, 4))]) == [{"stem": "a", "lines": [1, 4]}]

    def test_round_trip(self):
        segments = [Segment("a", (1, 4)), Segment("b", None)]
        assert segments_from_dict(segments_to_dict(segments)) == segments

    def test_missing_lines_key_reads_as_the_whole_source(self):
        assert segments_from_dict([{"stem": "a"}]) == [Segment("a", None)]

    def test_a_non_list_is_no_order(self):
        assert segments_from_dict(None) == []
        assert segments_from_dict({"stem": "a"}) == []

    def test_malformed_entries_are_skipped(self):
        data = [{"stem": "a"}, {"lines": [1, 2]}, "nope", {"stem": "b", "lines": [1]}]
        assert segments_from_dict(data) == [Segment("a", None)]

    def test_a_boolean_is_not_a_line_number(self):
        assert segments_from_dict([{"stem": "a", "lines": [True, 4]}]) == []


class TestManifest:
    def test_round_trip_through_dict(self):
        entries = [
            TimelineSegment("a", 0.0, 12.5),
            TimelineSegment("b", 30.25, 99.0, lines=(31, 83)),
        ]
        assert manifest_from_dict(manifest_to_dict(entries)) == entries

    def test_whole_source_entry_omits_lines(self):
        data = manifest_to_dict([TimelineSegment("a", 0.0, 12.5)])
        assert data == {"segments": [{"stem": "a", "start": 0.0, "end": 12.5}]}

    def test_a_malformed_entry_is_skipped(self):
        data = {"segments": [{"stem": "a", "start": 0.0, "end": 1.0}, {"stem": "b"}]}
        assert manifest_from_dict(data) == [TimelineSegment("a", 0.0, 1.0)]

    def test_no_segments_key_is_no_manifest(self):
        assert manifest_from_dict({}) == []
        assert manifest_from_dict(None) == []

    def test_write_then_read(self, tmp_path):
        path = tmp_path / MANIFEST_NAME
        entries = [TimelineSegment("a", 0.0, 12.5, lines=(1, 9))]
        write_manifest(path, entries)
        assert read_manifest(path) == entries
        assert json.loads(path.read_text(encoding="utf-8"))["segments"][0]["lines"] == [1, 9]

    def test_reading_a_missing_file_is_no_manifest(self, tmp_path):
        # A project built before this feature has no timeline.json; blender
        # degrades to its per-source loop rather than failing.
        assert read_manifest(tmp_path / MANIFEST_NAME) == []

    def test_a_missing_file_is_not_worth_a_warning(self, tmp_path, caplog):
        # Absent is the normal degrade; warning about it every run would drown
        # the warning that matters, which is a manifest that will not parse.
        with caplog.at_level("WARNING"):
            read_manifest(tmp_path / MANIFEST_NAME)
        assert caplog.text == ""

    def test_an_unreadable_file_is_worth_a_warning(self, tmp_path, caplog):
        path = tmp_path / MANIFEST_NAME
        path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            read_manifest(path)
        assert MANIFEST_NAME in caplog.text

    def test_reading_junk_is_no_manifest(self, tmp_path):
        path = tmp_path / MANIFEST_NAME
        path.write_text("{not json", encoding="utf-8")
        assert read_manifest(path) == []


class TestLabels:
    def test_a_whole_source_segment_is_labelled_by_its_stem_alone(self):
        # Identity keeps today's report filenames and today's prompt wording.
        assert segment_label(Segment("a", None)) == "a"
        assert segment_unit(Segment("a", None)) == "a"

    def test_a_partial_segment_carries_its_range(self):
        assert segment_label(Segment("a", (31, 83))) == "a [31-83]"
        assert segment_unit(Segment("a", (31, 83))) == "a_31-83"


class TestGapEnd:
    """A segment may end on the silence after its last line (``"57~"``)."""

    def test_it_round_trips_in_the_director_json_spelling(self):
        segments = [Segment("a", (31, 57), gap_end=True), Segment("a", (58, 90))]
        data = segments_to_dict(segments)
        assert data[0] == {"stem": "a", "lines": [31, "57~"]}
        assert segments_from_dict(data) == segments

    def test_a_start_cannot_carry_the_flag(self):
        assert segments_from_dict([{"stem": "a", "lines": ["31~", 57]}]) == []

    def test_it_is_labelled(self):
        assert segment_label(Segment("a", (31, 57), gap_end=True)) == "a [31-57~]"

    def test_on_the_last_line_it_means_nothing_and_is_dropped(self):
        assert normalise([Segment("a", (4, 9), gap_end=True)], {"a": 9}) == [Segment("a", (4, 9))]

    def test_a_full_range_ending_on_it_collapses_to_the_whole_source(self):
        assert normalise([Segment("a", (1, 9), gap_end=True)], {"a": 9}) == [Segment("a", None)]

    def test_mid_source_it_is_kept(self):
        seg = Segment("a", (1, 5), gap_end=True)
        assert normalise([seg], {"a": 9}) == [seg]

    def test_coverage_is_still_counted_in_lines(self):
        order = [Segment("a", (6, 9)), Segment("a", (1, 5), gap_end=True)]
        assert validate_segments(order, {"a": 9}) == []


class TestManifestGapEnd:
    def test_the_manifest_spells_it_as_order_json_does(self):
        entry = TimelineSegment("a", 0.0, 59.0, lines=(1, 5), gap_end=True)
        data = manifest_to_dict([entry])
        assert data["segments"][0]["lines"] == [1, "5~"]
        assert manifest_from_dict(data) == [entry]
