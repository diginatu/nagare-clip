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
    parse_history,
    read_history,
)
from nagare_clip.plan.plan_llm import PartDirection
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
        return [PartDirection("a", (1, 2), "keep"), PartDirection("b", (1, 1), "remove")]

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
        return []

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
            return [PartDirection("a", (1, 4), "second")]

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

    def _enabled(self, monkeypatch):
        monkeypatch.setattr(plan_run, "generate_plan", lambda ps, cfg, **kw: [])

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
