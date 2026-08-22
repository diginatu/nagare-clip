"""The whole loop, over the real parsers: plan writes, the human argues,
plan_revise revises, and a plan re-run invalidates what was built on it."""

from __future__ import annotations

import json

import nagare_clip.plan.plan_llm as plan_llm
import nagare_clip.plan.run as plan_run
import nagare_clip.plan_revise.revise_llm as revise_llm
import nagare_clip.plan_revise.run as revise_run
from nagare_clip.plan.dialogue import append_turn, read_active_history
from nagare_clip.plan_revise.ids import assign_ids
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict

PARTS = [
    PartSummary("v1", (1, 30), "the build"),
    PartSummary("v1", (31, 83), "the demonstration"),
    PartSummary("v2", (1, 20), "the wrap-up"),
]

PLAN_RESPONSE = json.dumps(
    {
        "directions": [
            {"index": 1, "direction": "shorten — the setup drags"},
            {"index": 2, "direction": "feature — the climactic demonstration"},
            {"index": 3, "direction": "remove — repeats part 1"},
        ]
    }
)


class Loop:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.calls = {"plan": 0, "revise": 0}
        self.summary = tmp_path / "summary.json"
        self.summary.write_text(
            json.dumps(summary_to_dict(ProjectSummary("a pump build", PARTS))), encoding="utf-8"
        )
        self.plan_json = tmp_path / "plan" / "plan.json"
        self.revised = tmp_path / "plan_revise" / "plan.json"
        self.history = tmp_path / "plan_dialogue" / "history.md"
        self.revise_response = "{}"

        real_plan = plan_llm.generate_plan
        real_revise = revise_llm.generate_revision

        def plan_llm_call(_messages, _cfg):
            self.calls["plan"] += 1
            return PLAN_RESPONSE

        def revise_llm_call(messages, _cfg):
            self.calls["revise"] += 1
            self.seen = messages[-1]["content"]
            return self.revise_response

        monkeypatch.setattr(
            plan_run,
            "generate_plan",
            lambda ps, cfg, **kw: real_plan(ps, cfg, call_llm=plan_llm_call, **kw),
        )
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda ps, ds, turns, cfg, **kw: real_revise(
                ps, ds, turns, cfg, call_llm=revise_llm_call, **kw
            ),
        )

    def plan(self):
        plan_run.run_plan(
            self.summary,
            self.plan_json,
            {"plan": {"enabled": True, "prompt": "P"}},
            history=self.history,
            revised=self.revised,
        )

    def revise(self):
        revise_run.run_plan_revise(
            self.summary,
            self.plan_json,
            self.revised,
            {"plan_revise": {"enabled": True, "prompt": "R"}},
            history=self.history,
        )

    def directions(self, path):
        return json.loads(path.read_text(encoding="utf-8"))["directions"]


def test_the_conversation_loop(tmp_path, monkeypatch):
    loop = Loop(tmp_path, monkeypatch)

    # 1. nothing to say: plan writes its directions, plan_revise costs nothing
    loop.plan()
    loop.revise()
    assert loop.calls == {"plan": 1, "revise": 0}
    assert len(loop.directions(loop.plan_json)) == 3
    assert not loop.revised.exists()  # director reads plan/plan.json

    # 2. one human turn: one revise call, and only the named direction changes
    before = loop.directions(loop.plan_json)
    ids = assign_ids(plan_llm.plan_from_dict({"directions": before}))
    loop.revise_response = json.dumps(
        {
            "delete": [ids[1]],
            "add": [
                {"index": 2, "lines": [31, 59], "direction": "remove — the digression"},
                {"index": 2, "lines": [60, 83], "direction": "feature — the demo itself"},
            ],
            "message": "パート2を31-59と60-83に分割しました",
        }
    )
    append_turn(loop.history, "human", "v1 [31,83] は 60-83 だけがデモ本体")
    loop.revise()
    assert loop.calls["revise"] == 1
    after = loop.directions(loop.revised)
    assert len(after) == 4
    assert after[0] == before[0] and after[3] == before[2]
    assert [d["lines"] for d in after] == [[1, 30], [31, 59], [60, 83], [1, 20]]
    assert loop.directions(loop.plan_json) == before  # plan/ is never written into

    # the reply answers the turn, so a second revise costs nothing
    loop.revise()
    assert loop.calls["revise"] == 1

    # 3. plan re-runs: the revision is invalidated and the turns are retired
    loop.plan()
    assert not loop.revised.exists()
    assert read_active_history(loop.history) == []
    assert "31,83" in loop.history.read_text(encoding="utf-8")  # divided, not deleted
    loop.revise()
    assert loop.calls["revise"] == 1  # nothing follows the divider

    # 4. ids are the same short prefixes across two plan runs over one summary
    assert (
        assign_ids(plan_llm.plan_from_dict({"directions": loop.directions(loop.plan_json)})) == ids
    )
    assert all(len(i) == 4 for i in ids)
