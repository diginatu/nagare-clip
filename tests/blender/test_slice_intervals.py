"""Tests for slicing one source's intervals JSON to a segment's time window.

This is what makes a segment placeable: a boundary can fall inside a keep
interval or a speed range, so both are split at it, while a caption or an
overlay belongs to exactly one segment (the one containing its start).
"""

from __future__ import annotations

from nagare_clip.blender.frames import ordered_sources, slice_intervals_data
from nagare_clip.order import TimelineSegment

DATA = {
    "source_file": "a.mp4",
    "duration_sec": 100.0,
    "keep_intervals": [
        {"start": 0.0, "end": 10.0},
        {"start": 20.0, "end": 40.0},
        {"start": 60.0, "end": 70.0},
    ],
    "captions": [
        {"start": 1.0, "end": 5.0, "text": "one"},
        {"start": 25.0, "end": 35.0, "text": "two"},
        {"start": 61.0, "end": 65.0, "text": "three"},
    ],
    "overlays": [
        {"start": 2.0, "duration": 3.0, "text": "first"},
        {"start": 62.0, "duration": 3.0, "text": "last"},
    ],
    "speed_ranges": [{"start": 20.0, "end": 70.0, "factor": 4.0}],
}


class TestFullCoverIsANoOp:
    def test_a_window_covering_everything_returns_the_same_content(self):
        # The identity path must be provably today's: a whole-source window
        # introduces no boundary and drops nothing.
        out = slice_intervals_data(DATA, 0.0, 100.0)
        for key in ("keep_intervals", "captions", "overlays", "speed_ranges"):
            assert out[key] == DATA[key]

    def test_metadata_is_carried_through(self):
        out = slice_intervals_data(DATA, 0.0, 100.0)
        assert out["source_file"] == "a.mp4"
        assert out["duration_sec"] == 100.0

    def test_the_input_is_not_mutated(self):
        before = [dict(iv) for iv in DATA["keep_intervals"]]
        slice_intervals_data(DATA, 0.0, 30.0)
        assert DATA["keep_intervals"] == before


class TestKeepIntervals:
    def test_an_interval_straddling_the_boundary_is_split(self):
        out = slice_intervals_data(DATA, 0.0, 30.0)
        assert out["keep_intervals"] == [
            {"start": 0.0, "end": 10.0},
            {"start": 20.0, "end": 30.0},
        ]

    def test_the_other_side_of_the_boundary_gets_the_rest(self):
        out = slice_intervals_data(DATA, 30.0, 100.0)
        assert out["keep_intervals"] == [
            {"start": 30.0, "end": 40.0},
            {"start": 60.0, "end": 70.0},
        ]

    def test_the_two_sides_lose_nothing_between_them(self):
        left = slice_intervals_data(DATA, 0.0, 30.0)["keep_intervals"]
        right = slice_intervals_data(DATA, 30.0, 100.0)["keep_intervals"]
        kept = sum(iv["end"] - iv["start"] for iv in left + right)
        assert kept == sum(iv["end"] - iv["start"] for iv in DATA["keep_intervals"])

    def test_an_interval_wholly_outside_is_dropped(self):
        out = slice_intervals_data(DATA, 45.0, 55.0)
        assert out["keep_intervals"] == []

    def test_a_touching_interval_is_not_a_zero_length_sliver(self):
        # [0,10] ends exactly where the window starts: it contributes nothing.
        out = slice_intervals_data(DATA, 10.0, 20.0)
        assert out["keep_intervals"] == []

    def test_other_keys_on_an_interval_survive_the_split(self):
        data = {"keep_intervals": [{"start": 0.0, "end": 10.0, "speed_factor": 2.0}]}
        out = slice_intervals_data(data, 0.0, 5.0)
        assert out["keep_intervals"] == [{"start": 0.0, "end": 5.0, "speed_factor": 2.0}]


class TestSpeedRanges:
    def test_a_speed_range_straddling_the_boundary_is_split(self):
        assert slice_intervals_data(DATA, 0.0, 30.0)["speed_ranges"] == [
            {"start": 20.0, "end": 30.0, "factor": 4.0}
        ]
        assert slice_intervals_data(DATA, 30.0, 100.0)["speed_ranges"] == [
            {"start": 30.0, "end": 70.0, "factor": 4.0}
        ]


class TestCaptionsAndOverlays:
    def test_a_caption_belongs_to_the_window_holding_its_start(self):
        assert [c["text"] for c in slice_intervals_data(DATA, 0.0, 30.0)["captions"]] == [
            "one",
            "two",
        ]
        assert [c["text"] for c in slice_intervals_data(DATA, 30.0, 100.0)["captions"]] == ["three"]

    def test_a_caption_straddling_the_boundary_is_placed_once(self):
        # By overlap it would render in both segments; place_captions already
        # clamps it against whatever timeline map it is given.
        data = {"captions": [{"start": 28.0, "end": 35.0, "text": "straddle"}]}
        assert len(slice_intervals_data(data, 0.0, 30.0)["captions"]) == 1
        assert slice_intervals_data(data, 30.0, 100.0)["captions"] == []

    def test_an_anchor_exactly_on_the_boundary_belongs_to_the_later_window(self):
        # The windows are half-open [start, end), so a boundary anchor lands in
        # exactly one of them rather than in both.
        data = {"captions": [{"start": 30.0, "end": 32.0, "text": "edge"}]}
        assert slice_intervals_data(data, 0.0, 30.0)["captions"] == []
        assert len(slice_intervals_data(data, 30.0, 100.0)["captions"]) == 1

    def test_an_overlay_belongs_to_the_window_holding_its_anchor(self):
        assert [o["text"] for o in slice_intervals_data(DATA, 0.0, 30.0)["overlays"]] == ["first"]
        assert [o["text"] for o in slice_intervals_data(DATA, 30.0, 100.0)["overlays"]] == ["last"]


class TestOrderedSources:
    def test_entries_come_back_in_playback_order_sliced(self):
        # A real reorder: the tail of "a" plays before its own head.
        entries = [
            TimelineSegment("a", 30.0, 100.0, lines=(5, 9)),
            TimelineSegment("b", 0.0, 50.0),
            TimelineSegment("a", 0.0, 30.0, lines=(1, 4)),
        ]
        other = {"duration_sec": 50.0, "keep_intervals": [{"start": 0.0, "end": 50.0}]}
        out = ordered_sources(entries, {"a": DATA, "b": other})
        assert [stem for stem, _ in out] == ["a", "b", "a"]
        assert out[0][1]["keep_intervals"][0]["start"] == 30.0
        assert out[2][1]["keep_intervals"][0]["start"] == 0.0

    def test_a_source_with_no_data_is_skipped(self):
        entries = [TimelineSegment("a", 0.0, 100.0), TimelineSegment("gone", 0.0, 1.0)]
        out = ordered_sources(entries, {"a": DATA})
        assert [stem for stem, _ in out] == ["a"]


class TestPlacementOrder:
    def test_no_manifest_is_shooting_order_covering_each_whole_source(self):
        from nagare_clip.blender.frames import placement_order

        out = placement_order(["a", "b"], [])
        assert [e.stem for e in out] == ["a", "b"]
        assert all(e.start == 0.0 and e.lines is None for e in out)
        # A whole-source window must not clip anything off the tail.
        assert all(e.end == float("inf") for e in out)

    def test_a_manifest_is_restricted_to_the_known_sources(self):
        from nagare_clip.blender.frames import placement_order

        entries = [
            TimelineSegment("a", 30.0, 100.0, lines=(5, 9)),
            TimelineSegment("b", 0.0, 50.0),
            TimelineSegment("a", 0.0, 30.0, lines=(1, 4)),
        ]
        # --source a: only a's segments are built, each keeping its position.
        assert [e.lines for e in placement_order(["a"], entries)] == [(5, 9), (1, 4)]

    def test_playback_order_is_the_manifests_not_the_sources(self):
        from nagare_clip.blender.frames import placement_order

        entries = [TimelineSegment("b", 0.0, 1.0), TimelineSegment("a", 0.0, 1.0)]
        assert [e.stem for e in placement_order(["a", "b"], entries)] == ["b", "a"]
