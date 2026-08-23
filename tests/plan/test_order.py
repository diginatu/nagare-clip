"""plan emits the finished video's playback order.

The identity path is "no order key at all", which is indistinguishable from a
working order unless a test writes a real one — so most of these write a real
reorder.
"""

from __future__ import annotations

import json

from nagare_clip.order import Segment
from nagare_clip.plan.plan_llm import (
    generate_plan,
    order_from_dict,
    plan_to_dict,
    try_parse_plan_response,
)
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

PARTS = [
    PartSummary("mix", (1, 30), "餌やり"),
    PartSummary("mix", (31, 83), "装置の実演"),
    PartSummary("mix", (84, 97), "魚"),
    PartSummary("dev", (1, 40), "装置づくり"),
]
COUNTS = {"mix": 97, "dev": 40}
REORDER = [
    {"stem": "dev", "lines": [1, 40]},
    {"stem": "mix", "lines": [31, 83]},
    {"stem": "mix", "lines": [1, 30]},
    {"stem": "mix", "lines": [84, 97]},
]


def _response(**extra):
    return json.dumps({"directions": [], "message": "説明", **extra})


class TestParsingTheOrder:
    def test_a_reorder_is_parsed(self):
        parsed = try_parse_plan_response(_response(order=REORDER), PARTS, line_counts=COUNTS)
        assert parsed.order == [
            Segment("dev", None),
            Segment("mix", (31, 83)),
            Segment("mix", (1, 30)),
            Segment("mix", (84, 97)),
        ]

    def test_a_full_range_is_normalised_to_the_whole_source(self):
        # dev [1,40] is the whole source; it must be indistinguishable from a
        # segment written without lines at all.
        parsed = try_parse_plan_response(_response(order=REORDER), PARTS, line_counts=COUNTS)
        assert parsed.order[0] == Segment("dev", None)

    def test_no_order_key_is_no_order(self):
        assert try_parse_plan_response(_response(), PARTS, line_counts=COUNTS).order == []

    def test_an_order_missing_a_line_is_dropped_whole(self, caplog):
        bad = [{"stem": "dev"}, {"stem": "mix", "lines": [1, 30]}]
        drops = []
        parsed = try_parse_plan_response(_response(order=bad), PARTS, drops, line_counts=COUNTS)
        assert parsed.order == []
        assert drops  # recorded in the LLM report, not silently swallowed

    def test_an_order_missing_a_source_is_dropped_whole(self):
        bad = [{"stem": "mix", "lines": [1, 97]}]
        assert try_parse_plan_response(_response(order=bad), PARTS, line_counts=COUNTS).order == []

    def test_an_overlapping_order_is_dropped_whole(self):
        bad = [
            {"stem": "dev"},
            {"stem": "mix", "lines": [1, 50]},
            {"stem": "mix", "lines": [40, 97]},
        ]
        assert try_parse_plan_response(_response(order=bad), PARTS, line_counts=COUNTS).order == []

    def test_a_valid_order_survives_alongside_a_dropped_direction(self):
        response = json.dumps(
            {
                "directions": [{"index": 99, "direction": "out of range"}],
                "order": REORDER,
                "message": "m",
            }
        )
        parsed = try_parse_plan_response(response, PARTS, [], line_counts=COUNTS)
        assert parsed.directions == []
        assert len(parsed.order) == 4

    def test_without_line_counts_no_order_can_be_validated(self):
        # Never write an unvalidated contract into the artifact.
        assert try_parse_plan_response(_response(order=REORDER), PARTS).order == []

    def test_the_order_is_not_a_hard_parse_failure(self):
        # A bad order must not re-roll every direction.
        response = json.dumps(
            {"directions": [{"index": 1, "direction": "remove"}], "order": [{"stem": "zzz"}]}
        )
        parsed = try_parse_plan_response(response, PARTS, line_counts=COUNTS)
        assert parsed is not None
        assert len(parsed.directions) == 1


class TestWritingTheOrder:
    def test_plan_to_dict_carries_the_order(self):
        data = plan_to_dict([], [Segment("dev", None), Segment("mix", (31, 83))])
        assert data["order"] == [{"stem": "dev"}, {"stem": "mix", "lines": [31, 83]}]

    def test_no_order_writes_no_key(self):
        # Every project predating the feature reads back as shooting order.
        assert "order" not in plan_to_dict([], [])

    def test_round_trip(self):
        order = [Segment("dev", None), Segment("mix", (31, 83))]
        assert order_from_dict(plan_to_dict([], order)) == order


class TestGeneratePlan:
    def test_the_order_reaches_the_result(self):
        result = generate_plan(
            ProjectSummary("全体", PARTS),
            {"prompt": "P", "max_retries": 0},
            call_llm=lambda m, c: _response(order=REORDER),
            line_counts=COUNTS,
        )
        assert [s.stem for s in result.order] == ["dev", "mix", "mix", "mix"]

    def test_a_failed_call_has_no_order(self):
        def boom(messages, cfg):
            raise RuntimeError("nope")

        result = generate_plan(
            ProjectSummary("全体", PARTS),
            {"prompt": "P", "max_retries": 0},
            call_llm=boom,
            line_counts=COUNTS,
        )
        assert result.order == []
