"""Tests for the plan stage (rough directions per part, pure, no network)."""

from __future__ import annotations

import yaml as _yaml

from nagare_clip.llm_report import Recorder
from nagare_clip.plan.plan_llm import (
    ParsedPlan,
    PartDirection,
    format_parts_for_plan,
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
            _project().parts,
        )
        assert out.directions == [
            PartDirection("a", (1, 4), "keep"),
            PartDirection("b", (1, 3), "remove"),
        ]

    def test_out_of_range_index_dropped(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 9, "direction": "x"}, {"index": 1, "direction": "ok"}]}',
            _project().parts,
        )
        assert out.directions == [PartDirection("a", (1, 4), "ok")]

    def test_empty_direction_dropped(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "direction": ""}]}', _project().parts
        )
        assert out.directions == []

    def test_hard_failure_returns_none(self):
        assert try_parse_plan_response("not json", _project().parts) is None
        assert try_parse_plan_response('{"foo": 1}', _project().parts) is None

    def test_valid_empty_returns_no_directions(self):
        assert try_parse_plan_response('{"directions": []}', _project().parts).directions == []


class TestParseSplit:
    def test_own_lines_narrow_the_direction(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 2, "lines": [7, 9], "direction": "demo body"}]}',
            _project().parts,
        )
        assert out.directions == [PartDirection("a", (7, 9), "demo body")]

    def test_one_part_splits_into_several_directions(self):
        out = try_parse_plan_response(
            '{"directions": ['
            '{"index": 2, "lines": [7, 9], "direction": "demo body"},'
            '{"index": 2, "lines": [5, 6], "direction": "digression — cut"}'
            "]}",
            _project().parts,
        )
        assert out.directions == [
            PartDirection("a", (5, 6), "digression — cut"),
            PartDirection("a", (7, 9), "demo body"),
        ]

    def test_lines_outside_the_part_dropped(self):
        drops: list[str] = []
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "lines": [3, 9], "direction": "x"}]}',
            _project().parts,
            drops,
        )
        assert out.directions == []
        assert drops and "range" in drops[0]

    def test_malformed_lines_dropped(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "lines": [4, 1], "direction": "x"},'
            ' {"index": 3, "lines": "nope", "direction": "y"}]}',
            _project().parts,
        )
        assert out.directions == []

    def test_the_message_is_read(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "direction": "x"}], '
            '"message": "the build is the throughline"}',
            _project().parts,
        )
        assert out.directions == [PartDirection("a", (1, 4), "x")]
        assert out.message == "the build is the throughline"

    def test_a_missing_message_keeps_the_directions(self):
        """An unexplained plan is a defect, not a parse failure: retrying would
        re-roll every direction to recover one paragraph."""
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "direction": "x"}]}', _project().parts
        )
        assert out.directions == [PartDirection("a", (1, 4), "x")]
        assert out.message == ""

    def test_a_non_string_message_is_empty(self):
        out = try_parse_plan_response('{"directions": [], "message": 7}', _project().parts)
        assert out.message == ""

    def test_same_range_twice_keeps_the_last(self):
        out = try_parse_plan_response(
            '{"directions": [{"index": 1, "direction": "a"}, {"index": 1, "direction": "b"}]}',
            _project().parts,
        )
        assert out.directions == [PartDirection("a", (1, 4), "b")]


class TestGeneratePlan:
    def test_assembles_part_directions(self):
        resp = (
            '{"directions": ['
            '{"index": 1, "direction": "keep"},'
            '{"index": 3, "direction": "remove"}'
            "]}"
        )
        out = generate_plan(_project(), {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert out.directions == [
            PartDirection("a", (1, 4), "keep"),
            PartDirection("b", (1, 3), "remove"),
        ]

    def test_empty_parts_no_call(self):
        called = {"n": 0}

        def fake(m, c):
            called["n"] += 1
            return '{"directions": []}'

        out = generate_plan(ProjectSummary("", []), {"prompt": "P"}, call_llm=fake)
        assert out.directions == []
        assert called["n"] == 0

    def test_llm_failure_returns_empty(self):
        def boom(m, c):
            raise ConnectionError("x")

        out = generate_plan(_project(), {"prompt": "P"}, call_llm=boom)
        assert out.directions == []

    def test_retries_then_succeeds(self):
        fake = _seq_llm(["junk", '{"directions": [{"index": 1, "direction": "keep"}]}'])
        out = generate_plan(_project(), {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert fake.calls["i"] == 2
        assert out.directions == [PartDirection("a", (1, 4), "keep")]

    def test_generate_returns_a_parsed_plan(self):
        out = generate_plan(
            _project(), {"prompt": "P"}, call_llm=lambda m, c: '{"directions": [], "message": "m"}'
        )
        assert isinstance(out, ParsedPlan)

    def test_returns_the_account_of_the_plan(self):
        resp = (
            '{"directions": [{"index": 1, "direction": "keep"}],'
            ' "message": "the build is the throughline; the sign-off is filler"}'
        )
        out = generate_plan(_project(), {"prompt": "P"}, call_llm=lambda m, c: resp)
        assert out.message == "the build is the throughline; the sign-off is filler"

    def test_a_missing_message_is_not_retried(self):
        """One call, kept: the plan is usable, only its explanation is missing."""
        fake = _seq_llm(['{"directions": [{"index": 1, "direction": "keep"}]}', "unused"])
        out = generate_plan(_project(), {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert fake.calls["i"] == 1
        assert out.directions and out.message == ""


class TestGeneratePlanIsPure:
    """plan reads the summaries and nothing else: the conversation moved to
    plan_revise, so a plan run is reproducible from summary.json alone."""

    def test_user_content_is_exactly_the_parts_document(self):
        seen: dict = {}

        def fake(messages, _cfg):
            seen["user"] = messages[-1]["content"]
            return '{"directions": []}'

        generate_plan(_project(), {"prompt": "P"}, call_llm=fake)
        assert seen["user"] == format_parts_for_plan(_project())

    def test_takes_no_conversation_arguments(self):
        import inspect

        params = inspect.signature(generate_plan).parameters
        assert "previous" not in params and "history" not in params


class TestFormatExistingDirections:
    """The same parts document, with the current directions rendered under the
    part each belongs to — what plan_revise shows the model."""

    def test_renders_under_its_part_with_its_id(self):
        ps = _project()
        ds = [PartDirection("a", (1, 4), "feature")]
        out = format_parts_for_plan(ps, ds, ["k7f2"])
        lines = out.splitlines()
        i = next(n for n, ln in enumerate(lines) if ln.startswith("1: a [1-4]"))
        assert "feature" in lines[i + 1]
        assert "k7f2" in lines[i + 1]
        assert lines[i + 1].startswith(" ")

    def test_split_directions_both_render(self):
        ps = _project()
        ds = [PartDirection("a", (5, 6), "cut"), PartDirection("a", (7, 9), "feature")]
        out = format_parts_for_plan(ps, ds, ["aaaa", "bbbb"])
        assert "5-6" in out and "7-9" in out
        assert "aaaa" in out and "bbbb" in out

    def test_direction_matching_no_part_is_dropped(self):
        ps = _project()
        out = format_parts_for_plan(ps, [PartDirection("zzz", (1, 2), "orphan")], ["aaaa"])
        assert "orphan" not in out

    def test_no_directions_is_byte_identical(self):
        ps = _project()
        assert format_parts_for_plan(ps, [], []) == format_parts_for_plan(ps)


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


def _ps():
    return ProjectSummary(
        summary="overall",
        parts=[
            PartSummary(stem="v", lines=(1, 2), summary="p1"),
            PartSummary(stem="v", lines=(3, 4), summary="p2"),
        ],
    )


def _outcome(tmp_path, unit):
    text = (tmp_path / "plan" / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)["outcome"]


class TestFormatPartsTiming:
    def test_same_stem_gap_and_last_part_no_gap(self):
        ps = ProjectSummary(
            summary="",
            parts=[
                PartSummary("v", (1, 2), "intro", start=0.0, end=10.0),
                PartSummary("v", (3, 4), "demo", start=11.5, end=19.5),
            ],
        )
        out = format_parts_for_plan(ps)
        assert "1: v [1-2] [10.0s, gap 1.5s] — intro" in out
        assert "2: v [3-4] [8.0s] — demo" in out

    def test_cross_video_boundary_has_no_gap(self):
        ps = ProjectSummary(
            summary="",
            parts=[
                PartSummary("a", (1, 2), "x", start=0.0, end=10.0),
                PartSummary("b", (1, 2), "y", start=2.0, end=8.0),
            ],
        )
        out = format_parts_for_plan(ps)
        # part 1 is last of stem "a" -> dur only, no gap into "b"
        assert "1: a [1-2] [10.0s] — x" in out
        assert "2: b [1-2] [6.0s] — y" in out

    def test_missing_times_no_bracket(self):
        ps = ProjectSummary(
            summary="",
            parts=[
                PartSummary("v", (1, 2), "intro"),
            ],
        )
        out = format_parts_for_plan(ps)
        assert out == "1: v [1-2] — intro"


def test_format_inserts_video_summary_header():
    ps = ProjectSummary(
        summary="overall",
        parts=[
            PartSummary(stem="A", lines=(1, 2), summary="a1"),
            PartSummary(stem="B", lines=(1, 3), summary="b1"),
        ],
        video_summaries={"A": "video A overview", "B": "video B overview"},
    )
    text = format_parts_for_plan(ps)
    assert 'Video "A": video A overview' in text
    assert 'Video "B": video B overview' in text
    # header appears before that video's part line
    assert text.index('Video "A"') < text.index("1: A [1-2]")


def test_format_byte_identical_when_no_video_summaries():
    parts = [
        PartSummary(stem="A", lines=(1, 2), summary="a1"),
        PartSummary(stem="B", lines=(1, 3), summary="b1"),
    ]
    without = format_parts_for_plan(ProjectSummary(summary="overall", parts=parts))
    # Expected == the exact current format (no Video: lines).
    assert 'Video "' not in without


def test_format_parts_renders_speech_silence_bracket():
    from nagare_clip.plan.plan_llm import format_parts_for_plan
    from nagare_clip.summary.summarize import PartSummary, ProjectSummary

    ps = ProjectSummary(
        summary="",
        parts=[
            PartSummary(
                stem="v",
                lines=(1, 5),
                summary="long part",
                start=0.0,
                end=75.8,
                silence=62.9,
            )
        ],
    )
    out = format_parts_for_plan(ps)
    assert "[12.9s speech, 62.9s silence]" in out


def test_format_parts_without_silence_unchanged():
    from nagare_clip.plan.plan_llm import format_parts_for_plan
    from nagare_clip.summary.summarize import PartSummary, ProjectSummary

    ps = ProjectSummary(
        summary="",
        parts=[PartSummary(stem="v", lines=(1, 5), summary="p", start=0.0, end=4.2)],
    )
    assert "[4.2s]" in format_parts_for_plan(ps)


class TestPlanRecorder:
    def test_records_ok(self, tmp_path):
        rec = Recorder("plan", tmp_path, enabled=True)
        resp = '{"directions":[{"index":1,"direction":"keep"},{"index":2,"direction":"cut"}]}'

        def fake(_m, _c):
            return resp

        out = generate_plan(_ps(), {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert len(out.directions) == 2
        assert _outcome(tmp_path, "plan") == "ok"

    def test_records_dropped_items(self, tmp_path):
        rec = Recorder("plan", tmp_path, enabled=True)
        resp = '{"directions":[{"index":1,"direction":"keep"},{"index":9,"direction":"x"}]}'

        def fake(_m, _c):
            return resp

        out = generate_plan(_ps(), {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert len(out.directions) == 1
        assert _outcome(tmp_path, "plan") == "dropped-items"
