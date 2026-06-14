"""Tests for the plan stage (rough directions per part, pure, no network)."""

from __future__ import annotations

from nagare_clip.plan.plan_llm import (
    PartDirection,
    generate_plan,
    plan_from_dict,
    plan_to_dict,
    try_parse_plan_response,
)
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


def _seq_llm(items):
    box = {"i": 0}

    def fake(_messages, _cfg):
        item = items[box["i"]]
        box["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    fake.calls = box
    return fake


def _project():
    return ProjectSummary(
        summary="overall",
        parts=[
            PartSummary("a", (1, 4), "intro"),
            PartSummary("a", (5, 9), "demo"),
            PartSummary("b", (1, 3), "wrap"),
        ],
    )


class TestParse:
    def test_maps_by_index(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "direction": "keep"},'
            ' {"index": 3, "direction": "remove"}]}',
            num_parts=3,
        )
        assert out == {1: "keep", 3: "remove"}

    def test_out_of_range_index_dropped(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 9, "direction": "x"},'
            ' {"index": 1, "direction": "ok"}]}',
            num_parts=3,
        )
        assert out == {1: "ok"}

    def test_empty_direction_dropped(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "direction": ""}]}', num_parts=3
        )
        assert out == {}

    def test_hard_failure_returns_none(self):
        assert try_parse_plan_response("not json", num_parts=3) is None
        assert try_parse_plan_response('{"foo": 1}', num_parts=3) is None

    def test_valid_empty_returns_empty_dict(self):
        assert try_parse_plan_response('{"directions": []}', num_parts=3) == {}


class TestGeneratePlan:
    def test_assembles_part_directions(self):
        resp = (
            '{"directions": ['
            '{"index": 1, "direction": "keep"},'
            '{"index": 3, "direction": "remove"}'
            "]}"
        )
        out = generate_plan(_project(), {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert out == [
            PartDirection("a", (1, 4), "keep"),
            PartDirection("b", (1, 3), "remove"),
        ]

    def test_empty_parts_no_call(self):
        called = {"n": 0}

        def fake(m, c):
            called["n"] += 1
            return '{"directions": []}'

        assert generate_plan(ProjectSummary("", []), {"prompt": "P"}, call_llm=fake) == []
        assert called["n"] == 0

    def test_llm_failure_returns_empty(self):
        def boom(m, c):
            raise ConnectionError("x")

        assert generate_plan(_project(), {"prompt": "P"}, call_llm=boom) == []

    def test_retries_then_succeeds(self):
        fake = _seq_llm(
            ["junk", '{"directions": [{"index": 1, "direction": "keep"}]}']
        )
        out = generate_plan(_project(), {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert fake.calls["i"] == 2
        assert out == [PartDirection("a", (1, 4), "keep")]


class TestRoundTrip:
    def test_to_from_dict(self):
        directions = [
            PartDirection("a", (1, 4), "keep"),
            PartDirection("b", (1, 3), "remove"),
        ]
        d = plan_to_dict(directions)
        assert d == {
            "directions": [
                {"stem": "a", "lines": [1, 4], "direction": "keep"},
                {"stem": "b", "lines": [1, 3], "direction": "remove"},
            ]
        }
        assert plan_from_dict(d) == directions

    def test_from_dict_tolerates_garbage(self):
        assert plan_from_dict("nope") == []
        assert plan_from_dict({}) == []
