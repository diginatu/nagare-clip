"""plan_revise run: it fires only on an unanswered human turn, and writes its
own artifact rather than into plan/."""

from __future__ import annotations

import json

import yaml

import nagare_clip.plan_revise.run as revise_run
from nagare_clip.plan.dialogue import append_divider, append_turn, read_active_history
from nagare_clip.plan.plan_llm import PartDirection, plan_to_dict
from nagare_clip.plan_revise.revise_llm import Revision
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict


def _ps():
    return ProjectSummary(
        "all",
        [PartSummary("a", (1, 4), "intro"), PartSummary("a", (5, 9), "the payoff")],
    )


def _plan():
    return [
        PartDirection("a", (1, 4), "feature — sets up the build"),
        PartDirection("a", (5, 9), "feature — the payoff, full length"),
    ]


def _paths(tmp_path, directions=None, project_summary=None):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(summary_to_dict(project_summary or _ps())), encoding="utf-8")
    plan_json = tmp_path / "plan" / "plan.json"
    plan_json.parent.mkdir(exist_ok=True)
    plan_json.write_text(
        json.dumps(plan_to_dict(_plan() if directions is None else directions)), encoding="utf-8"
    )
    return summary, plan_json, tmp_path / "plan_revise" / "plan.json"


def _run(tmp_path, cfg_dict, *, history=None, directions=None):
    summary, plan_json, out = _paths(tmp_path, directions)
    revise_run.run_plan_revise(
        summary, plan_json, out, yaml.safe_load(yaml.safe_dump(cfg_dict)), history=history
    )
    return out


ENABLED = {"plan_revise": {"enabled": True}}


class TestFiringCondition:
    def test_no_history_no_call(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(revise_run, "generate_revision", lambda *a, **k: calls.append(1))
        out = _run(tmp_path, ENABLED, history=tmp_path / "history.md")
        assert calls == [] and not out.exists()

    def test_answered_turn_no_call(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(revise_run, "generate_revision", lambda *a, **k: calls.append(1))
        history = tmp_path / "history.md"
        append_turn(history, "human", "split part 2")
        append_turn(history, "plan", "done")
        out = _run(tmp_path, ENABLED, history=history)
        assert calls == [] and not out.exists()

    def test_turns_above_a_divider_do_not_fire_it(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(revise_run, "generate_revision", lambda *a, **k: calls.append(1))
        history = tmp_path / "history.md"
        append_turn(history, "human", "split part 2")
        append_divider(history)
        out = _run(tmp_path, ENABLED, history=history)
        assert calls == [] and not out.exists()

    def test_an_existing_revision_survives_a_run_that_does_not_fire(self, tmp_path, monkeypatch):
        monkeypatch.setattr(revise_run, "generate_revision", lambda *a, **k: 1 / 0)
        summary, plan_json, out = _paths(tmp_path)
        out.parent.mkdir(exist_ok=True)
        out.write_text('{"directions": [{"stem": "a", "lines": [1, 4], "direction": "x"}]}\n')
        before = out.read_text(encoding="utf-8")
        revise_run.run_plan_revise(summary, plan_json, out, ENABLED, history=None)
        assert out.read_text(encoding="utf-8") == before

    def test_disabled_deletes_a_stale_revision(self, tmp_path, monkeypatch):
        monkeypatch.setattr(revise_run, "generate_revision", lambda *a, **k: 1 / 0)
        summary, plan_json, out = _paths(tmp_path)
        out.parent.mkdir(exist_ok=True)
        out.write_text('{"directions": []}\n')
        history = tmp_path / "history.md"
        append_turn(history, "human", "split part 2")
        revise_run.run_plan_revise(
            summary, plan_json, out, {"plan_revise": {"enabled": False}}, history=history
        )
        assert not out.exists()


class TestRevising:
    def _history(self, tmp_path):
        history = tmp_path / "history.md"
        append_turn(history, "human", "part 2: only 7-9 is the demo, 5-6 is a digression")
        return history

    def test_one_call_writes_its_own_artifact_and_replies(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake(project_summary, directions, turns, cfg, **kw):
            seen["turns"] = turns
            seen["directions"] = directions
            seen["prompt"] = cfg.get("prompt")
            return Revision(
                directions=[_plan()[0], PartDirection("a", (7, 9), "feature — the demo")],
                message="split part 2 at line 7",
                applied="1 added, 1 deleted, 0 updated",
                ok=True,
            )

        monkeypatch.setattr(revise_run, "generate_revision", fake)
        history = self._history(tmp_path)
        out = _run(tmp_path, ENABLED, history=history)
        assert json.loads(out.read_text(encoding="utf-8")) == {
            "directions": [
                {"stem": "a", "lines": [1, 4], "direction": "feature — sets up the build"},
                {"stem": "a", "lines": [7, 9], "direction": "feature — the demo"},
            ]
        }
        assert seen["directions"] == _plan()
        assert [t.role for t in seen["turns"]] == ["human"]
        # the reply answers the turn, so a re-run costs nothing
        turns = read_active_history(history)
        assert turns[-1].role == "plan" and "split part 2 at line 7" in turns[-1].text

    def test_plan_json_is_never_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda *a, **k: Revision(directions=[], message="m", applied="", ok=True),
        )
        summary, plan_json, out = _paths(tmp_path)
        before = plan_json.read_text(encoding="utf-8")
        revise_run.run_plan_revise(
            summary, plan_json, out, ENABLED, history=self._history(tmp_path)
        )
        assert plan_json.read_text(encoding="utf-8") == before

    def test_an_empty_message_still_answers_the_turn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda *a, **k: Revision(
                directions=_plan(), message="", applied="0 added, 1 deleted, 0 updated", ok=True
            ),
        )
        history = self._history(tmp_path)
        _run(tmp_path, ENABLED, history=history)
        turns = read_active_history(history)
        assert turns[-1].role == "plan" and "1 deleted" in turns[-1].text

    def test_a_failed_call_writes_nothing_and_leaves_the_turn_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda *a, **k: Revision(directions=_plan(), ok=False),
        )
        history = self._history(tmp_path)
        out = _run(tmp_path, ENABLED, history=history)
        assert not out.exists()
        assert read_active_history(history)[-1].role == "human"

    def test_missing_summary_degrades_without_a_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(revise_run, "generate_revision", lambda *a, **k: 1 / 0)
        _, plan_json, out = _paths(tmp_path)
        revise_run.run_plan_revise(
            tmp_path / "nope.json", plan_json, out, ENABLED, history=self._history(tmp_path)
        )
        assert not out.exists()

    def test_project_brief_is_appended_to_the_prompt(self, tmp_path, monkeypatch):
        seen: dict = {}

        def fake(project_summary, directions, turns, cfg, **kw):
            seen["prompt"] = cfg["prompt"]
            return Revision(directions=[], ok=True)

        monkeypatch.setattr(revise_run, "generate_revision", fake)
        _run(
            tmp_path,
            {
                "plan_revise": {"enabled": True, "prompt": "P"},
                "project": {"target_duration": "12 minutes"},
            },
            history=self._history(tmp_path),
        )
        assert seen["prompt"].startswith("P\n\n")
        assert seen["prompt"].endswith("- Target duration: 12 minutes")


class TestRoundTrip:
    """The loop from the improvement request, end to end over the real LLM
    parsing: one sentence splits one part and the rest is carried through."""

    def test_untouched_directions_are_byte_identical(self, tmp_path, monkeypatch):
        import nagare_clip.plan_revise.revise_llm as revise_llm

        ps = ProjectSummary(
            "all",
            [
                PartSummary("a", (1, 4), "intro"),
                PartSummary("a", (5, 9), "the payoff"),
                PartSummary("b", (1, 3), "wrap"),
            ],
        )
        directions = [
            PartDirection("a", (1, 4), "feature — sets up the build"),
            PartDirection("a", (5, 9), "feature — the payoff, full length"),
            PartDirection("b", (1, 3), "shorten — the sign-off drags"),
        ]
        ids = revise_llm.assign_ids(directions)
        response = json.dumps(
            {
                "delete": [ids[1]],
                "add": [
                    {"index": 2, "lines": [5, 6], "direction": "remove — digression"},
                    {"index": 2, "lines": [7, 9], "direction": "feature — the demo itself"},
                ],
                "message": "パート2を2つに分割しました",
            }
        )
        seen: list[str] = []

        def fake_llm(messages, _cfg):
            seen.append(messages[-1]["content"])
            return response

        real = revise_llm.generate_revision
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda ps_, ds, turns, cfg, **kw: real(ps_, ds, turns, cfg, call_llm=fake_llm, **kw),
        )

        summary = tmp_path / "summary.json"
        summary.write_text(json.dumps(summary_to_dict(ps)), encoding="utf-8")
        plan_json = tmp_path / "plan" / "plan.json"
        plan_json.parent.mkdir()
        plan_json.write_text(
            json.dumps(plan_to_dict(directions), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        history = tmp_path / "history.md"
        append_turn(history, "human", "パート2は 7-9 だけがデモ本体、5-6 は脱線")
        out = tmp_path / "plan_revise" / "plan.json"
        revise_run.run_plan_revise(summary, plan_json, out, ENABLED, history=history)

        before = json.loads(plan_json.read_text(encoding="utf-8"))["directions"]
        after = json.loads(out.read_text(encoding="utf-8"))["directions"]
        assert len(after) == 4
        # the two the human never mentioned come through untouched
        assert after[0] == before[0]
        assert after[3] == before[2]
        assert [d["lines"] for d in after] == [[1, 4], [5, 6], [7, 9], [1, 3]]
        # the model saw the ids and the human's sentence
        assert f"[{ids[1]}]" in seen[0] and "7-9 だけがデモ本体" in seen[0]
