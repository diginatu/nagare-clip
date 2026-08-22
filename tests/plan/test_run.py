"""plan run: disabled no-op, enabled rough directions, and what a re-run
invalidates (the revised plan, and the conversation turns above it)."""

from __future__ import annotations

import json

import yaml

import nagare_clip.plan.run as plan_run
from nagare_clip.plan.dialogue import (
    DialogueTurn,
    active_turns,
    append_turn,
    has_unanswered_human,
    parse_history,
    read_history,
)
from nagare_clip.plan.plan_llm import ParsedPlan, PartDirection
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict


def _run(monkeypatch, tmp_path, cfg_dict, project_summary, history=None, revised=None):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(summary_to_dict(project_summary)), encoding="utf-8")
    out = tmp_path / "plan.json"
    plan_run.run_plan(
        summary,
        out,
        yaml.safe_load(cfg.read_text(encoding="utf-8")),
        history=history,
        revised=revised,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def test_disabled_writes_empty(monkeypatch, tmp_path):
    ps = ProjectSummary("all", [PartSummary("a", (1, 2), "x")])
    data = _run(monkeypatch, tmp_path, {"plan": {"enabled": False}}, ps)
    assert data == {"directions": []}


def test_enabled_writes_directions(monkeypatch, tmp_path):
    ps = ProjectSummary("all", [PartSummary("a", (1, 2), "x"), PartSummary("b", (1, 1), "y")])

    def fake_generate(project_summary, cfg, **kwargs):
        # confirms the loaded summary round-tripped into the stage
        assert [p.stem for p in project_summary.parts] == ["a", "b"]
        return ParsedPlan(
            [PartDirection("a", (1, 2), "keep"), PartDirection("b", (1, 1), "remove")]
        )

    monkeypatch.setattr(plan_run, "generate_plan", fake_generate)
    data = _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, ps)
    assert data == {
        "directions": [
            {"stem": "a", "lines": [1, 2], "direction": "keep"},
            {"stem": "b", "lines": [1, 1], "direction": "remove"},
        ]
    }


def test_project_brief_appended_to_plan_prompt(monkeypatch, tmp_path):
    ps = ProjectSummary("all", [PartSummary("a", (1, 2), "x")])
    seen: dict = {}

    def fake_generate(project_summary, cfg, **kwargs):
        seen["prompt"] = cfg["prompt"]
        return ParsedPlan([])

    monkeypatch.setattr(plan_run, "generate_plan", fake_generate)
    _run(
        monkeypatch,
        tmp_path,
        {"plan": {"enabled": True, "prompt": "P"}, "project": {"target_duration": "12 minutes"}},
        ps,
    )
    assert seen["prompt"].startswith("P\n\n")
    assert seen["prompt"].endswith("- Target duration: 12 minutes")

    seen.clear()
    _run(monkeypatch, tmp_path, {"plan": {"enabled": True, "prompt": "P"}}, ps)
    assert seen["prompt"] == "P"


class TestPlanIsPure:
    """plan reads summary.json and nothing else — no previous plan, no history."""

    def _ps(self):
        return ProjectSummary("all", [PartSummary("a", (1, 4), "x")])

    def test_previous_plan_is_not_fed_back(self, monkeypatch, tmp_path):
        seen: dict = {}

        def fake_generate(project_summary, cfg, **kwargs):
            seen["kwargs"] = kwargs
            return ParsedPlan([PartDirection("a", (1, 4), "second")])

        monkeypatch.setattr(plan_run, "generate_plan", fake_generate)
        (tmp_path / "plan.json").write_text(
            json.dumps({"directions": [{"stem": "a", "lines": [1, 4], "direction": "first"}]}),
            encoding="utf-8",
        )
        history = tmp_path / "plan_dialogue" / "history.md"
        append_turn(history, "human", "part 1 is mixed")
        data = _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)
        assert "previous" not in seen["kwargs"] and "history" not in seen["kwargs"]
        assert data["directions"][0]["direction"] == "second"


class TestReRunInvalidates:
    def _ps(self):
        return ProjectSummary("all", [PartSummary("a", (1, 4), "x")])

    def _enabled(self, monkeypatch, message=""):
        monkeypatch.setattr(
            plan_run, "generate_plan", lambda ps, cfg, **kw: ParsedPlan([], message)
        )

    def test_divider_retires_the_turns_written_so_far(self, monkeypatch, tmp_path):
        self._enabled(monkeypatch)
        history = tmp_path / "plan_dialogue" / "history.md"
        append_turn(history, "human", "part 1 is mixed")
        append_turn(history, "plan", "split it")
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)
        text = history.read_text(encoding="utf-8")
        # divided, not deleted: the record survives for a human to copy from
        assert active_turns(text) == []
        assert parse_history(text) == [
            DialogueTurn("human", "part 1 is mixed"),
            DialogueTurn("plan", "split it"),
        ]

    def test_history_file_created_when_enabled(self, monkeypatch, tmp_path):
        self._enabled(monkeypatch)
        history = tmp_path / "plan_dialogue" / "history.md"
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)
        assert history.is_file()
        assert read_history(history) == []

    def test_disabled_writes_no_history(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        _run(monkeypatch, tmp_path, {"plan": {"enabled": False}}, self._ps(), history=history)
        assert not history.exists()

    def test_revised_plan_is_deleted(self, monkeypatch, tmp_path):
        self._enabled(monkeypatch)
        revised = tmp_path / "plan_revise" / "plan.json"
        revised.parent.mkdir()
        revised.write_text('{"directions": []}', encoding="utf-8")
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), revised=revised)
        # it revises a plan that no longer exists; director must stop preferring it
        assert not revised.exists()

    def test_revised_plan_deleted_even_when_disabled(self, monkeypatch, tmp_path):
        revised = tmp_path / "plan_revise" / "plan.json"
        revised.parent.mkdir()
        revised.write_text('{"directions": []}', encoding="utf-8")
        _run(monkeypatch, tmp_path, {"plan": {"enabled": False}}, self._ps(), revised=revised)
        assert not revised.exists()

    def test_missing_revised_plan_is_fine(self, monkeypatch, tmp_path):
        self._enabled(monkeypatch)
        _run(
            monkeypatch,
            tmp_path,
            {"plan": {"enabled": True}},
            self._ps(),
            revised=tmp_path / "plan_revise" / "plan.json",
        )


class TestPlanExplainsItself:
    """The run that builds the whole plan is the one a human most needs
    explained — and it is the run with no conversation to reply to."""

    def _ps(self):
        return ProjectSummary("all", [PartSummary("a", (1, 4), "x")])

    def _run_with(self, monkeypatch, tmp_path, message, history):
        monkeypatch.setattr(
            plan_run,
            "generate_plan",
            lambda ps, cfg, **kw: ParsedPlan([PartDirection("a", (1, 4), "feature")], message),
        )
        return _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)

    def test_the_account_is_written_below_the_divider(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        append_turn(history, "human", "an old instruction")
        self._run_with(monkeypatch, tmp_path, "the build is the throughline", history)
        # it describes the plan this run just made, so it is not retired with the
        # turns above the divider — and plan_revise sees it as context
        assert active_turns(history.read_text(encoding="utf-8")) == [
            DialogueTurn("plan", "the build is the throughline")
        ]

    def test_it_does_not_look_like_an_unanswered_turn(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        self._run_with(monkeypatch, tmp_path, "an account", history)
        assert not has_unanswered_human(active_turns(history.read_text(encoding="utf-8")))

    def test_the_file_ends_where_a_human_can_reply(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        self._run_with(monkeypatch, tmp_path, "an account", history)
        assert history.read_text(encoding="utf-8").rstrip().endswith("## human")

    def test_no_message_appends_no_turn(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        self._run_with(monkeypatch, tmp_path, "", history)
        assert active_turns(history.read_text(encoding="utf-8")) == []
        assert history.read_text(encoding="utf-8").rstrip().endswith("## human")
