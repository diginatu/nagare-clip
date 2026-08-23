"""The director's context block, when the timeline is made of segments.

Improvements 19 and 22 both become well defined here: "video 3 of 7" breaks the
moment one source plays at two places, and "what plays before this" is the
previous SEGMENT, which may be another segment of this very source.
"""

from __future__ import annotations

from nagare_clip.director.context import Seam, build_director_context
from nagare_clip.order import Segment
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

PROJECT = ProjectSummary(
    summary="全体の要約",
    parts=[
        PartSummary("mix", (1, 30), "餌やり"),
        PartSummary("mix", (31, 83), "装置の実演"),
        PartSummary("mix", (84, 97), "魚"),
        PartSummary("dev", (1, 40), "装置づくり"),
    ],
    video_summaries={"mix": "混ざった回", "dev": "装置の回"},
)

# dev plays whole; mix is split, with its demo pulled forward.
ORDER = [
    Segment("dev", None),
    Segment("mix", (31, 83)),
    Segment("mix", (1, 30)),
    Segment("mix", (84, 97)),
]


def _ctx(segment, **kw):
    return build_director_context(PROJECT, [], segment, all_segments=ORDER, **kw)


class TestPosition:
    def test_the_position_counts_segments_not_videos(self):
        assert "segment 2 of 4" in _ctx(ORDER[1])

    def test_the_first_segment_is_named_as_such(self):
        assert "FIRST" in _ctx(ORDER[0])

    def test_the_last_segment_is_named_as_such(self):
        assert "LAST" in _ctx(ORDER[3])

    def test_a_partial_segment_states_its_line_range(self):
        assert 'This segment ("mix [31-83]")' in _ctx(ORDER[1])

    def test_a_whole_source_segment_is_named_by_its_stem_alone(self):
        assert 'This segment ("dev")' in _ctx(ORDER[0])


class TestOwnParts:
    def test_only_the_parts_this_segment_covers_are_listed(self):
        out = _ctx(ORDER[1])
        assert "装置の実演" in out
        assert "餌やり" not in out.split("Earlier in the finished video")[0]

    def test_a_part_is_clipped_to_the_segment(self):
        # A segment covering half a part must not invite an op on the other half.
        out = build_director_context(PROJECT, [], Segment("mix", (31, 50)), all_segments=ORDER)
        assert "lines 31-50" in out

    def test_a_direction_outside_the_segment_is_not_shown(self):
        directions = [
            PartDirection("mix", (1, 30), "remove — 前置き"),
            PartDirection("mix", (31, 83), "feature — 実演"),
        ]
        out = build_director_context(PROJECT, directions, ORDER[1], all_segments=ORDER)
        assert "feature — 実演" in out
        assert "remove — 前置き" not in out


class TestSiblings:
    def test_earlier_and_later_are_segments(self):
        out = _ctx(ORDER[2])
        earlier = out.split("Earlier in the finished video")[1].split("Later in")[0]
        later = out.split("Later in the finished video:")[1]
        assert "1. dev" in earlier
        assert "2. mix [31-83]" in earlier
        assert "4. mix [84-97]" in later

    def test_another_segment_of_the_same_source_is_a_sibling(self):
        # The source is no longer the unit: its other stretches are neighbours
        # like any other footage.
        out = _ctx(ORDER[1])
        assert "3. mix [1-30]" in out

    def test_a_partial_sibling_is_described_by_the_parts_it_covers(self):
        assert "装置の実演" in _ctx(ORDER[2])

    def test_a_whole_source_sibling_keeps_its_video_summary(self):
        assert "装置の回" in _ctx(ORDER[1])


class TestSeams:
    def test_the_seam_names_the_neighbouring_segment(self):
        out = build_director_context(
            PROJECT,
            [],
            ORDER[2],
            all_segments=ORDER,
            seam_before=Seam("mix [31-83]", ["終わりです"]),
            seam_after=Seam("mix [84-97]", ["さて魚です"]),
        )
        assert "mix [31-83], its last lines" in out
        assert "mix [84-97], its first lines" in out
        assert "終わりです" in out
        assert "さて魚です" in out


class TestSingleSegmentProject:
    def test_one_segment_has_no_position_to_explain(self):
        out = build_director_context(
            PROJECT, [], Segment("dev", None), all_segments=[Segment("dev", None)]
        )
        assert "segment 1 of 1" not in out
