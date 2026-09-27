"""summary run: disabled no-op and enabled project-wide summary paths."""

from __future__ import annotations

import json

import yaml

import nagare_clip.summary.run as summary_run
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


def _run(monkeypatch, tmp_path, cfg_dict, txt_by_stem, json_by_stem=None):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    txt_args = []
    for stem, text in txt_by_stem.items():
        p = tmp_path / f"{stem}.txt"
        p.write_text(text, encoding="utf-8")
        txt_args.append(p)

    json_args = []
    if json_by_stem:
        for stem, js_data in json_by_stem.items():
            p = tmp_path / f"{stem}.json"
            p.write_text(json.dumps(js_data), encoding="utf-8")
            json_args.append(p)

    out = tmp_path / "summary.json"
    summary_run.run_summary(
        txt_args,
        out,
        yaml.safe_load(cfg.read_text(encoding="utf-8")),
        json_paths=json_args if json_args else None,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def test_disabled_writes_empty(monkeypatch, tmp_path):
    data = _run(monkeypatch, tmp_path, {"summary": {"enabled": False}}, {"a": "x\n"})
    assert data == {"summary": "", "parts": [], "keywords": {}, "video_summaries": {}}


def test_enabled_writes_summary_with_stems_from_basename(monkeypatch, tmp_path):
    captured = {}

    def fake_build(parts_input, cfg, **kwargs):
        captured["stems"] = [stem for stem, _ in parts_input]
        return ProjectSummary(
            summary="all",
            parts=[PartSummary("a", (1, 1), "ay"), PartSummary("b", (1, 1), "be")],
        )

    monkeypatch.setattr(summary_run, "build_summary", fake_build)
    data = _run(
        monkeypatch,
        tmp_path,
        {"summary": {"enabled": True}},
        {"a": "ax\n", "b": "bx\n"},
    )
    assert captured["stems"] == ["a", "b"]
    assert data == {
        "summary": "all",
        "parts": [
            {"stem": "a", "lines": [1, 1], "summary": "ay"},
            {"stem": "b", "lines": [1, 1], "summary": "be"},
        ],
        "keywords": {},
        "video_summaries": {},
    }


def test_json_passes_seg_times_by_stem(monkeypatch, tmp_path):
    captured = {}

    def fake_build(parts_input, cfg, **kwargs):
        captured["seg_times_by_stem"] = kwargs.get("seg_times_by_stem")
        return ProjectSummary(summary="all", parts=[PartSummary("v", (1, 1), "x")])

    monkeypatch.setattr(summary_run, "build_summary", fake_build)

    _run(
        monkeypatch,
        tmp_path,
        {"summary": {"enabled": True}},
        {"v": "あ\nい\n"},
        {"v": {"segments": [{"start": 1.0, "end": 3.0}, {"start": 4.0, "end": 6.5}]}},
    )
    assert captured["seg_times_by_stem"] == {"v": [(1.0, 3.0), (4.0, 6.5)]}


def test_lines_passed_verbatim(monkeypatch, tmp_path):
    captured = {}

    def fake_build(parts_input, cfg, **kwargs):
        captured["lines"] = parts_input[0][1]
        return ProjectSummary(summary="", parts=[])

    monkeypatch.setattr(summary_run, "build_summary", fake_build)
    _run(monkeypatch, tmp_path, {"summary": {"enabled": True}}, {"v": "line {{a->b}} raw\n"})
    assert captured["lines"] == ["line {{a->b}} raw"]


def test_keywords_written_to_output(monkeypatch, tmp_path):
    def fake_build(parts_input, cfg, **kwargs):
        return ProjectSummary(summary="all", parts=[], keywords={"a": ["K"]})

    monkeypatch.setattr(summary_run, "build_summary", fake_build)
    data = _run(monkeypatch, tmp_path, {"summary": {"enabled": True}}, {"a": "ax\n"})
    assert data["keywords"] == {"a": ["K"]}


def test_run_summary_feeds_gap_descriptions_into_the_prompt(tmp_path, monkeypatch):
    import nagare_clip.summary.summarize as summarize_mod

    txt = tmp_path / "a.txt"
    txt.write_text("いち\nに\n", encoding="utf-8")
    jsonp = tmp_path / "a.json"
    jsonp.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 10.0}, {"start": 20.0, "end": 25.0}]}),
        encoding="utf-8",
    )
    gapsp = tmp_path / "a_gaps.json"
    gapsp.write_text(
        json.dumps(
            {"gaps": [{"start": 10.0, "end": 20.0, "frames": [], "description": "デモが動く"}]}
        ),
        encoding="utf-8",
    )

    seen = {}

    def fake_llm(messages, cfg):
        seen.setdefault("users", []).append(messages[1]["content"])
        return json.dumps({"parts": [], "keywords": [], "video_summary": "v"})

    monkeypatch.setattr(summarize_mod, "_call_llm", fake_llm)
    summary_run.run_summary(
        [txt],
        tmp_path / "summary.json",
        {"summary": {"enabled": True, "prompt": "P", "overall_prompt": "O", "max_retries": 0}},
        json_paths=[jsonp],
        gaps_paths=[gapsp],
    )
    assert any("after line 1" in u and "デモが動く" in u for u in seen["users"])


def test_project_brief_appended_to_both_summary_prompts(monkeypatch, tmp_path):
    """The editorial brief reaches the per-video and the all-videos prompts."""
    from nagare_clip.summary import summarize as summarize_mod

    txt = tmp_path / "v.txt"
    txt.write_text("一行目\n", encoding="utf-8")

    systems: list[str] = []

    def fake_llm(messages, cfg):
        systems.append(messages[0]["content"])
        return json.dumps({"parts": [{"lines": [1, 1], "summary": "s"}], "video_summary": "v"})

    monkeypatch.setattr(summarize_mod, "_call_llm", fake_llm)
    summary_run.run_summary(
        [txt],
        tmp_path / "summary.json",
        {
            "summary": {"enabled": True, "prompt": "P", "overall_prompt": "O", "max_retries": 0},
            "project": {"tone": "punchy"},
        },
    )
    assert len(systems) == 2  # segment + reduce
    assert all(s.endswith("- Tone: punchy") for s in systems)
    assert systems[0].startswith("P\n\n") and systems[1].startswith("O\n\n")


def test_no_brief_leaves_summary_prompts_untouched(monkeypatch, tmp_path):
    from nagare_clip.summary import summarize as summarize_mod

    txt = tmp_path / "v.txt"
    txt.write_text("一行目\n", encoding="utf-8")

    systems: list[str] = []

    def fake_llm(messages, cfg):
        systems.append(messages[0]["content"])
        return json.dumps({"parts": [{"lines": [1, 1], "summary": "s"}], "video_summary": "v"})

    monkeypatch.setattr(summarize_mod, "_call_llm", fake_llm)
    summary_run.run_summary(
        [txt],
        tmp_path / "summary.json",
        {"summary": {"enabled": True, "prompt": "P", "overall_prompt": "O", "max_retries": 0}},
    )
    assert systems == ["P", "O"]
