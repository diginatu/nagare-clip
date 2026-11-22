"""frames: the shortlist and any line's still, extracted in one container."""

from __future__ import annotations

import json
import subprocess

import pytest

from nagare_clip.director.ops import DirectorOp
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.external import build_snapshot_batch_cmd
from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools import frames
from nagare_clip.publish.tools.frames import (
    Still,
    extract_stills,
    format_stills,
    plan_stills,
    read_stills,
    snapshot_jobs,
    write_stills,
)


def _overlay_ops(monkeypatch, ops):
    monkeypatch.setattr(frames, "read_ops", lambda ctx, stems: {"a": ops})


def test_shortlist_from_director_ops(project, monkeypatch):
    op = DirectorOp(type="overlay", lines=(1, 1), text="Hi", duration=2.0)
    _overlay_ops(monkeypatch, [op])
    d = project.ctx.stage_dir("sentence_split")
    d.mkdir(parents=True)
    (d / "a.json").write_text(
        json.dumps({"segments": [{"start": 1.0, "end": 2.0}, {"start": 5.0, "end": 6.0}]})
    )
    stills, problems = plan_stills(project, project.ctx.cfg, [])
    assert problems == []
    assert len(stills) == 1
    s = stills[0]
    assert (s.stem, s.kind, s.source_time, s.label, s.line) == ("a", "overlay", 1.5, "Hi", 1)
    assert s.path == "frames/a/1.500.jpg"


def test_line_requests_add_stills(project):
    stills, problems = plan_stills(project, project.ctx.cfg, [(1, 1.0)])
    assert problems == []
    assert [(s.kind, s.source_time, s.line) for s in stills] == [("line", 2.0, 1)]


def test_line_request_midpoint(project):
    stills, _ = plan_stills(project, project.ctx.cfg, [(2, 0.5)])
    assert stills[0].source_time == 3.5
    assert stills[0].line == 2


def test_bad_pos_is_a_problem(project):
    stills, problems = plan_stills(project, project.ctx.cfg, [(1, 1.5), (99, 0.5)])
    assert stills == []
    assert len(problems) == 2


def test_jobs_use_discovered_relative_path(tmp_path):
    s = Still("My Clip", 1.0, "line", "", 1, "frames/My Clip/1.000.jpg")
    jobs = snapshot_jobs([s], {"My Clip": "My Clip.MP4"})
    assert jobs == [("My Clip.MP4", 1.0, "/output/publish/frames/My Clip/1.000.jpg")]
    assert "'My Clip.MP4'" in build_snapshot_batch_cmd(tmp_path, jobs, 640).script


def _prep(project):
    (project.ctx.input_videos_dir).mkdir(parents=True, exist_ok=True)
    (project.ctx.input_videos_dir / "a.mp4").write_bytes(b"")
    return [Still("a", t, "line", "", 1, f"frames/a/{t:.3f}.jpg") for t in (1.0, 1.5, 1.9)]


def test_one_docker_call_for_all_stills(project):
    stills = _prep(project)
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        for s in stills:
            p = project.ctx.stage_dir("publish") / s.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")

    out = extract_stills(project, stills, run=run)
    assert len(calls) == 1 and out == stills
    assert calls[0][1]["stdin_text"] and calls[0][1]["capture_stderr"] is True
    assert calls[0][1]["env_extra"]["OUTPUT_DIR"] == str(project.ctx.output_dir)


def test_no_stills_no_call(project):
    assert extract_stills(project, [], run=lambda *a, **k: pytest.fail("called")) == []


def test_docker_failure_raises_pipeline_error(project):
    stills = _prep(project)

    def run(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr="boom daemon")

    with pytest.raises(PipelineError, match="boom daemon"):
        extract_stills(project, stills, run=run)


def test_missing_output_dropped(project):
    stills = _prep(project)

    def run(argv, **kw):
        p = project.ctx.stage_dir("publish") / stills[0].path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    assert extract_stills(project, stills, run=run) == [stills[0]]


def test_write_read_and_format(tmp_path):
    placements = [Placement("a", 0.0, 10.0, 0.0, 10.0, 1.0)]
    stills = [
        Still("a", 7.0, "line", "late", 2, "frames/a/7.000.jpg"),
        Still("a", 3.0, "overlay", "early", 1, "frames/a/3.000.jpg"),
        Still("a", 99.0, "keep", "cut", None, "frames/a/99.000.jpg"),
    ]
    path = tmp_path / "stills.json"
    assert read_stills(path) == []
    write_stills(path, stills, placements)
    rows = read_stills(path)
    assert rows[0] == {
        "stem": "a",
        "source_time": 7.0,
        "kind": "line",
        "label": "late",
        "line": 2,
        "path": "frames/a/7.000.jpg",
        "timeline_time": 7.0,
    }
    assert rows[2]["timeline_time"] is None
    lines = format_stills(stills, placements).splitlines()
    assert [ln.split()[-1] for ln in lines] == [
        "frames/a/3.000.jpg",
        "frames/a/7.000.jpg",
        "frames/a/99.000.jpg",
    ]
    assert "0:03" in lines[0] and "✂" in lines[2]
    # with no timeline at all it is unknown, not cut
    assert "—" in format_stills(stills, None).splitlines()[2]


def _timings(project):
    d = project.ctx.stage_dir("sentence_split")
    d.mkdir(parents=True)
    (d / "a.json").write_text(
        json.dumps({"segments": [{"start": 1.0, "end": 2.0}, {"start": 5.0, "end": 6.0}]})
    )


SHOWN_ONLY_LINE_1 = [Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0)]


def test_shortlist_leaves_out_stills_not_in_the_finished_video(project, monkeypatch):
    # overlay on line 1 (shown) and a higher-priority-equal one on line 2 (cut)
    ops = [
        DirectorOp(type="overlay", lines=(1, 1), text="in", duration=2.0),
        DirectorOp(type="overlay", lines=(2, 2), text="out", duration=2.0),
    ]
    _overlay_ops(monkeypatch, ops)
    _timings(project)
    monkeypatch.setattr(frames, "finished_placements", lambda p: SHOWN_ONLY_LINE_1)
    notes: list[str] = []
    stills, problems = plan_stills(project, project.ctx.cfg, [], notes=notes)
    assert problems == []
    assert [s.label for s in stills] == ["in"]
    assert notes == ["1 shortlist still(s) not in the finished video were left out"]


def test_the_cap_is_filled_with_stills_that_are_shown(project, monkeypatch):
    ops = [
        DirectorOp(type="overlay", lines=(2, 2), text="out", duration=2.0),
        DirectorOp(type="overlay", lines=(1, 1), text="in", duration=2.0),
    ]
    _overlay_ops(monkeypatch, ops)
    _timings(project)
    project.ctx.cfg["publish"]["max_frames"] = 1
    monkeypatch.setattr(frames, "finished_placements", lambda p: SHOWN_ONLY_LINE_1)
    stills, _ = plan_stills(project, project.ctx.cfg, [])
    assert [s.label for s in stills] == ["in"]


def test_without_a_timeline_the_shortlist_is_unchanged(project, monkeypatch):
    ops = [DirectorOp(type="overlay", lines=(2, 2), text="out", duration=2.0)]
    _overlay_ops(monkeypatch, ops)
    _timings(project)
    monkeypatch.setattr(frames, "finished_placements", lambda p: None)
    notes: list[str] = []
    stills, _ = plan_stills(project, project.ctx.cfg, [], notes=notes)
    assert [s.label for s in stills] == ["out"]
    assert notes == []


def test_line_requests_are_never_dropped(project, monkeypatch):
    monkeypatch.setattr(frames, "finished_placements", lambda p: SHOWN_ONLY_LINE_1)
    stills, _ = plan_stills(project, project.ctx.cfg, [(2, 0.5)])
    assert len(stills) == 1


def test_at_stills_map_finished_time_to_a_source_still(project):
    placements = [Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0), Placement("a", 5.0, 6.0, 1.0, 2.0, 1.0)]
    stills, problems = frames.at_stills(project, placements, [0.5, 1.5])
    assert problems == []
    assert [(s.kind, s.label, s.source_time, s.line, s.path) for s in stills] == [
        ("at", "at 0:00", 1.5, 1, "frames/a/1.500.jpg"),
        ("at", "at 0:01", 5.5, 3, "frames/a/5.500.jpg"),
    ]


def test_at_without_a_timeline_or_past_the_end_is_a_problem(project):
    placements = [Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0), Placement("a", 5.0, 6.0, 1.0, 2.0, 1.0)]
    stills, problems = frames.at_stills(project, None, [1.0])
    assert stills == []
    assert problems == ["--at needs the finished timeline (run intervals)"]
    stills, problems = frames.at_stills(project, placements, [90.0, 3600.0])
    assert stills == []
    assert problems == [
        "--at 1:30 is past the finished runtime 0:02",
        "--at 1:00:00 is past the finished runtime 0:02",
    ]


def _sheet_stills(project, n):
    out = project.ctx.stage_dir("publish")
    stills = []
    for i in range(n):
        s = Still("a", 1.0 + i, "line", f"l{i}", 1 if i % 2 == 0 else None, f"frames/a/{i}.jpg")
        (out / s.path).parent.mkdir(parents=True, exist_ok=True)
        (out / s.path).write_bytes(b"x")
        stills.append(s)
    return stills


PLAYBACK = [Placement("a", 0.0, 100.0, 0.0, 100.0, 1.0)]


def test_listing_carries_the_index(project):
    stills = _sheet_stills(project, 2)
    lines = format_stills(stills, PLAYBACK).splitlines()
    assert lines[0].startswith("#1  ") and lines[1].startswith("#2  ")


def test_sheet_commands_chunk_twelve_per_sheet_in_listed_order(project):
    stills = _sheet_stills(project, 14)
    out = project.ctx.stage_dir("publish")
    cmds = frames.sheet_commands(stills, PLAYBACK, out, width=320, font="")
    assert [len(c) for c in cmds] == [len(cmds[0]), len(cmds[1])]
    assert [c[-1] for c in cmds] == [str(out / "sheets" / f"sheet{k}.jpg") for k in (1, 2)]
    first = cmds[0]
    assert first[:2] == ["magick", "montage"]
    assert first[first.index("-tile") + 1] == "4x"
    assert first[first.index("-geometry") + 1] == "320x+4+4"
    assert "-font" not in first
    labels = [first[i + 1] for i, a in enumerate(first) if a == "-label"]
    files = [a for a in first if a.endswith(".jpg") and "frames" in a]
    assert len(labels) == len(files) == 12
    assert labels[0] == "#1  L1  0:01"
    assert labels[1] == "#2  L-  0:02"
    assert files[0].endswith("frames/a/0.jpg") and files[11].endswith("frames/a/11.jpg")
    assert [cmds[1][i + 1] for i, a in enumerate(cmds[1]) if a == "-label"] == [
        "#13  L1  0:13",
        "#14  L-  0:14",
    ]


def test_sheet_labels_follow_the_listing_index_not_the_file_order(project):
    stills = _sheet_stills(project, 3)
    # the middle one is cut: listed last, so it is tile #3
    placements = [Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0), Placement("a", 3.0, 4.0, 1.0, 2.0, 1.0)]
    cmds = frames.sheet_commands(stills, placements, project.ctx.stage_dir("publish"), width=100)
    labels = [cmds[0][i + 1] for i, a in enumerate(cmds[0]) if a == "-label"]
    assert labels == ["#1  L1  0:00", "#2  L1  0:01", "#3  L-  cut"]


def test_sheet_escapes_percent_and_passes_the_font(project):
    s = Still("a", 1.0, "line", "x", 5, "frames/a/0.jpg")
    out = project.ctx.stage_dir("publish")
    (out / s.path).parent.mkdir(parents=True)
    (out / s.path).write_bytes(b"x")
    assert frames.magick_label("50% #1") == "50%% #1"
    cmd = frames.sheet_commands([s], PLAYBACK, out, width=100, font="Noto Sans")[0]
    assert cmd[cmd.index("-font") + 1] == "Noto Sans"


def test_sheet_skips_missing_files_and_makes_no_command_when_none_left(project):
    stills = _sheet_stills(project, 2)
    out = project.ctx.stage_dir("publish")
    (out / stills[0].path).unlink()
    cmds = frames.sheet_commands(stills, PLAYBACK, out, width=100)
    assert sum(a.endswith("frames/a/1.jpg") for a in cmds[0]) == 1
    assert sum(a.endswith("frames/a/0.jpg") for a in cmds[0]) == 0
    (out / stills[1].path).unlink()
    assert frames.sheet_commands(stills, PLAYBACK, out, width=100) == []


def test_sheet_labels_are_ascii_even_without_a_timeline(project):
    stills = _sheet_stills(project, 2)
    out = project.ctx.stage_dir("publish")
    cmd = frames.sheet_commands(stills, None, out, width=100)[0]
    labels = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-label"]
    assert labels == ["#1  L1  -", "#2  L-  -"]
    assert all(label.isascii() for label in labels)
