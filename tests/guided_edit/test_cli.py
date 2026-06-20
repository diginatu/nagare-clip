"""guided_edit CLI: disabled passthrough and enabled apply paths."""

from __future__ import annotations

import json
import sys

import yaml

import nagare_clip.guided_edit.cli as ge_cli


def _setup(tmp_path, cfg_dict, edits_text, director_ops):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text(edits_text, encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(json.dumps({"ops": director_ops}), encoding="utf-8")
    out = tmp_path / "out_edits.txt"
    return cfg, edits, director, out


def _argv(edits, director, out, cfg):
    return [
        "guided_edit",
        "--edits-txt",
        str(edits),
        "--director",
        str(director),
        "--output",
        str(out),
        "--config",
        str(cfg),
    ]


def test_disabled_copies_through(monkeypatch, tmp_path):
    cfg, edits, director, out = _setup(
        tmp_path, {"guided_edit": {"enabled": False}}, "あ\nい\n", [{"type": "cut", "lines": [1, 1]}]
    )
    monkeypatch.setattr(sys, "argv", _argv(edits, director, out, cfg))
    ge_cli.main()
    assert out.read_text(encoding="utf-8") == "あ\nい\n"


def test_enabled_applies_ops(monkeypatch, tmp_path):
    cfg, edits, director, out = _setup(
        tmp_path,
        {"guided_edit": {"enabled": True}},
        "あいう\nかきく\n",
        [{"type": "cut", "lines": [1, 1]}],
    )

    monkeypatch.setattr(sys, "argv", _argv(edits, director, out, cfg))
    ge_cli.main()
    # cut is a span op -> applied deterministically (no LLM needed)
    assert out.read_text(encoding="utf-8").splitlines()[0] == "<cut>あいう</cut>"
    # Unapplied report written next to output
    report = tmp_path / "out_unapplied.txt"
    assert report.exists()
    assert "all director ops applied" in report.read_text(encoding="utf-8")
