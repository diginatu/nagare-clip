"""plan run: disabled no-op and enabled rough-directions paths."""

from __future__ import annotations

import json

import yaml

import nagare_clip.plan.run as plan_run
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict


def _run(monkeypatch, tmp_path, cfg_dict, project_summary):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(summary_to_dict(project_summary)), encoding="utf-8")
    out = tmp_path / "plan.json"
    plan_run.run_plan(
        summary,
        out,
        yaml.safe_load(cfg.read_text(encoding="utf-8")),
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
