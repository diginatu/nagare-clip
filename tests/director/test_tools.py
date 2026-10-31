"""``python -m nagare_clip.director.tools``: view / dump / preview / write.

The fixture project has two sources.  ``a`` has a 28 s wait after its first
line, which the view shows as a silence line of its own:

    [1] a                 [2] b
    1: aの1行目           5: bの1行目
    2: [silent …]         6: bの2行目
    3: aの2行目
    4: aの3行目
"""

from __future__ import annotations

import json

import pytest
import yaml

from nagare_clip.director import director_llm, loop
from nagare_clip.director.tools.cli import main
from nagare_clip.director.tools.project import load_project
from nagare_clip.director.tools.validate import to_source, validate_edit
from nagare_clip.order import Segment, order_from_dict

TIMES = {
    "a": [(0.0, 2.0), (30.0, 34.0), (35.0, 40.0)],
    "b": [(0.0, 3.0), (4.0, 6.0)],
}
PLAN = "aの作業を早送りし、bを先に見せる。"


@pytest.fixture
def project(tmp_path):
    out = tmp_path / "out"
    (tmp_path / "in").mkdir()
    for sub in ("text_filter", "sentence_split", "summary", "gap_context"):
        (out / sub).mkdir(parents=True)
    for stem, times in TIMES.items():
        (tmp_path / "in" / f"{stem}.mp4").touch()
        (out / "text_filter" / f"{stem}_edits.txt").write_text(
            "\n".join(f"{stem}の{i}行目" for i in range(1, len(times) + 1)) + "\n",
            encoding="utf-8",
        )
        segs = [
            {
                "start": s,
                "end": e,
                "text": f"{stem}{i}",
                "words": [{"word": "w", "start": s, "end": e}],
            }
            for i, (s, e) in enumerate(times, start=1)
        ]
        (out / "sentence_split" / f"{stem}.json").write_text(
            json.dumps({"segments": segs}), encoding="utf-8"
        )
    (out / "summary" / "summary.json").write_text(
        json.dumps(
            {
                "summary": "全体の要約",
                "parts": [],
                "keywords": {},
                "video_summaries": {"b": "bの要約"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = tmp_path / "cfg.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "pipeline": {"input_videos_dir": str(tmp_path / "in"), "output_dir": str(out)},
                "director": {"max_keep_lines": 2},
                "project": {"audience": "makers"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _cfg(root):
    return str(root / "cfg.yml")


def _director(root):
    return root / "out" / "director"


def _listing(root):
    return sorted((p.relative_to(root), p.stat().st_mtime_ns) for p in root.rglob("*"))


def _edit_file(root, data, name="edit.json"):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _run(capsys, *argv, code=0):
    assert main(list(argv)) == code
    return capsys.readouterr()


GOOD = {
    "plan": PLAN,
    "order": [[5, 6], [1, 4]],
    "ops": [
        {"type": "timelapse", "lines": [1, 3], "factor": 6.0, "text": "作業中", "note": "wait"},
        {"type": "overlay", "lines": [5, 5], "text": "結果", "duration": 2.0, "note": ""},
        {"type": "cut", "lines": [4, 4], "note": "redundant"},
    ],
}


# --- view --------------------------------------------------------------------


def test_view_prints_brief_context_and_numbered_transcript(project, capsys):
    out = _run(capsys, "view", "--config", _cfg(project)).out
    assert "- Audience: makers" in out
    assert 'A "keep" op may span at most 2 line(s)' in out
    assert "Overall: 全体の要約" in out and "[2] b: bの要約" in out
    assert "6 display lines in 2 source(s)" in out
    assert "[1] a: lines 1-4" in out and "[2] b: lines 5-6" in out
    assert "1: aの1行目  [2.0s]" in out
    assert "2: [silent " in out
    assert "6: bの2行目" in out


def test_view_lines_pages_and_no_context_drops_the_prefix(project, capsys):
    out = _run(capsys, "view", "--config", _cfg(project), "--lines", "3-5", "--no-context").out
    assert "Audience" not in out and "Overall" not in out
    assert "Showing lines 3-5 of 6." in out
    assert "1: aの1行目" not in out and "6: bの2行目" not in out
    assert "3: aの2行目" in out and "5: bの1行目" in out
    assert "[1] a\n3:" in out and "[2] b\n5:" in out


def test_view_without_text_filter_output_says_what_to_run(project, capsys):
    (project / "out" / "text_filter" / "b_edits.txt").unlink()
    err = _run(capsys, "view", "--config", _cfg(project), code=2).err
    assert "--to-stage text_filter" in err


def test_annotation_lines_have_no_number(project, capsys):
    # A described gap INSIDE a line is an indented, un-numbered annotation:
    # no display number names it, so no op can address it.
    (project / "out" / "gap_context" / "a_gaps.json").write_text(
        json.dumps({"gaps": [{"start": 35.5, "end": 39.5, "description": "手元のアップ"}]}),
        encoding="utf-8",
    )
    out = _run(capsys, "view", "--config", _cfg(project), "--no-context").out
    assert "    [silent gap: 手元のアップ]" in out
    assert "6 display lines" in out


# --- write: validation ---------------------------------------------------------


def test_write_refuses_everything_at_once_and_writes_nothing(project, capsys):
    bad = {
        "order": [[5, 6], [1, 3]],
        "extra": 1,
        "ops": [
            {"type": "speed", "lines": [1, 1], "factor": 2},
            {"type": "timelapse", "lines": [1, 3], "factor": 2.0, "text": "x"},
            {"type": "timelapse", "lines": [1, 3], "factor": 6.0, "text": "x"},
            {"type": "keep", "lines": [3, 6]},
            {"type": "overlay", "lines": [1, 1], "text": " ", "duration": 2},
            {"type": "overlay", "lines": [1, 1], "text": "ok"},
            {"type": "cut", "lines": [3, 4]},
            {"type": "edit", "lines": [2, 2], "note": "x"},
            {"type": "cut", "lines": [1, "2~"]},
            "x",
            {"type": "keep", "lines": [1, 4]},
            {"type": "cut", "lines": [7, 7]},
        ],
    }
    path = _edit_file(project, bad)
    before = _listing(project)
    err = _run(capsys, "write", "--config", _cfg(project), "--edit", path, code=1).err
    assert not _director(project).exists()
    assert "nothing written" in err
    expected = [
        'unknown key "extra"',
        "plan: there is no plan yet",
        'order: "order" must cover every line exactly once — use cut to drop footage: lines 4-4 are in no range.',
        "op #1 (speed [1, 1]): 'speed' op [1, 1] is not an operation the director may emit",
        "op #2 (timelapse [1, 3]): timelapse factor 2.0 < 4.0",
        "op #4 (keep [3, 6]): keep op spans 4 lines [3, 6] > max_keep_lines=2",
        "op #4 (keep [3, 6]): keep [3,6] crosses the segment join after line 4",
        "op #5 (overlay [1, 1]): overlay op text is blank",
        "op #6 (overlay [1, 1]): overlay op missing duration",
        "op #7 (cut [3, 4]): overlaps op #3 (timelapse [1, 3])",
        "op #8 (edit [2, 2]): line 2 is a silence line",
        "op #9 (cut [1, '2~']): cut op lines [1, 2] use the \"n~\" form",
        "op #10: not a JSON object",
        "op #11 (keep [1, 4]): keep op spans 4 lines [1, 4] > max_keep_lines=2",
        "op #12 (cut [7, 7]): bad lines [7, 7] — give [first, last] display line numbers within 1-6",
    ]
    lines = [line.strip() for line in err.splitlines()[1:]]
    assert len(lines) == len(expected)
    for line, want in zip(lines, expected):
        assert want in line, (want, line)
    assert before == _listing(project)


def test_order_with_only_a_silence_line_range_is_refused(project, capsys):
    edit = dict(GOOD, order=[[2, 2], [1, 1], [3, 6]], ops=[])
    err = _run(
        capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, edit), code=1
    ).err
    assert "a range holds only a silence line" in err


def test_timelapse_breaks_are_checked_against_the_edits_own_order(project, capsys):
    # The order breaks after line 3 (a plays 4, then 1-3): a timelapse over it is refused.
    edit = {
        "plan": PLAN,
        "order": [[4, 4], [1, 3], [5, 6]],
        "ops": [{"type": "timelapse", "lines": [2, 4], "factor": 5.0}],
    }
    err = _run(
        capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, edit), code=1
    ).err
    assert "timelapse [2,4] crosses the order's break after line 3" in err


def test_the_order_on_disk_does_not_feed_validation(project, capsys):
    # A reorder on disk, then an edit without "order": that edit is shooting
    # order, so the same timelapse is fine — and order.json is reset.
    first = {"plan": PLAN, "order": [[4, 4], [1, 3], [5, 6]], "ops": []}
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, first))
    second = {"ops": [{"type": "timelapse", "lines": [2, 4], "factor": 5.0}]}
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, second))
    order = json.loads((_director(project) / "order.json").read_text(encoding="utf-8"))
    assert order == {"order": [{"stem": "a"}, {"stem": "b"}]}


def test_touching_keeps_count_as_one_against_the_cap(project):
    view = load_project(_load(project)).view
    ops = [
        {"type": "keep", "lines": [1, 2]},
        {"type": "keep", "lines": [3, 4]},
        {"type": "keep", "lines": [5, 6]},
    ]
    _edit, problems = validate_edit(
        view, {"plan": PLAN, "ops": ops}, has_plan=False, max_keep_lines=2
    )
    # a's chain (1-4) is too wide; b's keep (5-6) is another source, so the
    # source boundary ends the chain even though the numbers touch.
    assert [str(p) for p in problems] == [
        "op #1 (keep chain [1, 4]): keeps #1, #2 touch and together span 4 lines > "
        "max_keep_lines=2; consecutive keeps count as one. Cover an on-screen event "
        "with ONE keep; for one wider than the cap, ask the user to raise "
        "director.max_keep_lines (or, for manual work, consider a timelapse) — never "
        "chain keeps around it"
    ]


def test_separated_keeps_and_a_chain_within_the_cap_are_fine(project):
    view = load_project(_load(project)).view
    for ops, cap in (
        ([{"type": "keep", "lines": [1, 1]}, {"type": "keep", "lines": [3, 3]}], 2),
        ([{"type": "keep", "lines": [1, 1]}, {"type": "keep", "lines": [2, 3]}], 3),
        ([{"type": "keep", "lines": [1, 2]}, {"type": "keep", "lines": [3, 4]}], 0),
    ):
        _edit, problems = validate_edit(
            view, {"plan": PLAN, "ops": ops}, has_plan=False, max_keep_lines=cap
        )
        assert problems == [], (ops, cap)


def test_view_states_the_skill_keep_rule(project, capsys):
    out = _run(capsys, "view", "--config", _cfg(project)).out
    assert "keeps that touch or overlap count as one keep" in out
    assert "consecutive keeps that each stay within the cap" not in out


def test_two_overlapping_cuts_are_reported_once(project):
    view = load_project(_load(project)).view
    _edit, problems = validate_edit(
        view,
        {"plan": PLAN, "ops": [{"type": "cut", "lines": [1, 3]}, {"type": "cut", "lines": [3, 4]}]},
        has_plan=False,
    )
    assert [str(p) for p in problems] == [
        "op #1 (cut [1, 3]): overlaps op #2 (cut [3, 4]); a cut range must not overlap any "
        "other op's range — shorten the cut so the other op's lines are outside it"
    ]


def test_adjacent_cut_and_keep_are_fine(project):
    view = load_project(_load(project)).view
    _edit, problems = validate_edit(
        view,
        {
            "plan": PLAN,
            "ops": [{"type": "cut", "lines": [1, 1]}, {"type": "keep", "lines": [2, 3]}],
        },
        has_plan=False,
    )
    assert problems == []


def _load(root):
    from nagare_clip.config import get_effective_config

    return get_effective_config([root / "cfg.yml"], {})


# --- write: output + round trip -----------------------------------------------


def test_write_converts_to_source_coordinates(project, capsys):
    out = _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, GOOD)).out
    assert "--from-stage guided_edit" in out
    d = _director(project)
    a = json.loads((d / "a_director.json").read_text(encoding="utf-8"))["ops"]
    b = json.loads((d / "b_director.json").read_text(encoding="utf-8"))["ops"]
    # Display 1-3 is a's line 1, its silence, line 2: source [1, 2].
    assert [(o["type"], o["lines"]) for o in a] == [("timelapse", [1, 2]), ("cut", [3, 3])]
    assert [(o["type"], o["lines"]) for o in b] == [("overlay", [1, 1])]
    order = order_from_dict(json.loads((d / "order.json").read_text(encoding="utf-8")))
    assert order == [Segment("b", None), Segment("a", None)]
    assert (d / "plan.md").read_text(encoding="utf-8") == f"# The director's plan\n\n{PLAN}\n"


def test_silence_edges_become_n_tilde(project, capsys):
    edit = {
        "plan": PLAN,
        "order": [[3, 4], [1, 2], [5, 6]],
        "ops": [{"type": "keep", "lines": [2, 2], "note": "the wait"}],
    }
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, edit))
    d = _director(project)
    a = json.loads((d / "a_director.json").read_text(encoding="utf-8"))["ops"]
    assert a[0]["lines"] == ["1~", "1~"]
    order = json.loads((d / "order.json").read_text(encoding="utf-8"))["order"]
    assert order == [
        {"stem": "a", "lines": [2, 3]},
        {"stem": "a", "lines": [1, "1~"]},
        {"stem": "b"},
    ]


@pytest.mark.parametrize(
    "edit",
    [
        GOOD,
        {
            "plan": PLAN,
            "order": [[3, 4], [1, 2], [5, 6]],
            "ops": [
                {"type": "keep", "lines": [2, 2], "note": "w"},
                {"type": "edit", "lines": [3, 3], "note": "drop の"},
                {"type": "timelapse", "lines": [5, 6], "factor": 4.0, "text": "a\nb", "note": ""},
            ],
        },
    ],
)
def test_write_then_dump_round_trips(project, capsys, edit):
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, edit))
    dumped = json.loads(_run(capsys, "dump", "--config", _cfg(project)).out)
    want = sorted(edit["ops"], key=lambda o: o["lines"])
    for op in want:
        op.setdefault("note", "")
    assert dumped["plan"] == edit["plan"]
    assert dumped["order"] == edit["order"]
    assert dumped["ops"] == want


def test_display_source_round_trip_every_range(project):
    # Every single-segment range maps to source and back to itself.
    proj = load_project(_load(project))
    view = proj.view
    from nagare_clip.director.tools.validate import Edit, to_display

    total = len(view.lines)
    for a in range(1, total + 1):
        for b in range(a, total + 1):
            if view.to_source(a, b) is None:
                assert loop.segment_joins(view, a, b)
                continue
            op = director_llm.DirectorOp(type="cut", lines=(a, b))
            index, (src,) = next(iter(to_source(view, Edit(ops=[((a, b), op)])).items()))
            assert to_display(view, index, src)["lines"] == [a, b]


def test_write_without_plan_keeps_it_and_without_order_resets_it(project, capsys):
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, GOOD))
    d = _director(project)
    plan = (d / "plan.md").read_bytes()
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, {"ops": []}))
    assert (d / "plan.md").read_bytes() == plan
    order = json.loads((d / "order.json").read_text(encoding="utf-8"))
    assert order == {"order": [{"stem": "a"}, {"stem": "b"}]}
    assert json.loads((d / "a_director.json").read_text(encoding="utf-8")) == {"ops": []}


def test_write_is_idempotent(project, capsys):
    path = _edit_file(project, GOOD)
    _run(capsys, "write", "--config", _cfg(project), "--edit", path)
    d = _director(project)
    first = {p.name: p.read_bytes() for p in d.iterdir()}
    _run(capsys, "write", "--config", _cfg(project), "--edit", path)
    assert {p.name: p.read_bytes() for p in d.iterdir()} == first


def test_dump_keeps_a_reorder_so_revising_from_it_preserves_it(project, capsys):
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, GOOD))
    dumped = _run(capsys, "dump", "--config", _cfg(project)).out
    before = (_director(project) / "order.json").read_bytes()
    path = project / "dumped.json"
    path.write_text(dumped, encoding="utf-8")
    _run(capsys, "write", "--config", _cfg(project), "--edit", str(path))
    assert (_director(project) / "order.json").read_bytes() == before


def test_write_reads_stdin(project, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(GOOD, ensure_ascii=False)))
    _run(capsys, "write", "--config", _cfg(project), "--edit", "-")
    assert (_director(project) / "plan.md").is_file()


# --- preview -------------------------------------------------------------------


def test_preview_candidate_writes_nothing(project, capsys):
    before = _listing(project)
    out = _run(
        capsys, "preview", "--config", _cfg(project), "--edit", _edit_file(project, GOOD)
    ).out
    assert PLAN in out
    assert "timelapse [1,3] x6.0 「作業中」" in out
    assert "order: 5-6, 1-4" in out
    assert "whole video so far: default 6.5 s" in out
    after = [row for row in _listing(project) if row[0].name != "edit.json"]
    assert after == [row for row in before if row[0].name != "edit.json"]


def test_preview_candidate_with_problems_exits_1_and_shows_valid_ops(project, capsys):
    edit = dict(GOOD, ops=[*GOOD["ops"], {"type": "speed", "lines": [6, 6], "factor": 2}])
    res = _run(
        capsys, "preview", "--config", _cfg(project), "--edit", _edit_file(project, edit), code=1
    )
    assert "timelapse [1,3]" in res.out
    assert "'speed' op [6, 6] is not an operation" in res.err


def test_preview_of_files_on_disk(project, capsys):
    _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, GOOD))
    out = _run(capsys, "preview", "--config", _cfg(project)).out
    assert "timelapse [1,3] x6.0 「作業中」" in out and "order: 5-6, 1-4" in out


# --- shared with the LLM director ------------------------------------------------


def test_the_llm_director_drops_what_write_refuses_with_the_same_reason(project):
    """One implementation: the parser's drop message is the tool's reason."""
    view = load_project(_load(project)).view
    ops = [
        {"type": "speed", "lines": [1, 1], "factor": 2.0},
        {"type": "timelapse", "lines": [1, 1], "factor": 3.0},
        {"type": "keep", "lines": [1, 4]},
        {"type": "overlay", "lines": [1, 1], "text": "x", "duration": 0},
    ]
    drops: list[str] = []
    director_llm.try_parse_director_response(json.dumps({"ops": ops}), len(view.lines), drops, 2)
    _edit, problems = validate_edit(
        view, {"plan": PLAN, "ops": ops}, has_plan=False, max_keep_lines=2
    )
    assert sorted(drops) == sorted(p.reason for p in problems)


def test_write_warns_when_guided_edit_would_not_apply_the_ops(project, capsys):
    err = _run(capsys, "write", "--config", _cfg(project), "--edit", _edit_file(project, GOOD)).err
    assert "guided_edit.enabled is false" in err
    on = project / "on.yml"
    on.write_text("guided_edit:\n  enabled: true\n", encoding="utf-8")
    err = _run(
        capsys,
        "write",
        "--config",
        _cfg(project),
        "--config",
        str(on),
        "--edit",
        _edit_file(project, GOOD),
    ).err
    assert "guided_edit.enabled" not in err


def test_keeps_separated_by_a_speech_line_do_not_chain(project):
    """A checker property, not a recommended route: the skill covers an event
    with ONE keep, and asks the user to raise the cap for a wider one."""
    # A second silence line in b: a 10 s wait between its two lines.
    root = project
    data = {
        "segments": [
            {
                "start": 0.0,
                "end": 3.0,
                "text": "bの1行目",
                "words": [{"word": "w", "start": 0.0, "end": 3.0}],
            },
            {
                "start": 13.0,
                "end": 15.0,
                "text": "bの2行目",
                "words": [{"word": "w", "start": 13.0, "end": 15.0}],
            },
            {
                "start": 40.0,
                "end": 41.0,
                "text": "bの3行目",
                "words": [{"word": "w", "start": 40.0, "end": 41.0}],
            },
        ]
    }
    (root / "out" / "sentence_split" / "b.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "out" / "text_filter" / "b_edits.txt").write_text(
        "bの1行目\nbの2行目\nbの3行目\n", encoding="utf-8"
    )
    view = load_project(_load(root)).view
    silence = [line.number for line in view.lines if line.is_silence and line.stem == "b"]
    assert len(silence) == 2 and silence[1] - silence[0] == 2  # a speech line between
    ops = [{"type": "keep", "lines": [n, n]} for n in silence]
    _edit, problems = validate_edit(
        view, {"plan": PLAN, "ops": ops}, has_plan=False, max_keep_lines=1
    )
    assert problems == []
