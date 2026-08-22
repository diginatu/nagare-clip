"""plan_revise: a revision expressed as operations against the current plan."""

from __future__ import annotations

from nagare_clip.llm_report import Recorder
from nagare_clip.plan.dialogue import DialogueTurn
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.plan_revise.ids import assign_ids
from nagare_clip.plan_revise.revise_llm import (
    apply_revision,
    build_user_content,
    generate_revision,
    try_parse_revision,
)
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


def _project():
    return ProjectSummary(
        summary="overall",
        parts=[
            PartSummary("a", (1, 4), "intro"),
            PartSummary("a", (5, 9), "demo"),
            PartSummary("b", (1, 3), "wrap"),
        ],
    )


def _plan():
    return [
        PartDirection("a", (1, 4), "feature — sets up the build"),
        PartDirection("a", (5, 9), "feature — the payoff"),
        PartDirection("b", (1, 3), "remove — repeats part 1"),
    ]


def _id(directions, n):
    return assign_ids(directions)[n]


class TestParse:
    def test_reads_every_operation(self):
        ds, parts = _plan(), _project().parts
        resp = (
            f'{{"delete": ["{_id(ds, 2)}"],'
            ' "add": [{"index": 2, "lines": [5, 6], "direction": "remove — digression"}],'
            f' "update": [{{"id": "{_id(ds, 0)}", "direction": "shorten heavily"}}],'
            ' "message": "split it"}'
        )
        ops = try_parse_revision(resp, parts)
        assert ops is not None
        assert ops.delete == [_id(ds, 2)]
        assert ops.update == [(_id(ds, 0), "shorten heavily")]
        assert ops.add == [PartDirection("a", (5, 6), "remove — digression")]
        assert ops.message == "split it"

    def test_add_without_lines_covers_the_whole_part(self):
        ops = try_parse_revision(
            '{"add": [{"index": 3, "direction": "feature"}]}', _project().parts
        )
        assert ops.add == [PartDirection("b", (1, 3), "feature")]

    def test_message_only_is_valid(self):
        # The human asked a question; answering without editing is a real reply.
        ops = try_parse_revision('{"message": "which lines?"}', _project().parts)
        assert ops is not None
        assert ops.message == "which lines?"
        assert not ops.delete and not ops.add and not ops.update

    def test_hard_failures_return_none(self):
        assert try_parse_revision("not json", _project().parts) is None
        assert try_parse_revision("[1, 2]", _project().parts) is None
        assert try_parse_revision('{"foo": 1}', _project().parts) is None

    def test_fenced_json_is_read(self):
        ops = try_parse_revision('```json\n{"message": "hi"}\n```', _project().parts)
        assert ops is not None and ops.message == "hi"

    def test_malformed_entries_are_dropped_not_fatal(self):
        drops: list[str] = []
        resp = (
            '{"delete": ["", 7],'
            ' "add": [{"index": 9, "direction": "x"},'
            '         {"index": 1, "lines": [3, 9], "direction": "y"},'
            '         {"index": 1, "direction": ""}],'
            ' "update": [{"id": "abcd"}, {"direction": "z"}],'
            ' "message": "ok"}'
        )
        ops = try_parse_revision(resp, _project().parts, drops)
        assert ops is not None
        assert not ops.delete and not ops.add and not ops.update
        assert len(drops) == 7

    def test_non_list_operation_keys_are_ignored(self):
        ops = try_parse_revision('{"delete": "k7f2", "message": "x"}', _project().parts)
        assert ops is not None and ops.delete == []


class TestApply:
    def test_unnamed_directions_are_carried_through_by_the_code(self):
        ds = _plan()
        ops = try_parse_revision('{"message": "nothing to change"}', _project().parts)
        assert apply_revision(ds, ops, _project().parts) == ds

    def test_delete_removes_only_the_named_direction(self):
        ds = _plan()
        ops = try_parse_revision(f'{{"delete": ["{_id(ds, 1)}"]}}', _project().parts)
        assert apply_revision(ds, ops, _project().parts) == [ds[0], ds[2]]

    def test_update_replaces_the_text_and_keeps_the_range(self):
        ds = _plan()
        ops = try_parse_revision(
            f'{{"update": [{{"id": "{_id(ds, 1)}", "direction": "shorten heavily"}}]}}',
            _project().parts,
        )
        out = apply_revision(ds, ops, _project().parts)
        assert out[1] == PartDirection("a", (5, 9), "shorten heavily")
        assert out[0] == ds[0] and out[2] == ds[2]

    def test_add_lands_in_part_and_line_order(self):
        ds = _plan()
        ops = try_parse_revision(
            '{"add": [{"index": 2, "lines": [5, 6], "direction": "cut"}]}', _project().parts
        )
        out = apply_revision(ds, ops, _project().parts)
        assert [(d.stem, d.lines) for d in out] == [
            ("a", (1, 4)),
            ("a", (5, 6)),
            ("a", (5, 9)),
            ("b", (1, 3)),
        ]

    def test_a_split_is_delete_plus_two_adds(self):
        ds = _plan()
        ops = try_parse_revision(
            f'{{"delete": ["{_id(ds, 1)}"], "add": ['
            '{"index": 2, "lines": [5, 6], "direction": "remove — digression"},'
            '{"index": 2, "lines": [7, 9], "direction": "feature — the demo itself"}]}',
            _project().parts,
        )
        out = apply_revision(ds, ops, _project().parts)
        assert out == [
            ds[0],
            PartDirection("a", (5, 6), "remove — digression"),
            PartDirection("a", (7, 9), "feature — the demo itself"),
            ds[2],
        ]

    def test_add_over_an_existing_range_replaces_it(self):
        ds = _plan()
        ops = try_parse_revision(
            '{"add": [{"index": 1, "lines": [1, 4], "direction": "new text"}]}', _project().parts
        )
        out = apply_revision(ds, ops, _project().parts)
        assert len(out) == 3 and out[0].direction == "new text"

    def test_unknown_id_is_dropped_and_logged(self):
        ds = _plan()
        drops: list[str] = []
        ops = try_parse_revision(
            '{"delete": ["zzzz"], "update": [{"id": "yyyy", "direction": "x"}]}', _project().parts
        )
        assert apply_revision(ds, ops, _project().parts, drops) == ds
        assert len(drops) == 2 and all("zzzz" in d or "yyyy" in d for d in drops)

    def test_a_direction_over_footage_no_part_covers_survives(self):
        # plan.json is hand-editable; an entry the summary no longer covers must
        # not silently vanish through a revision that never named it.
        ds = _plan() + [PartDirection("gone", (1, 2), "stale")]
        ops = try_parse_revision('{"message": "x"}', _project().parts)
        assert apply_revision(ds, ops, _project().parts)[-1] == ds[-1]


class TestUserContent:
    def test_shows_the_ids_the_parts_and_the_conversation(self):
        ds = _plan()
        content = build_user_content(
            _project(), ds, assign_ids(ds), [DialogueTurn("human", "part 2 is mixed")]
        )
        for i in assign_ids(ds):
            assert f"[{i}]" in content
        assert "feature — the payoff" in content
        assert "part 2 is mixed" in content
        assert "2: a [5-9]" in content


class TestGenerate:
    def test_one_call_returns_the_merged_plan(self):
        ds = _plan()
        calls: list[list[dict]] = []

        def fake(messages, _cfg):
            calls.append(messages)
            return f'{{"delete": ["{_id(ds, 2)}"], "message": "dropped the wrap"}}'

        out = generate_revision(
            _project(),
            ds,
            [DialogueTurn("human", "drop the wrap")],
            {"prompt": "P"},
            call_llm=fake,
        )
        assert len(calls) == 1
        assert out.ok and out.directions == [ds[0], ds[1]]
        assert out.message == "dropped the wrap"

    def test_retries_a_hard_failure_then_succeeds(self):
        ds = _plan()
        responses = ["junk", '{"message": "ok"}']
        out = generate_revision(
            _project(),
            ds,
            [DialogueTurn("human", "x")],
            {"prompt": "P", "max_retries": 2},
            call_llm=lambda m, c: responses.pop(0),
        )
        assert out.ok and out.message == "ok" and out.directions == ds
        assert not responses

    def test_total_failure_keeps_the_plan_and_reports_not_ok(self):
        ds = _plan()

        def boom(m, c):
            raise ConnectionError("x")

        out = generate_revision(
            _project(), ds, [DialogueTurn("human", "x")], {"prompt": "P"}, call_llm=boom
        )
        # nothing is written and no reply is appended, so the next run retries
        assert not out.ok and out.directions == ds and out.message == ""

    def test_applied_summarises_the_operations(self):
        ds = _plan()
        out = generate_revision(
            _project(),
            ds,
            [DialogueTurn("human", "x")],
            {"prompt": "P"},
            call_llm=lambda m, c: (
                f'{{"delete": ["{_id(ds, 2)}"], "add": [{{"index": 1, "lines": [1, 2],'
                ' "direction": "z"}]}'
            ),
        )
        assert "1 deleted" in out.applied and "1 added" in out.applied

    def test_records_dropped_items(self, tmp_path):
        rec = Recorder("plan_revise", tmp_path, enabled=True)
        ds = _plan()
        generate_revision(
            _project(),
            ds,
            [DialogueTurn("human", "x")],
            {"max_retries": 0},
            call_llm=lambda m, c: '{"delete": ["zzzz"], "message": "hm"}',
            recorder=rec,
        )
        import yaml

        text = (tmp_path / "plan_revise" / "plan_revise.md").read_text(encoding="utf-8")
        _, fm, _ = text.split("---", 2)
        assert yaml.safe_load(fm)["outcome"] == "dropped-items"
