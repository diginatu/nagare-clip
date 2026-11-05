"""The leading silence: the wait before line 1, as a silence line of its own.

Every silence line used to be "the silence AFTER line n" (``"n~"``, n >= 1), so
the wait before a source's first word could never be addressed, and a source
with no speech at all (WhisperX ``segments: []``) had nothing addressable —
its ``view`` crashed and it played nothing.  Now the leading wait is a silence
line written above line 1 and addressed as ``"0~"``; a source with no speech is
that one line, ``0.0`` to the media end the audio_silence cut list records.

The fixture project, in display numbering::

    [1] a                     [2] z (no speech)
    1: [silent 12.0s]         4: [silent 42.5s: a hand assembles the pump]
    2: aの1行目
    3: aの2行目
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import nagare_clip.intervals.run as intervals_run
from nagare_clip.audio_silence.cuts_file import read_cuts, read_media_duration, write_cuts
from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.config import DEFAULTS, get_effective_config
from nagare_clip.director.checks import order_ranges, order_segments
from nagare_clip.director.ops import ops_from_dict, parse_op
from nagare_clip.director.tools.cli import main
from nagare_clip.director.tools.project import current_state, load_project
from nagare_clip.edit_lines import (
    expected_silences,
    gap_spans,
    insert_silence_lines,
    leading_span,
    parse_edit_lines,
    silence_body,
    silence_problems,
    with_media_end,
)
from nagare_clip.guided_edit.run import run_guided_edit
from nagare_clip.intervals import check_edits as check_edits_cli
from nagare_clip.intervals.check_edits import check_edits
from nagare_clip.intervals.manifest import build_manifest
from nagare_clip.intervals.run import run_intervals
from nagare_clip.order import Segment, TimelineSegment, validate_segments


def _segment(text: str, start: float, end: float) -> dict:
    """One line, one word per character, evenly over ``[start, end]``."""
    step = (end - start) / len(text)
    words = [
        {"word": ch, "start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3)}
        for i, ch in enumerate(text)
    ]
    return {"start": start, "end": end, "text": text, "words": words}


A_SEGMENTS = [_segment("aの1行目", 12.0, 14.0), _segment("aの2行目", 15.0, 17.0)]
A_JSON = {"segments": A_SEGMENTS}
Z_JSON = {"segments": [], "word_segments": []}
DESCRIPTION = "a hand assembles the pump"
A_LEAD = silence_body(12.0)
Z_LEAD = silence_body(42.5, (DESCRIPTION,))


@pytest.fixture
def project(tmp_path):
    out = tmp_path / "out"
    (tmp_path / "in").mkdir()
    for sub in ("text_filter", "sentence_split", "gap_context", "audio_silence"):
        (out / sub).mkdir(parents=True)
    for stem, data, lines, duration in (
        ("a", A_JSON, ["aの1行目", "aの2行目"], 20.0),
        ("z", Z_JSON, [], 42.5),
    ):
        (tmp_path / "in" / f"{stem}.mp4").touch()
        (out / "text_filter" / f"{stem}_edits.txt").write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8"
        )
        (out / "sentence_split" / f"{stem}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        write_cuts(out / "audio_silence" / f"{stem}_cuts.txt", [], duration=duration)
    (out / "gap_context" / "z_gaps.json").write_text(
        json.dumps(
            {"gaps": [{"start": 3.0, "end": 40.0, "frames": [], "description": DESCRIPTION}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "cfg.yml").write_text(
        yaml.safe_dump(
            {"pipeline": {"input_videos_dir": str(tmp_path / "in"), "output_dir": str(out)}}
        ),
        encoding="utf-8",
    )
    return tmp_path


def _cfg(root: Path) -> str:
    return str(root / "cfg.yml")


def _run(capsys, *argv, code=0):
    assert main(list(argv)) == code
    return capsys.readouterr()


def _edit_file(root: Path, data: dict) -> str:
    path = root / "edit.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


# --- the contract: edit_lines ---------------------------------------------------


def test_the_leading_span_runs_from_the_start_to_line_1s_first_word():
    assert leading_span(A_JSON) == (0.0, 12.0)
    assert gap_spans(A_JSON) == {0: (0.0, 12.0), 1: (14.0, 15.0)}
    assert expected_silences(A_JSON, 5.0) == {0}


def test_a_source_with_no_speech_is_one_leading_silence_to_the_media_end():
    assert leading_span(Z_JSON) is None  # no media end known: no guess
    with_end = with_media_end(Z_JSON, 42.5)
    assert leading_span(with_end) == (0.0, 42.5)
    assert expected_silences(with_end, 5.0) == {0}


def test_the_media_end_is_only_recorded_for_a_source_with_no_speech():
    assert with_media_end(A_JSON, 99.0) is A_JSON
    assert with_media_end(Z_JSON, None) is Z_JSON
    assert with_media_end({"segments": [], "duration": 3.0}, 99.0)["duration"] == 3.0


def test_the_leading_body_is_written_before_line_1():
    assert insert_silence_lines(["x", "y"], {0: A_LEAD}) == [A_LEAD, "x", "y"]
    assert insert_silence_lines([], {0: Z_LEAD}) == [Z_LEAD]
    parsed = parse_edit_lines([A_LEAD, "x"])
    assert parsed.speech_lines() == ["x"]
    assert parsed.file_index(0, True) == 1
    assert silence_body(3.0, after_line=0) == "[silent 3.0s before line 1]"


def test_a_leading_silence_line_is_expected_where_the_wait_is():
    expected = expected_silences(A_JSON, 5.0)
    assert silence_problems(parse_edit_lines([A_LEAD, "x", "y"]), expected) == []
    # Missing is still read: position 0 shifts nothing, and files written
    # before the leading line existed look like this.
    assert silence_problems(parse_edit_lines(["x", silence_body(1.0), "y"]), {0, 1}) == []
    ((line, message),) = silence_problems(parse_edit_lines([A_LEAD, A_LEAD, "x"]), expected)
    assert (line, "duplicate silence line before speech line 1" in message) == (2, True)


# --- audio_silence records the media end -----------------------------------------


def test_the_cut_list_records_the_media_duration_as_a_comment(tmp_path):
    path = tmp_path / "c_cuts.txt"
    write_cuts(path, [(1.0, 2.0)], duration=42.5)
    assert read_media_duration(path) == 42.5
    assert read_cuts(path) == [(1.0, 2.0)]  # a comment, invisible to every range reader
    write_cuts(path, [(1.0, 2.0)])
    assert read_media_duration(path) is None
    assert read_media_duration(tmp_path / "missing.txt") is None


def test_audio_silence_takes_the_duration_from_ffmpegs_own_line(tmp_path):
    raw = tmp_path / "raw.log"
    raw.write_text(
        "  Duration: 00:01:02.50, start: 0.000000, bitrate: 1 kb/s\n"
        "[silencedetect @ 0x1] silence_start: 0\n",
        encoding="utf-8",
    )
    out = tmp_path / "c_cuts.txt"
    run_audio_silence(out, {"audio_silence": {"enabled": True}}, raw_path=raw)
    assert read_media_duration(out) == 62.5
    assert read_cuts(out) == [(0.0, 62.5)]


# --- "0~" as an op edge -----------------------------------------------------------


def test_0_tilde_is_the_leading_silence():
    op = parse_op({"type": "keep", "lines": ["0~", "0~"]}, 0)
    assert (op.lines, op.gap_start, op.gap_end) == ((0, 0), True, True)
    op = parse_op({"type": "timelapse", "lines": ["0~", 2], "factor": 8.0}, 2)
    assert (op.lines, op.gap_start, op.gap_end) == ((0, 2), True, False)


def test_0_tilde_is_refused_where_it_means_nothing():
    for bad in ([0, 2], ["0~", 0], ["1~", "0~"]):
        assert ops_from_dict({"ops": [{"type": "keep", "lines": bad}]}, 5) == [], bad
    # A segment that starts later has no leading silence of its own.
    assert parse_op({"type": "keep", "lines": ["0~", 31]}, 40, first_line=31) is None


# --- the tools: view / dump / preview / write ---------------------------------------


def test_view_shows_the_leading_silence_and_a_source_with_no_speech(project, capsys):
    out = _run(capsys, "view", "--config", _cfg(project)).out
    assert f"[1] a\n1: {A_LEAD}\n2: aの1行目" in out
    # The gap description is claimed by z's one silence line, not left over.
    assert f"[2] z\n4: {Z_LEAD}" in out
    assert "[silent gap:" not in out
    assert "4 display lines in 2 source(s)" in out
    assert "[2] z: lines 4-4, default 0.0 s" in out


def test_a_source_with_no_known_end_is_shown_without_lines(project, capsys):
    write_cuts(project / "out" / "audio_silence" / "z_cuts.txt", [])  # no duration
    out = _run(capsys, "view", "--config", _cfg(project)).out
    assert "[2] z\n    (no lines)" in out
    assert "3 display lines" in out


EDIT = {
    "plan": "zの組み立てを早送りで見せ、aの冒頭の待ちも残す。",
    "order": [[4, 4], [1, 3]],
    "ops": [
        {"type": "keep", "lines": [1, 1], "note": "the run-up"},
        {"type": "timelapse", "lines": [4, 4], "factor": 5.0, "text": "組み立て"},
    ],
}


def test_write_then_dump_round_trips_the_leading_silence(project, capsys):
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, EDIT))
    director = project / "out" / "director"
    a_ops = json.loads((director / "a_director.json").read_text(encoding="utf-8"))["ops"]
    z_ops = json.loads((director / "z_director.json").read_text(encoding="utf-8"))["ops"]
    assert [op["lines"] for op in a_ops] == [["0~", "0~"]]
    assert [(op["type"], op["lines"]) for op in z_ops] == [("timelapse", ["0~", "0~"])]
    order = json.loads((director / "order.json").read_text(encoding="utf-8"))
    # z plays first, whole; a follows, whole (its leading silence with line 1).
    assert order == {"order": [{"stem": "z"}, {"stem": "a"}]}

    dumped = json.loads(_run(capsys, "dump", "--config", _cfg(project)).out)
    assert dumped["order"] == [[4, 4], [1, 3]]
    assert [(op["type"], op["lines"]) for op in dumped["ops"]] == [
        ("keep", [1, 1]),
        ("timelapse", [4, 4]),
    ]
    assert current_state(load_project(get_effective_config(Path(_cfg(project)), {}))).unmapped == []


def test_preview_prices_what_a_keep_on_the_leading_silence_plays(project, capsys):
    out = _run(
        capsys, "preview", "--config", _cfg(project), "--edit", _edit_file(project, EDIT)
    ).out
    # a: 12.0 s kept before its 4.0 s of speech; z: 42.5 s at x5.
    assert "a: default 4.0 s → with these ops 16.0 s" in out
    assert "z: default 0.0 s → with these ops 8.5 s" in out
    assert "plays 12.0 s of footage in 12.0 s (dropped by default)" in out
    assert "  1x+silence  12.0 s  line 1\n  1x           4.0 s  lines 2-3" in out
    # The caption is listed under the silence line it hangs from.
    assert "  line 4: 「組み立て」 (8.5 s)" in out


def test_a_silence_line_alone_is_still_not_an_order_range(project, capsys):
    bad = dict(EDIT, order=[[1, 1], [2, 4]])
    err = _run(
        capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, bad), code=1
    ).err
    assert "a range holds only a silence line" in err


def test_order_maps_a_speechless_source_to_the_whole_source(project):
    view = load_project(get_effective_config(Path(_cfg(project)), {})).view
    assert order_segments(view, [(4, 4), (1, 3)]) == [Segment("z", None), Segment("a", (1, 2))]
    # Back: line 1 of a takes its leading silence line with it.
    assert order_ranges(view, [Segment("z", None), Segment("a", (1, 2))]) == [(4, 4), (1, 3)]


def test_an_order_may_hold_a_source_with_no_lines_whole():
    assert validate_segments([Segment("z", None), Segment("a", None)], {"a": 2, "z": 0}) == []
    assert validate_segments([Segment("z", (1, 1)), Segment("a", None)], {"a": 2, "z": 0})
    assert validate_segments(
        [Segment("z", None), Segment("z", None), Segment("a", None)], {"a": 2, "z": 0}
    )


# --- guided_edit writes it ----------------------------------------------------------


def _guided(project: Path, stem: str, ops: list[dict]) -> list[str]:
    out = project / "out"
    director = out / "director" / f"{stem}_director.json"
    director.parent.mkdir(parents=True, exist_ok=True)
    director.write_text(json.dumps({"ops": ops}), encoding="utf-8")
    target = out / "guided_edit" / f"{stem}_edits.txt"
    run_guided_edit(
        out / "text_filter" / f"{stem}_edits.txt",
        director,
        target,
        {},
        json_path=out / "sentence_split" / f"{stem}.json",
        gaps_path=out / "gap_context" / f"{stem}_gaps.json",
        cuts_txt=out / "audio_silence" / f"{stem}_cuts.txt",
    )
    return target.read_text(encoding="utf-8").splitlines()


def test_guided_edit_writes_the_leading_silence_line(project):
    assert _guided(project, "a", []) == [A_LEAD, "aの1行目", "aの2行目"]
    assert _guided(project, "z", []) == [Z_LEAD]


def test_guided_edit_lands_an_op_on_0_tilde(project):
    lines = _guided(project, "a", [{"type": "keep", "lines": ["0~", 1]}])
    assert lines == [f"<keep>{A_LEAD}", "aの1行目</keep>", "aの2行目"]
    lines = _guided(project, "z", [{"type": "timelapse", "lines": ["0~", "0~"], "factor": 5.0}])
    assert lines == [f'<keep><speed factor="5.0">{Z_LEAD}</speed></keep>']


def test_without_a_media_end_a_source_with_no_speech_gets_no_line(project):
    write_cuts(project / "out" / "audio_silence" / "z_cuts.txt", [])
    assert _guided(project, "z", [{"type": "keep", "lines": ["0~", "0~"]}]) == []
    assert (project / "out" / "guided_edit" / "z_edits.txt").read_text(encoding="utf-8") == ""


# --- intervals keeps it -------------------------------------------------------------


def _intervals(project: Path, monkeypatch, stem: str, lines: list[str]) -> dict:
    monkeypatch.setattr(intervals_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(intervals_run, "build_bunsetu_times", lambda *a, **k: [])
    out = project / "out"
    edits = out / "guided_edit" / f"{stem}_edits.txt"
    edits.parent.mkdir(parents=True, exist_ok=True)
    edits.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    cfg = {
        **DEFAULTS,
        "intervals": {**DEFAULTS["intervals"], "keep_pre_margin": 0.0, "keep_post_margin": 0.0},
    }
    target = out / "intervals" / f"{stem}_intervals.json"
    run_intervals(
        edits,
        out / "sentence_split" / f"{stem}.json",
        target,
        cfg,
        cuts_txt=out / "audio_silence" / f"{stem}_cuts.txt",
    )
    return json.loads(target.read_text(encoding="utf-8"))


def test_a_source_with_no_speech_plays_nothing_by_default(project, monkeypatch):
    data = _intervals(project, monkeypatch, "z", [Z_LEAD])
    assert data["duration_sec"] == 42.5
    assert data["keep_intervals"] == []


def test_a_keep_on_its_one_silence_line_plays_the_whole_source(project, monkeypatch):
    data = _intervals(project, monkeypatch, "z", [f"<keep>{Z_LEAD}</keep>"])
    assert data["keep_intervals"] == [{"start": 0.0, "end": 42.5}]
    data = _intervals(
        project, monkeypatch, "z", [f'<keep><speed factor="5.0">{Z_LEAD}</speed></keep>']
    )
    assert data["keep_intervals"] == [{"start": 0.0, "end": 42.5}]
    assert data["speed_ranges"] == [{"start": 0.0, "end": 42.5, "factor": 5.0}]


def test_a_keep_on_the_leading_silence_keeps_the_run_up(project, monkeypatch):
    default = _intervals(project, monkeypatch, "a", [A_LEAD, "aの1行目", "aの2行目"])
    assert default["keep_intervals"][0]["start"] == 12.0
    kept = _intervals(project, monkeypatch, "a", [f"<keep>{A_LEAD}</keep>", "aの1行目", "aの2行目"])
    assert kept["keep_intervals"][0]["start"] == 0.0


def test_intervals_refuses_a_leading_line_where_there_is_no_wait(project, monkeypatch):
    # No media end recorded: a source with no speech has no leading silence.
    write_cuts(project / "out" / "audio_silence" / "z_cuts.txt", [])
    with pytest.raises(ValueError, match="unexpected silence line before speech line 1"):
        _intervals(project, monkeypatch, "z", [Z_LEAD])


# --- check_edits --------------------------------------------------------------------


def test_check_edits_accepts_the_leading_silence_line():
    assert check_edits([f"<keep>{A_LEAD}</keep>", "aの1行目", "aの2行目"], A_JSON) == []
    zero = with_media_end(Z_JSON, 42.5)
    assert check_edits([f"<keep>{Z_LEAD}</keep>"], zero) == []
    assert check_edits([], zero) == []  # the legacy shape: no silence lines at all


def test_check_edits_refuses_a_misplaced_leading_silence_line():
    problems = check_edits(["aの1行目", A_LEAD, "aの2行目"], A_JSON)
    assert [p.line for p in problems] == [2]
    assert "unexpected silence line after speech line 1" in problems[0].message
    # Without its media end a source with no speech has no leading silence.
    (problem,) = check_edits([Z_LEAD], Z_JSON)
    assert "unexpected silence line before speech line 1" in problem.message


def test_check_edits_cli_reads_the_media_end_from_the_cut_list(project, capsys):
    out = project / "out"
    edits = project / "z_edits.txt"
    edits.write_text(f"<keep>{Z_LEAD}</keep>\n", encoding="utf-8")
    argv = ["--edits-txt", str(edits), "--json", str(out / "sentence_split" / "z.json")]
    with pytest.raises(SystemExit) as exit_info:
        check_edits_cli.main(argv)
    assert exit_info.value.code == 1
    with pytest.raises(SystemExit) as exit_info:
        check_edits_cli.main([*argv, "--cuts-txt", str(out / "audio_silence" / "z_cuts.txt")])
    assert exit_info.value.code == 0
    assert "no problems found" in capsys.readouterr().out


# --- the manifest -------------------------------------------------------------------


def test_the_manifest_gives_a_speechless_source_its_whole_length():
    segments = [Segment("z", None), Segment("a", (1, 1)), Segment("a", (2, 2))]
    times = {"a": [(12.0, 14.0), (15.0, 17.0)]}
    entries = build_manifest(segments, times, {"a": 17.0, "z": 42.5})
    assert entries == [
        TimelineSegment("z", 0.0, 42.5),
        # The segment starting at line 1 owns the leading silence: it starts at 0.
        TimelineSegment("a", 0.0, 14.0, lines=(1, 1)),
        TimelineSegment("a", 14.0, 17.0, lines=(2, 2)),
    ]


# --- the checkpoint on resume -----------------------------------------------------


def test_resuming_checks_and_applies_a_project_with_a_speechless_source(project, capsys):
    from nagare_clip.pipeline import stages

    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, EDIT))
    ctx = load_project(get_effective_config(Path(_cfg(project)), {})).ctx
    stages._guided_edit_run(ctx)
    assert "Continuing with 2 op(s) (a 1, z 1); a reordered playback" in capsys.readouterr().out
    written = project / "out" / "guided_edit"
    assert (written / "z_edits.txt").read_text(encoding="utf-8").splitlines() == [
        f'<keep><speed factor="5.0"><overlay text="組み立て" duration="8.5"/>{Z_LEAD}'
        "</speed></keep>"
    ]
    assert (written / "a_edits.txt").read_text(encoding="utf-8").splitlines()[0] == (
        f"<keep>{A_LEAD}</keep>"
    )
