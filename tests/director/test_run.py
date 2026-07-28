"""director.run: disabled no-op and enabled op-generation paths."""

from __future__ import annotations

import json

import nagare_clip.director.run as director_run
from nagare_clip.director.director_llm import generate_director_ops


def test_disabled_writes_empty_ops(tmp_path):
    """When director.enabled is False, write empty ops."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あ\nい\n", encoding="utf-8")
    out = tmp_path / "clip_director.json"

    cfg = {"director": {"enabled": False}}
    director_run.run_director(
        edits_txt=edits,
        output=out,
        cfg=cfg,
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"ops": []}


def test_enabled_writes_parsed_ops(monkeypatch, tmp_path):
    """When director.enabled is True, generate and write ops."""

    def fake_llm(_messages, _cfg):
        return '{"ops": [{"type": "cut", "lines": [1, 2], "note": "boring"}]}'

    monkeypatch.setattr(
        director_run,
        "generate_director_ops",
        lambda lines, c, overview_context="", **kw: generate_director_ops(
            lines, c, call_llm=fake_llm, overview_context=overview_context
        ),
    )

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    out = tmp_path / "clip_director.json"

    cfg = {"director": {"enabled": True}}
    director_run.run_director(
        edits_txt=edits,
        output=out,
        cfg=cfg,
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ops"] == [{"type": "cut", "lines": [1, 2], "note": "boring"}]


def test_overview_context_injected_for_stem(monkeypatch, tmp_path):
    """When summary and plan are provided, build overview context and pass it to generate_director_ops."""
    # summary + plan artifacts referencing stem "clip"
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "summary": "Project overview text",
                "parts": [{"stem": "clip", "lines": [1, 2], "summary": "the part"}],
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"directions": [{"stem": "clip", "lines": [1, 2], "direction": "keep tight"}]}),
        encoding="utf-8",
    )

    captured = {}

    def fake(lines, c, overview_context="", **kw):
        captured["ctx"] = overview_context
        return []

    monkeypatch.setattr(director_run, "generate_director_ops", fake)

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    out = tmp_path / "clip_director.json"

    cfg = {"director": {"enabled": True}}
    director_run.run_director(
        edits_txt=edits,
        output=out,
        cfg=cfg,
        summary=summary,
        plan=plan,
        stem="clip",
    )

    assert "Project overview text" in captured["ctx"]
    assert "the part" in captured["ctx"]
    assert "keep tight" in captured["ctx"]


def test_json_passes_seg_times(monkeypatch, tmp_path):
    """When --json is provided, extract segment times and pass to generate_director_ops."""
    js = tmp_path / "clip.json"
    js.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 1.0, "end": 3.0, "text": "あい"},
                    {"start": 4.0, "end": 6.5, "text": "うえ"},
                ]
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake(lines, c, overview_context="", seg_times=None, **kw):
        captured["seg_times"] = seg_times
        return []

    monkeypatch.setattr(director_run, "generate_director_ops", fake)

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    out = tmp_path / "clip_director.json"

    cfg = {"director": {"enabled": True}}
    director_run.run_director(
        edits_txt=edits,
        output=out,
        cfg=cfg,
        json_path=js,
        stem="clip",
    )

    assert captured["seg_times"] == [(1.0, 3.0), (4.0, 6.5)]


def test_missing_json_passes_none_seg_times(monkeypatch, tmp_path):
    """When --json is not provided or missing, pass None for seg_times."""
    captured = {}

    def fake(lines, c, overview_context="", seg_times=None, **kw):
        captured["seg_times"] = seg_times
        return []

    monkeypatch.setattr(director_run, "generate_director_ops", fake)

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    out = tmp_path / "clip_director.json"

    cfg = {"director": {"enabled": True}}
    director_run.run_director(
        edits_txt=edits,
        output=out,
        cfg=cfg,
    )

    assert captured["seg_times"] is None


def test_run_director_annotates_the_transcript_from_the_gaps_file(tmp_path, monkeypatch):
    import nagare_clip.director.director_llm as dl

    edits = tmp_path / "a_edits.txt"
    edits.write_text("いち\nに\n", encoding="utf-8")
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
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        tmp_path / "a_director.json",
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        stem="a",
        json_path=jsonp,
        gaps=gapsp,
    )
    assert "[silent gap 10.0s: デモが動く]" in seen["user"]


def test_run_director_without_a_gaps_file_is_unchanged(tmp_path, monkeypatch):
    import nagare_clip.director.director_llm as dl

    edits = tmp_path / "a_edits.txt"
    edits.write_text("いち\nに\n", encoding="utf-8")

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        tmp_path / "a_director.json",
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        stem="a",
        gaps=tmp_path / "missing_gaps.json",
    )
    assert seen["user"] == "1: いち\n2: に"


def test_run_director_reads_cuts_txt_for_silence_brackets(tmp_path, monkeypatch):
    """cuts_txt spans inside a segment must surface as 'Ns speech, Ms silence'
    in the LLM user content."""
    import nagare_clip.director.director_llm as dl
    from nagare_clip.audio_silence.cuts_file import write_cuts

    edits = tmp_path / "v_edits.txt"
    edits.write_text("hello\n", encoding="utf-8")
    jsn = tmp_path / "v.json"
    jsn.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 75.8, "text": "hello"}]}),
        encoding="utf-8",
    )
    cuts = tmp_path / "v_cuts.txt"
    write_cuts(cuts, [(10.0, 72.9)])

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        tmp_path / "v_director.json",
        {"director": {"enabled": True, "prompt": "p", "max_retries": 0}},
        stem="v",
        json_path=jsn,
        cuts_txt=cuts,
    )
    assert "speech" in seen["user"] and "silence" in seen["user"]


def test_project_brief_appended_to_director_prompt(monkeypatch, tmp_path):
    """The brief precedes the summary/plan overview block in the system prompt."""
    from nagare_clip.director import director_llm as dl

    edits = tmp_path / "v_edits.txt"
    edits.write_text("あ\n", encoding="utf-8")

    seen: dict = {}

    def fake_llm(messages, cfg):
        seen["system"] = messages[0]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        tmp_path / "v_director.json",
        {
            "director": {"enabled": True, "prompt": "P", "max_retries": 0},
            "project": {"audience": "DIY viewers"},
        },
        stem="v",
    )
    assert seen["system"] == (
        "P\n\nEditorial brief (applies to the whole project; follow it when deciding "
        "what to keep, cut, tighten and emphasise):\n- Audience: DIY viewers"
    )

    seen.clear()
    director_run.run_director(
        edits,
        tmp_path / "v_director.json",
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        stem="v",
    )
    assert seen["system"] == "P"
