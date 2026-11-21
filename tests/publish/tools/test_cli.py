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


def test_view_first_line_names_the_output_dir(setup, capsys):
    project, _ = setup
    assert cli.main(["view"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"output dir: {project.ctx.output_dir}"


def test_frames_names_the_publish_dir(setup, monkeypatch, capsys):
    project, _ = setup
    monkeypatch.setattr(cli, "extract_stills", lambda proj, stills, **kw: stills)
    assert cli.main(["frames", "--line", "1"]) == 0
    out = capsys.readouterr().out
    assert f"stills in {project.ctx.stage_dir('publish')}" in out


def test_render_prints_the_thumbnails_dir(setup, capsys):
    project, _ = setup
    assert cli.main(["render"]) == 0
    assert (
        f"thumbnails in {project.ctx.stage_dir('render') / 'thumbnails'}" in capsys.readouterr().out
    )


def test_frames_prints_the_left_out_note(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "extract_stills", lambda proj, stills, **kw: stills)

    def fake_plan(project, cfg, requests, *, notes=None):
        notes.append("2 shortlist still(s) not in the finished video were left out")
        return [], []

    monkeypatch.setattr(cli, "plan_stills", fake_plan)
    assert cli.main(["frames"]) == 0
    assert "2 shortlist still(s) not in the finished video were left out" in capsys.readouterr().out


def test_view_lists_the_font_slots_and_marks_the_fallback(setup, capsys):
    project, _ = setup
    project.ctx.cfg["render"]["fonts"] = {"sans-bold": "Noto-Sans-CJK-JP-Bold", "serif": "Serif-X"}
    assert cli.main(["view"]) == 0
    out = capsys.readouterr().out
    assert "## Fonts (render.fonts — the `font` values a thumbnail line may name)" in out
    assert "- sans-bold: Noto-Sans-CJK-JP-Bold   (the fallback for a line with no font)" in out
    assert "- serif: Serif-X\n" in out


def test_view_says_when_no_fonts_are_set(setup, capsys):
    project, _ = setup
    project.ctx.cfg["render"]["fonts"] = {}
    assert cli.main(["view"]) == 0
    out = capsys.readouterr().out
    assert (
        "none set — a line uses ImageMagick's default face, which may draw Japanese "
        "as nothing; ask the user to set render.fonts"
    ) in out


def _caption_ops(monkeypatch, ops):
    monkeypatch.setattr(cli, "read_ops", lambda ctx, stems: {"a": ops})


def _captions_block(out: str) -> list[str]:
    block = out.split("## Captions\n", 1)[1].split("\n\n", 1)[0]
    return block.splitlines()


def test_captions_time_markers(setup, monkeypatch, capsys):
    from nagare_clip.director.ops import DirectorOp

    _caption_ops(
        monkeypatch,
        [
            DirectorOp(type="overlay", lines=(2, 2), text="late", duration=2.0),
            DirectorOp(type="overlay", lines=(1, 1), text="early", duration=2.0),
            DirectorOp(type="overlay", lines=(1, 1), text="early", duration=2.0),
            DirectorOp(
                type="overlay", lines=(1, 1), text="in silence", duration=2.0, gap_start=True
            ),
        ],
    )
    assert cli.main(["view"]) == 0
    assert _captions_block(capsys.readouterr().out) == [
        "- 0:00  early",
        "- 0:01  late",
        "- ✂  in silence",
    ]


def test_captions_without_a_timeline_show_a_dash(setup, monkeypatch, capsys):
    from nagare_clip.director.ops import DirectorOp

    monkeypatch.setattr(cli, "finished_placements", lambda p: None)
    _caption_ops(monkeypatch, [DirectorOp(type="overlay", lines=(2, 2), text="x", duration=2.0)])
    assert cli.main(["view"]) == 0
    assert _captions_block(capsys.readouterr().out) == ["- —  x"]


def test_view_compact_prints_the_played_transcript_with_finished_times(setup, capsys):
    assert cli.main(["view", "--compact"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("output dir:")
    assert "finished runtime" in out
    assert "THE TRANSCRIPT AS IT PLAYS" in out
    assert "0:00  1: one" in out
    assert "0:01  3: two" in out
    assert "0:00  one" not in out  # not the per-line rows


def test_view_compact_and_lines_are_mutually_exclusive(setup, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["view", "--compact", "--lines", "1-2"])
    assert e.value.code == 2


def test_view_compact_falls_back_when_the_director_files_do_not_map(setup, monkeypatch, capsys):
    from nagare_clip.director.tools.project import CurrentState

    monkeypatch.setattr(
        cli,
        "current_state",
        lambda project: CurrentState(ops={}, order=[], plan="", unmapped=["a: op 3-9 is gone"]),
    )
    assert cli.main(["view", "--compact"]) == 0
    cap = capsys.readouterr()
    assert "a: op 3-9 is gone" in cap.err
    assert "0:00  one" in cap.out


def test_chapters_prints_a_table_sorted_by_finished_time(setup, capsys):
    project, _ = setup
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5
    assert cli.main(["chapters", "3", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["line", "time", "length", "text"]
    assert out[1].split() == ["1", "0:00", "0:01", "one"]
    assert out[2].split() == ["3", "0:01", "0:01", "two"]
    assert any("only 2 chapter" in row for row in out[3:])


def test_chapters_reports_problems_and_exits_1(setup, capsys):
    assert cli.main(["chapters", "1", "3"]) == 1  # both runs are 1 s, under the 10 s minimum
    cap = capsys.readouterr()
    assert "shorter than the minimum 10s" in cap.err + cap.out
    assert "← shorter than 10 s" in cap.out


def test_chapters_truncates_text_to_40_chars(setup, monkeypatch, capsys):
    project, _ = setup
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5

    project.transcripts[0].edit_lines[0] = "x" * 60
    assert cli.main(["chapters", "1"]) in (0, 1)
    out = capsys.readouterr().out
    assert "x" * 39 + "…" in out and "x" * 40 not in out


def test_frames_docker_failure_exits_1_with_the_reason(setup, monkeypatch, capsys):
    from nagare_clip.pipeline.errors import PipelineError

    def boom(proj, stills, **kw):
        raise PipelineError(
            "frames: extraction in Docker failed (1 job(s)): exit 125\nno such image"
        )

    monkeypatch.setattr(cli, "extract_stills", boom)
    assert cli.main(["frames", "--line", "1"]) == 1
    err = capsys.readouterr().err
    assert "extraction in Docker failed" in err
    assert "no such image" in err


def test_frames_warns_on_stderr_about_a_still_the_batch_did_not_write(
    setup, tmp_path, monkeypatch, capsys
):
    from nagare_clip.publish.tools import frames as frames_mod

    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.mp4").write_bytes(b"")
    monkeypatch.setattr(
        cli,
        "extract_stills",
        lambda proj, stills, **kw: frames_mod.extract_stills(
            proj, stills, run=lambda argv, **k: None, **kw
        ),
    )
    assert cli.main(["frames", "--line", "1"]) == 0
    err = capsys.readouterr().err
    assert "warning: frames: no still written at 1.500s for a; dropped" in err


def test_frames_skips_a_malformed_stills_row_with_a_warning(setup, monkeypatch, capsys):
    project, _ = setup
    monkeypatch.setattr(cli, "extract_stills", lambda proj, stills, **kw: stills)
    path = project.ctx.stage_dir("publish") / "stills.json"
    path.parent.mkdir(parents=True)
    good = {
        "stem": "a",
        "source_time": 1.0,
        "kind": "line",
        "label": "keep me",
        "line": 1,
        "path": "frames/a/1.000.jpg",
    }
    path.write_text(json.dumps([{"stem": "a"}, good]), encoding="utf-8")
    assert cli.main(["frames", "--line", "3"]) == 0
    assert "stills.json row 0" in capsys.readouterr().err
    labels = [r["label"] for r in json.loads(path.read_text())]
    assert labels == ["keep me", "line 3 @0.5"]
