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
