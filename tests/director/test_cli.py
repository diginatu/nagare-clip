"""director CLI: disabled no-op and enabled op-generation paths."""

from __future__ import annotations

import json
import sys

import yaml

import nagare_clip.director.cli as director_cli
from nagare_clip.director.director_llm import generate_director_ops


def _run(monkeypatch, tmp_path, cfg_dict, edits_text):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text(edits_text, encoding="utf-8")
    out = tmp_path / "clip_director.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "director",
            "--edits-txt",
            str(edits),
            "--output",
            str(out),
            "--config",
            str(cfg),
        ],
    )
    director_cli.main()
    return json.loads(out.read_text(encoding="utf-8"))


def test_disabled_writes_empty_ops(monkeypatch, tmp_path):
    data = _run(monkeypatch, tmp_path, {"director": {"enabled": False}}, "あ\nい\n")
    assert data == {"ops": []}


def test_enabled_writes_parsed_ops(monkeypatch, tmp_path):
    def fake_llm(_messages, _cfg):
        return '{"ops": [{"type": "cut", "lines": [1, 2], "note": "boring"}]}'

    monkeypatch.setattr(
        director_cli,
        "generate_director_ops",
        lambda lines, c, overview_context="", **kw: generate_director_ops(
            lines, c, call_llm=fake_llm, overview_context=overview_context
        ),
    )

    data = _run(
        monkeypatch, tmp_path, {"director": {"enabled": True}}, "あい\nうえ\n"
    )
    assert data["ops"] == [{"type": "cut", "lines": [1, 2], "note": "boring"}]


def test_overview_context_injected_for_stem(monkeypatch, tmp_path):
    import json as _json

    # summary + plan artifacts referencing stem "clip"
    summary = tmp_path / "summary.json"
    summary.write_text(
        _json.dumps(
            {
                "summary": "Project overview text",
                "parts": [{"stem": "clip", "lines": [1, 2], "summary": "the part"}],
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        _json.dumps(
            {
                "directions": [
                    {"stem": "clip", "lines": [1, 2], "direction": "keep tight"}
                ]
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake(lines, c, overview_context="", **kw):
        captured["ctx"] = overview_context
        return []

    monkeypatch.setattr(director_cli, "generate_director_ops", fake)

    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({"director": {"enabled": True}}), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    out = tmp_path / "clip_director.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "director",
            "--edits-txt", str(edits),
            "--output", str(out),
            "--summary", str(summary),
            "--plan", str(plan),
            "--stem", "clip",
            "--config", str(cfg),
        ],
    )
    director_cli.main()
    assert "Project overview text" in captured["ctx"]
    assert "the part" in captured["ctx"]
    assert "keep tight" in captured["ctx"]
