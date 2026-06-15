"""summary CLI: disabled no-op and enabled project-wide summary paths."""

from __future__ import annotations

import json
import sys

import yaml

import nagare_clip.summary.cli as summary_cli
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


def _run(monkeypatch, tmp_path, cfg_dict, edits_by_stem):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")
    edits_args = []
    for stem, text in edits_by_stem.items():
        p = tmp_path / f"{stem}_edits.txt"
        p.write_text(text, encoding="utf-8")
        edits_args += ["--edits-txt", str(p)]
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["summary", *edits_args, "--output", str(out), "--config", str(cfg)],
    )
    summary_cli.main()
    return json.loads(out.read_text(encoding="utf-8"))


def test_disabled_writes_empty(monkeypatch, tmp_path):
    data = _run(monkeypatch, tmp_path, {"summary": {"enabled": False}}, {"a": "x\n"})
    assert data == {"summary": "", "parts": []}


def test_enabled_writes_summary_with_stems_from_basename(monkeypatch, tmp_path):
    captured = {}

    def fake_build(parts_input, cfg, **kwargs):
        captured["stems"] = [stem for stem, _ in parts_input]
        return ProjectSummary(
            summary="all",
            parts=[PartSummary("a", (1, 1), "ay"), PartSummary("b", (1, 1), "be")],
        )

    monkeypatch.setattr(summary_cli, "build_summary", fake_build)
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
    }


def test_json_passes_seg_times_by_stem(monkeypatch, tmp_path):
    captured = {}

    def fake_build(parts_input, cfg, **kwargs):
        captured["seg_times_by_stem"] = kwargs.get("seg_times_by_stem")
        return ProjectSummary(summary="all", parts=[PartSummary("v", (1, 1), "x")])

    monkeypatch.setattr(summary_cli, "build_summary", fake_build)

    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({"summary": {"enabled": True}}), encoding="utf-8")
    edits = tmp_path / "v_edits.txt"
    edits.write_text("あ\nい\n", encoding="utf-8")
    js = tmp_path / "v.json"
    js.write_text(
        json.dumps({"segments": [
            {"start": 1.0, "end": 3.0}, {"start": 4.0, "end": 6.5}]}),
        encoding="utf-8",
    )
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "summary",
            "--edits-txt", str(edits),
            "--json", str(js),
            "--output", str(out),
            "--config", str(cfg),
        ],
    )
    summary_cli.main()
    assert captured["seg_times_by_stem"] == {"v": [(1.0, 3.0), (4.0, 6.5)]}
