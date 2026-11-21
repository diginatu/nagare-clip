"""The publish tools CLI: view, frames, write, render (no Docker, no magick)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools import cli
from tests.publish.tools.conftest import make_project

REPO = Path(__file__).resolve().parents[3]

# line 1 (1-2s) survives at 0-1s; silence (2-5s) is cut; line 2 (5-6s) at 1-2s
PLACEMENTS = [
    Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0),
    Placement("a", 5.0, 6.0, 1.0, 2.0, 1.0),
]
PLACEMENTS_CUT = [Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0), Placement("a", 5.0, 6.0, 1.0, 2.0, 1.0)]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    monkeypatch.setattr(cli, "load_project", lambda cfg: project)
    monkeypatch.setattr(cli, "finished_placements", lambda p: PLACEMENTS)
    monkeypatch.setattr(cli, "read_ops", lambda ctx, stems: {})
    calls: list = []
    monkeypatch.setattr(
        cli,
        "run_render",
        lambda pj, out, cfg, *, run, markdown=None: calls.append((pj, out, markdown)),
    )
    return project, calls


GOOD = {
    "titles": ["Title"],
    "lead": "Lead",
    "chapters": [{"line": 1, "title": "Start"}, {"line": 3, "title": "End"}],
    "thumbnails": [
        {
            "background": "frames/a/1.500.jpg",
            "lines": [{"text": "Hi"}],
        }
    ],
}


def _edit_file(tmp_path, data):
    p = tmp_path / "e.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_view_prints_times_and_cut_marker(setup, capsys):
    assert cli.main(["view"]) == 0
    out = capsys.readouterr().out
    assert "0:00  one" in out
    assert "✂" in out  # the silence line is cut
    assert "0:01  two" in out
    assert "finished runtime" in out


def test_view_without_timeline_prints_dash(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "finished_placements", lambda p: None)
    assert cli.main(["view"]) == 0
    out = capsys.readouterr().out
    assert "unknown — no intervals yet" in out
    assert "—  one" in out
    assert "✂" not in out


def test_view_lines_narrows(setup, capsys):
    assert cli.main(["view", "--lines", "3-3"]) == 0
    out = capsys.readouterr().out
    assert "two" in out
    assert "  one" not in out


def test_write_problems_exit_1_writes_nothing(setup, tmp_path, capsys):
    project, calls = setup
    bad = {**GOOD, "titles": []}
    assert cli.main(["write", "--edit", _edit_file(tmp_path, bad)]) == 1
    assert "titles" in capsys.readouterr().err
    assert not (project.ctx.stage_dir("publish") / "edit.json").exists()
    assert calls == []


def test_write_writes_edit_publish_md_and_renders(setup, tmp_path, capsys):
    project, calls = setup
    path = _edit_file(tmp_path, GOOD)
    (project.ctx.stage_dir("publish") / "frames/a").mkdir(parents=True)
    (project.ctx.stage_dir("publish") / "frames/a/1.500.jpg").write_bytes(b"x")
    # chapters are 0:00 and 0:01 (< 10s) so min chapter would reject; lower it
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5
    assert cli.main(["write", "--edit", path]) == 0
    pub = project.ctx.stage_dir("publish")
    assert json.loads((pub / "edit.json").read_text()) == GOOD
    data = json.loads((pub / "publish.json").read_text())
    assert {"titles", "lead", "description", "chapters", "thumbnail_copy", "thumbnails"} <= set(
        data
    )
    assert (pub / "publish.md").is_file()
    assert len(calls) == 1
    assert calls[0][0] == pub / "publish.json"
    assert str(pub / "publish.md") in capsys.readouterr().out


def test_write_no_render(setup, tmp_path):
    project, calls = setup
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5
    (project.ctx.stage_dir("publish") / "frames/a").mkdir(parents=True)
    (project.ctx.stage_dir("publish") / "frames/a/1.500.jpg").write_bytes(b"x")
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    assert calls == []
    assert (project.ctx.stage_dir("publish") / "publish.json").is_file()


def test_render_runs_render(setup):
    _, calls = setup
    assert cli.main(["render"]) == 0
    assert len(calls) == 1


def test_frames_writes_stills_json(setup, monkeypatch, capsys):
    project, _ = setup
    seen = {}

    def fake_extract(proj, stills, **kw):
        seen["n"] = len(stills)
        return stills

    monkeypatch.setattr(cli, "extract_stills", fake_extract)
    assert cli.main(["frames", "--line", "1", "--line", "2@0.25"]) == 0
    rows = json.loads((project.ctx.stage_dir("publish") / "stills.json").read_text())
    assert [r["label"] for r in rows] == ["line 1 @0.5", "line 2 @0.25"]
    assert seen["n"] == 2
    assert "frames/a/" in capsys.readouterr().out


def test_frames_line_merges_with_existing(setup, monkeypatch):
    project, _ = setup
    monkeypatch.setattr(cli, "extract_stills", lambda proj, stills, **kw: stills)
    assert cli.main(["frames", "--line", "1"]) == 0
    assert cli.main(["frames", "--line", "2"]) == 0
    rows = json.loads((project.ctx.stage_dir("publish") / "stills.json").read_text())
    assert len(rows) == 2


def test_frames_bad_line_exit_1(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "extract_stills", lambda proj, stills, **kw: stills)
    assert cli.main(["frames", "--line", "99"]) == 1
    assert "no such line" in capsys.readouterr().err


def test_no_project_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["view"]) == 2
    assert "error" in capsys.readouterr().err


def test_shim_exists_and_is_executable():
    shim = REPO / "scripts" / "publish_tools.sh"
    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    assert "nagare_clip.publish.tools" in shim.read_text()
