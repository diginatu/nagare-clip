"""plan run: disabled no-op and enabled rough-directions paths."""

from __future__ import annotations

import json

import yaml

import nagare_clip.plan.run as plan_run
from nagare_clip.plan.dialogue import DialogueTurn, append_turn, read_history
from nagare_clip.plan.plan_llm import ParsedPlan, PartDirection
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict


def _run(monkeypatch, tmp_path, cfg_dict, project_summary, history=None):
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


class TestDialogue:
    def _ps(self):
        return ProjectSummary("all", [PartSummary("a", (1, 4), "x")])

    def test_previous_plan_is_fed_back(self, monkeypatch, tmp_path):
        seen: dict = {}

        def fake_generate(project_summary, cfg, **kwargs):
            seen["previous"] = kwargs.get("previous")
            return ParsedPlan([PartDirection("a", (1, 4), "second")])

        monkeypatch.setattr(plan_run, "generate_plan", fake_generate)
        (tmp_path / "plan.json").write_text(
            json.dumps({"directions": [{"stem": "a", "lines": [1, 4], "direction": "first"}]}),
            encoding="utf-8",
        )
        data = _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps())
        assert seen["previous"] == [PartDirection("a", (1, 4), "first")]
        assert data["directions"][0]["direction"] == "second"

    def test_no_previous_plan_yet(self, monkeypatch, tmp_path):
        seen: dict = {}

        def fake_generate(project_summary, cfg, **kwargs):
            seen["previous"] = kwargs.get("previous")
            return ParsedPlan([])

        monkeypatch.setattr(plan_run, "generate_plan", fake_generate)
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps())
        assert seen["previous"] == []

    def test_history_is_read_and_reply_appended(self, monkeypatch, tmp_path):
        seen: dict = {}
        history = tmp_path / "plan_dialogue" / "history.md"
        history.parent.mkdir()
        history.write_text("## human\n\npart 1 is mixed\n", encoding="utf-8")

        def fake_generate(project_summary, cfg, **kwargs):
            seen["history"] = kwargs.get("history")
            return ParsedPlan([], "split it into [1,2] and [3,4]")

        monkeypatch.setattr(plan_run, "generate_plan", fake_generate)
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)
        assert seen["history"] == [DialogueTurn("human", "part 1 is mixed")]
        assert read_history(history) == [
            DialogueTurn("human", "part 1 is mixed"),
            DialogueTurn("plan", "split it into [1,2] and [3,4]"),
        ]

    def test_empty_message_appends_nothing(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        monkeypatch.setattr(plan_run, "generate_plan", lambda ps, cfg, **kw: ParsedPlan([], ""))
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)
        assert read_history(history) == []
        # not even an empty heading: an empty reply is not a turn
        lines = history.read_text(encoding="utf-8").splitlines()
        assert not [ln for ln in lines if ln.strip() == "## plan"]

    def test_history_file_created_when_enabled(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        monkeypatch.setattr(plan_run, "generate_plan", lambda ps, cfg, **kw: ParsedPlan([], ""))
        _run(monkeypatch, tmp_path, {"plan": {"enabled": True}}, self._ps(), history=history)
        assert history.is_file()

    def test_disabled_writes_no_history(self, monkeypatch, tmp_path):
        history = tmp_path / "plan_dialogue" / "history.md"
        _run(monkeypatch, tmp_path, {"plan": {"enabled": False}}, self._ps(), history=history)
        assert not history.exists()


class TestConversationRoundTrip:
    """The loop from the improvement request: run, disagree in one sentence,
    re-run — the corrected part is split and the rest survives untouched."""

    def _ps(self):
        return ProjectSummary(
            "all",
            [PartSummary("a", (1, 4), "intro"), PartSummary("a", (5, 9), "the payoff")],
        )

    def test_second_run_splits_one_part_and_leaves_the_other_identical(self, monkeypatch, tmp_path):
        import nagare_clip.plan.plan_llm as plan_llm

        real = plan_llm.generate_plan
        seen: list[str] = []
        responses = [
            '{"directions": ['
            '{"index": 1, "direction": "feature — sets up the build"},'
            '{"index": 2, "direction": "feature — the payoff, full length"}'
            '], "message": "part 2 reads as one demonstration; is it?"}',
            '{"directions": ['
            '{"index": 1, "direction": "feature — sets up the build"},'
            '{"index": 2, "lines": [5, 6], "direction": "remove — digression"},'
            '{"index": 2, "lines": [7, 9], "direction": "feature — the demo itself"}'
            '], "message": "split part 2 at line 7 as you said"}',
        ]

        def fake_llm(messages, _cfg):
            seen.append(messages[-1]["content"])
            return responses.pop(0)

        monkeypatch.setattr(
            plan_run,
            "generate_plan",
            lambda ps, cfg, **kw: real(ps, cfg, call_llm=fake_llm, **kw),
        )
        history = tmp_path / "plan_dialogue" / "history.md"
        cfg = {"plan": {"enabled": True, "prompt": "P"}}

        first = _run(monkeypatch, tmp_path, cfg, self._ps(), history=history)
        assert "part 2 reads as one demonstration" in history.read_text(encoding="utf-8")

        # the human disagrees in one sentence
        append_turn(history, "human", "part 2: only 7-9 is the demo, 5-6 is a digression")

        second = _run(monkeypatch, tmp_path, cfg, self._ps(), history=history)

        # round two saw its own plan and both turns
        assert "feature — the payoff, full length" in seen[1]
        assert "only 7-9 is the demo" in seen[1]
        assert "part 2 reads as one demonstration" in seen[1]
        # round one saw neither
        assert "your last direction" not in seen[0]

        assert second["directions"][0] == first["directions"][0]
        assert second["directions"][1:] == [
            {"stem": "a", "lines": [5, 6], "direction": "remove — digression"},
            {"stem": "a", "lines": [7, 9], "direction": "feature — the demo itself"},
        ]
        assert read_history(history)[-1] == DialogueTurn(
            "plan", "split part 2 at line 7 as you said"
        )
