"""The publish tools CLI: view, frames, write, render (no Docker, no magick)."""

from __future__ import annotations

import json
import os
import re
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


def _writable(project):
    pub = project.ctx.stage_dir("publish")
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5
    (pub / "frames/a").mkdir(parents=True)
    (pub / "frames/a/1.500.jpg").write_bytes(b"x")


def test_write_refreshes_output_index(setup, tmp_path):
    project, _ = setup
    _writable(project)
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD)]) == 0
    index = (project.ctx.output_dir / "index.md").read_text(encoding="utf-8")
    row = next(ln for ln in index.splitlines() if "publish/publish.md" in ln)
    assert re.search(r"\| \d\d-\d\d \d\d:\d\d \|$", row)  # an mtime, not —


def test_render_refreshes_output_index(setup):
    project, _ = setup
    assert cli.main(["render"]) == 0
    assert (project.ctx.output_dir / "index.md").is_file()


def test_index_failure_only_warns(setup, tmp_path, monkeypatch, capsys):
    _writable(setup[0])

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(cli, "write_index", boom)
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    assert "warning: index.md not refreshed: nope" in capsys.readouterr().err


def _saved_lines(project):
    return project.ctx.stage_dir("publish") / "edit_lines.json"


def test_write_saves_the_chapter_lines_text(setup, tmp_path):
    project, _ = setup
    _writable(project)
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    assert json.loads(_saved_lines(project).read_text(encoding="utf-8")) == {
        "1": "one",
        "3": "two",
    }


def test_write_warns_when_a_chapter_line_now_reads_differently(setup, tmp_path, capsys):
    project, _ = setup
    _writable(project)
    _saved_lines(project).write_text(json.dumps({"1": "one", "3": "other"}), encoding="utf-8")
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    err = capsys.readouterr().err
    assert (
        "warning: line 3 read 「other」 when edit.json was last written; it now reads 「two」"
        " — the numbering may have moved; check this chapter"
    ) in err
    assert "line 1 read" not in err
    # and the file now describes this write
    assert json.loads(_saved_lines(project).read_text(encoding="utf-8"))["3"] == "two"


def test_write_is_quiet_when_the_text_is_unchanged_or_the_file_absent(setup, tmp_path, capsys):
    project, _ = setup
    _writable(project)
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    assert "read 「" not in capsys.readouterr().err
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    assert "read 「" not in capsys.readouterr().err
    _saved_lines(project).write_text("not json", encoding="utf-8")
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD), "--no-render"]) == 0
    assert "read 「" not in capsys.readouterr().err


def test_chapters_warns_about_drift_too(setup, capsys):
    project, _ = setup
    pub = project.ctx.stage_dir("publish")
    pub.mkdir(parents=True)
    _saved_lines(project).write_text(json.dumps({"3": "other"}), encoding="utf-8")
    cli.main(["chapters", "1", "3"])
    assert "line 3 read 「other」" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text,seconds",
    [("28:16", 1696.0), ("1:02:03", 3723.0), ("90", 90.0), ("1.5", 1.5), ("0:01.5", 1.5)],
)
def test_parse_at(text, seconds):
    assert cli._parse_at(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "-1", "1:xx", "0:75"])
def test_parse_at_rejects_bad_formats(text):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_at(text)


def test_frames_at_merges_with_line_and_uses_one_extraction(setup, monkeypatch, capsys):
    project, _ = setup
    seen = []

    def fake_extract(proj, stills, **kw):
        seen.append(len(stills))
        return stills

    monkeypatch.setattr(cli, "extract_stills", fake_extract)
    assert cli.main(["frames", "--line", "1", "--at", "0:01"]) == 0
    assert seen == [2]
    rows = json.loads((project.ctx.stage_dir("publish") / "stills.json").read_text())
    assert [(r["kind"], r["label"], r["source_time"]) for r in rows] == [
        ("line", "line 1 @0.5", 1.5),
        ("at", "at 0:01", 5.0),
    ]
    assert cli.main(["frames", "--at", "0:00"]) == 0  # merges, does not rewrite
    rows = json.loads((project.ctx.stage_dir("publish") / "stills.json").read_text())
    assert len(rows) == 3


def test_frames_at_past_the_runtime_exits_1_and_extracts_nothing(setup, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "extract_stills", lambda *a, **k: called.append(1) or [])
    assert cli.main(["frames", "--at", "31:00"]) == 1
    assert "--at 31:00 is past the finished runtime 0:02" in capsys.readouterr().err
    assert called == []


def test_frames_at_without_a_timeline_exits_1(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "finished_placements", lambda p: None)
    monkeypatch.setattr(cli, "extract_stills", lambda *a, **k: [])
    assert cli.main(["frames", "--at", "0:01"]) == 1
    assert "--at needs the finished timeline (run intervals)" in capsys.readouterr().err


def _fake_magick(monkeypatch, calls, fail=False):
    def run(cmd):
        calls.append(cmd)
        if fail:
            raise RuntimeError("montage broke")
        Path(cmd[-1]).write_bytes(b"sheet")
        return ""

    monkeypatch.setattr(cli, "run_magick", run)


def _frames_with_files(monkeypatch):
    def extract(proj, stills, **kw):
        for s in stills:
            p = proj.ctx.stage_dir("publish") / s.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"j")
        return stills

    monkeypatch.setattr(cli, "extract_stills", extract)


def test_frames_sheet_writes_sheets_and_prints_paths(setup, monkeypatch, capsys):
    project, _ = setup
    _frames_with_files(monkeypatch)
    calls: list = []
    _fake_magick(monkeypatch, calls)
    sheets = project.ctx.stage_dir("publish") / "sheets"
    sheets.mkdir(parents=True)
    (sheets / "sheet9.jpg").write_bytes(b"stale")
    (sheets / "keep.txt").write_text("k")
    assert cli.main(["frames", "--line", "1", "--line", "2", "--sheet"]) == 0
    assert len(calls) == 1
    assert not (sheets / "sheet9.jpg").exists() and (sheets / "keep.txt").exists()
    assert (sheets / "sheet1.jpg").is_file()
    out = capsys.readouterr().out
    assert f"sheet: {sheets / 'sheet1.jpg'}" in out
    assert out.index("#1  ") < out.index("sheet:")


def test_frames_sheet_covers_every_still_in_stills_json(setup, monkeypatch):
    project, _ = setup
    _frames_with_files(monkeypatch)
    calls: list = []
    _fake_magick(monkeypatch, calls)
    assert cli.main(["frames", "--line", "1"]) == 0
    assert cli.main(["frames", "--line", "2", "--sheet"]) == 0
    assert len([a for a in calls[0] if a == "-label"]) == 2


def test_frames_sheet_magick_failure_exits_1_but_keeps_stills_json(setup, monkeypatch, capsys):
    project, _ = setup
    _frames_with_files(monkeypatch)
    _fake_magick(monkeypatch, [], fail=True)
    assert cli.main(["frames", "--line", "1", "--sheet"]) == 1
    assert "error: " in capsys.readouterr().err
    assert (project.ctx.stage_dir("publish") / "stills.json").is_file()


def test_frames_without_sheet_runs_no_magick(setup, monkeypatch):
    _frames_with_files(monkeypatch)
    calls: list = []
    _fake_magick(monkeypatch, calls)
    assert cli.main(["frames", "--line", "1"]) == 0
    assert calls == []


def test_view_compact_marks_a_line_with_no_surviving_footage_as_one_row(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "finished_placements", lambda p: PLACEMENTS[:1])  # line 3 gone
    assert cli.main(["view", "--compact"]) == 0
    out = capsys.readouterr().out
    assert "0:00  1: one" in out
    assert "    ✂ dropped line 3 — not in the finished video" in out
    assert "✂  3:" not in out


def test_view_rows_still_show_a_cut_line_as_a_scissor(setup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "finished_placements", lambda p: PLACEMENTS[:1])
    assert cli.main(["view"]) == 0
    assert "3  ✂  two" in capsys.readouterr().out


def test_chapters_shows_the_first_chapter_at_0_00_as_write_does(setup, capsys):
    project, _ = setup
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5
    cli.main(["chapters", "3"])
    out = capsys.readouterr().out.splitlines()
    assert out[1].split() == ["3", "0:00", "0:02", "two"]  # survives from 0:01; rendered 0:00


def test_chapters_problems_are_listed_in_edit_order(setup, capsys):
    assert cli.main(["chapters", "3", "1"]) == 1  # chapters[1] (line 1) plays first
    err = capsys.readouterr().err
    assert err.index("chapters[0]") < err.index("chapters[1]")


# --- the .blend's real length vs the computed one ------------------------------

from nagare_clip.publish.blend_check import BlendRange  # noqa: E402


def _blend(project, *, seconds: float | None = None, error: Exception | None = None):
    """Put a .blend where the blender stage writes it; reading it gives *seconds*."""
    path = project.ctx.blend_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"blend")
    calls: list = []

    def read(blend):
        calls.append(blend)
        if error is not None:
            raise error
        return BlendRange(10, 1.0, 1, round(seconds * 10))

    return read, calls


def test_view_warns_when_the_blend_was_trimmed_by_hand(setup, monkeypatch, capsys):
    project, _ = setup
    read, calls = _blend(project, seconds=0.5)  # the timeline computes 2.0 s
    monkeypatch.setattr(cli, "read_blend_range", read)
    assert cli.main(["view"]) == 0
    captured = capsys.readouterr()
    assert calls == [project.ctx.blend_path]
    assert "1.5 s" in captured.err and "shorter" in captured.err
    assert "chapter" in captured.err
    assert "shorter" not in captured.out


def test_view_is_quiet_when_the_blend_matches(setup, monkeypatch, capsys):
    project, _ = setup
    read, calls = _blend(project, seconds=2.0)
    monkeypatch.setattr(cli, "read_blend_range", read)
    assert cli.main(["view"]) == 0
    assert calls and ".blend" not in capsys.readouterr().err


def test_view_does_not_look_without_a_blend_or_a_timeline(setup, monkeypatch, capsys):
    project, _ = setup
    calls: list = []
    monkeypatch.setattr(cli, "read_blend_range", lambda b: calls.append(b))
    assert cli.main(["view"]) == 0  # no .blend yet
    _blend(project, seconds=0.5)
    monkeypatch.setattr(cli, "finished_placements", lambda p: None)
    assert cli.main(["view"]) == 0  # no timeline to compare
    assert calls == []
    assert ".blend" not in capsys.readouterr().err


def test_view_says_when_the_blend_cannot_be_read(setup, monkeypatch, capsys):
    project, _ = setup
    read, _ = _blend(project, error=FileNotFoundError("blender"))
    monkeypatch.setattr(cli, "read_blend_range", read)
    assert cli.main(["view"]) == 0
    err = capsys.readouterr().err
    assert "not checked" in err and "blender" in err


def test_write_warns_on_a_trimmed_blend_and_still_writes(setup, tmp_path, monkeypatch, capsys):
    project, calls = setup
    read, _ = _blend(project, seconds=5.0)
    monkeypatch.setattr(cli, "read_blend_range", read)
    project.ctx.cfg["publish"]["min_chapter_duration"] = 0.5
    (project.ctx.stage_dir("publish") / "frames/a").mkdir(parents=True)
    (project.ctx.stage_dir("publish") / "frames/a/1.500.jpg").write_bytes(b"x")
    assert cli.main(["write", "--edit", _edit_file(tmp_path, GOOD)]) == 0
    err = capsys.readouterr().err
    assert "3.0 s" in err and "longer" in err
    assert (project.ctx.stage_dir("publish") / "publish.json").is_file()
    assert len(calls) == 1


def test_write_with_problems_does_not_read_the_blend(setup, tmp_path, monkeypatch):
    project, _ = setup
    read, calls = _blend(project, seconds=5.0)
    monkeypatch.setattr(cli, "read_blend_range", read)
    assert cli.main(["write", "--edit", _edit_file(tmp_path, {**GOOD, "titles": []})]) == 1
    assert calls == []
