"""publish run(): disabled no-op, wiring of every input, graceful degradation."""

from __future__ import annotations

import json

from nagare_clip.director.director_llm import DirectorOp, ops_to_dict
from nagare_clip.plan.plan_llm import PartDirection, plan_to_dict
from nagare_clip.publish import run as publish_run
from nagare_clip.publish.publish_llm import PublishCopy, ThumbLine, ThumbSet
from nagare_clip.publish.render import empty_publish_data
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict

SEG_TIMES = [(0.0, 60.0), (60.0, 120.0), (120.0, 240.0)]


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _project(tmp_path, *, keeps=((0.0, 240.0),), ops=(), cfg_extra=None):
    intervals = _write(
        tmp_path / "a_intervals.json",
        {"keep_intervals": [{"start": s, "end": e} for s, e in keeps]},
    )
    summary = _write(
        tmp_path / "summary.json",
        summary_to_dict(
            ProjectSummary(
                summary="a project",
                parts=[
                    PartSummary("a", (1, 1), "part one", start=0.0, end=60.0),
                    PartSummary("a", (2, 2), "part two", start=60.0, end=120.0),
                    PartSummary("a", (3, 3), "part three", start=120.0, end=240.0),
                ],
            )
        ),
    )
    plan = _write(tmp_path / "plan.json", plan_to_dict([PartDirection("a", (1, 1), "feature it")]))
    director = _write(tmp_path / "a_director.json", ops_to_dict(list(ops)))
    segments = _write(
        tmp_path / "a.json",
        {"segments": [{"start": s, "end": e, "text": "x"} for s, e in SEG_TIMES]},
    )
    cfg = {"publish": {"enabled": True, **(cfg_extra or {})}}
    return cfg, dict(
        intervals_paths=[intervals],
        summary_json=summary,
        plan_json=plan,
        director_paths=[director],
        json_paths=[segments],
    )


def _run(tmp_path, cfg, inputs):
    out_json = tmp_path / "out" / "publish.json"
    out_md = tmp_path / "out" / "publish.md"
    candidates = publish_run.run_publish(["a"], out_json, out_md, cfg, **inputs)
    return json.loads(out_json.read_text(encoding="utf-8")), out_md.read_text("utf-8"), candidates


def test_disabled_writes_an_empty_artifact_and_calls_no_llm(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM must not be called when disabled")

    monkeypatch.setattr(publish_run.publish_llm, "generate_publish_copy", boom)
    cfg, inputs = _project(tmp_path)
    cfg["publish"]["enabled"] = False
    data, md, candidates = _run(tmp_path, cfg, inputs)
    assert data == empty_publish_data()
    assert candidates == []
    assert "publish stage disabled" in md


def test_enabled_writes_chapters_copy_and_frame_candidates(tmp_path, monkeypatch):
    seen: dict = {}

    def fake(project, chapters, cfg, **kwargs):
        seen["chapters"] = [(c.start, c.title) for c in chapters]
        seen["overlays"] = kwargs["overlays"]
        seen["directions"] = [d.direction for d in kwargs["directions"]]
        seen["duration"] = kwargs["duration"]
        return PublishCopy(
            titles=["hooky title"],
            lead="a lead",
            chapter_titles={1: "導入"},
            thumbnail_copy=[ThumbSet([ThumbLine("hook", "水浸し！")])],
        )

    monkeypatch.setattr(publish_run.publish_llm, "generate_publish_copy", fake)
    ops = [
        DirectorOp(type="overlay", lines=(2, 2), text="水浸し！", duration=3.0),
        DirectorOp(type="keep", lines=(3, 3)),
    ]
    cfg, inputs = _project(tmp_path, ops=ops)
    data, md, candidates = _run(tmp_path, cfg, inputs)

    # the LLM saw the already-final chapters, the plan and the on-screen copy
    assert seen["chapters"] == [(0.0, "part one"), (60.0, "part two"), (120.0, "part three")]
    assert seen["overlays"] == ["水浸し！"] and seen["directions"] == ["feature it"]
    assert seen["duration"] == 240.0
    # and its titles landed in the output
    assert data["titles"] == ["hooky title"]
    assert data["chapters"][0]["title"] == "導入"
    assert data["description"].startswith("a lead\n\n0:00 導入\n1:00 part two")
    assert data["chapter_issues"] == []
    assert [f["source_time"] for f in data["thumbnail_frames"]] == [60.0, 180.0]
    assert [c.relpath for c in candidates] == ["frames/a/60.000.jpg", "frames/a/180.000.jpg"]
    assert "hooky title" in md


def test_cut_footage_reshapes_the_chapter_list(tmp_path, monkeypatch):
    monkeypatch.setattr(
        publish_run.publish_llm, "generate_publish_copy", lambda *a, **k: PublishCopy()
    )
    # part two is cut entirely; part three survives 5s -> merged away
    cfg, inputs = _project(tmp_path, keeps=((0.0, 60.0), (120.0, 125.0)))
    data, md, _ = _run(tmp_path, cfg, inputs)
    assert [c["title"] for c in data["chapters"]] == ["part one"]
    assert data["chapter_issues"] == ["only 1 timestamp(s); YouTube needs at least 3"]
    assert "YouTube will not render" in md


def test_missing_optional_inputs_degrade_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(
        publish_run.publish_llm, "generate_publish_copy", lambda *a, **k: PublishCopy()
    )
    cfg, inputs = _project(tmp_path)
    inputs["summary_json"] = tmp_path / "nope.json"
    inputs["plan_json"] = None
    inputs["director_paths"] = None
    inputs["json_paths"] = None
    data, _, candidates = _run(tmp_path, cfg, inputs)
    assert data["chapters"] == [] and candidates == []
    assert data["duration_sec"] == 240.0


def test_max_frames_caps_the_shortlist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        publish_run.publish_llm, "generate_publish_copy", lambda *a, **k: PublishCopy()
    )
    ops = [DirectorOp(type="overlay", lines=(i, i), text=f"o{i}", duration=2.0) for i in (1, 2, 3)]
    cfg, inputs = _project(tmp_path, ops=ops, cfg_extra={"max_frames": 2})
    data, _, _ = _run(tmp_path, cfg, inputs)
    assert len(data["thumbnail_frames"]) == 2


def test_project_brief_is_appended_to_the_publish_prompt(tmp_path, monkeypatch):
    seen: dict = {}

    def fake(project, chapters, cfg, **kwargs):
        seen["prompt"] = cfg.get("prompt", "")
        return PublishCopy()

    monkeypatch.setattr(publish_run.publish_llm, "generate_publish_copy", fake)
    cfg, inputs = _project(tmp_path)
    cfg["publish"]["prompt"] = "base prompt"
    cfg["project"] = {"audience": "aquarium builders"}
    _run(tmp_path, cfg, inputs)
    assert seen["prompt"].startswith("base prompt")
    assert "aquarium builders" in seen["prompt"]


def test_no_brief_leaves_the_prompt_byte_identical(tmp_path, monkeypatch):
    seen: dict = {}

    def fake(project, chapters, cfg, **kwargs):
        seen["prompt"] = cfg.get("prompt", "")
        return PublishCopy()

    monkeypatch.setattr(publish_run.publish_llm, "generate_publish_copy", fake)
    cfg, inputs = _project(tmp_path)
    cfg["publish"]["prompt"] = "base prompt"
    _run(tmp_path, cfg, inputs)
    assert seen["prompt"] == "base prompt"
