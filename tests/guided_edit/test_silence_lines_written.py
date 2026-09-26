"""guided_edit writes the director's silence lines, and every op as a marker.

An op with a ``"n~"`` edge used to write nothing (``is_time_resolved``); the
intervals stage resolved it from ``_director.json`` instead, so the edit was
invisible in ``_edits.txt``.  Now the silence line after ``n`` is in the file,
``"n~"`` is an ordinary line index pointing at it, and the marker lands there.
"""

from __future__ import annotations

import json
from pathlib import Path

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.director.display import build_display_view
from nagare_clip.director.run import SegmentInputs, load_segment_transcript
from nagare_clip.edit_lines import parse_edit_lines
from nagare_clip.gap_context.gaps import Gap, gaps_to_dict
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.guided_edit.reconcile import verify_op
from nagare_clip.guided_edit.run import run_guided_edit
from nagare_clip.order import Segment

from ..intervals.silence_equivalence_cases import TEXT_FILTER_LINES, WHISPERX

# Physical layout once written: 1 あい, 2 S1, 3 うえ, 4 おか, 5 S3, 6 きく, 7 S4,
# 8 けこ, 9 さし.
S1, S3, S4 = "[silent 10.0s]", "[silent 8.0s]", "[silent 6.0s]"
SILENCED = ["あい", S1, "うえ", "おか", S3, "きく", S4, "けこ", "さし"]


def _run(tmp_path: Path, ops: list[dict], *, enabled=True, json_path=True, gaps=None, cfg=None):
    (tmp_path / "c.json").write_text(json.dumps(WHISPERX), encoding="utf-8")
    (tmp_path / "tf_edits.txt").write_text("\n".join(TEXT_FILTER_LINES) + "\n", encoding="utf-8")
    (tmp_path / "c_director.json").write_text(json.dumps({"ops": ops}), encoding="utf-8")
    run_guided_edit(
        tmp_path / "tf_edits.txt",
        tmp_path / "c_director.json",
        tmp_path / "c_edits.txt",
        cfg or {"guided_edit": {"enabled": enabled}},
        json_path=(tmp_path / "c.json") if json_path else None,
        gaps_path=gaps,
    )
    return (tmp_path / "c_edits.txt").read_text(encoding="utf-8").splitlines()


def test_disabled_still_writes_the_silence_lines(tmp_path):
    assert _run(tmp_path, [{"type": "cut", "lines": [1, 1]}], enabled=False) == SILENCED


def test_without_timings_there_are_no_silence_lines(tmp_path):
    assert _run(tmp_path, [], enabled=False, json_path=False) == TEXT_FILTER_LINES


def test_the_threshold_is_director_silence_line_min(tmp_path):
    cfg = {"guided_edit": {"enabled": False}, "director": {"silence_line_min": 7.0}}
    assert S4 not in _run(tmp_path, [], cfg=cfg)


def test_a_keep_on_one_silence_is_a_marker_on_its_line(tmp_path):
    out = _run(tmp_path, [{"type": "keep", "lines": ["1~", "1~"]}])
    assert out[1] == f"<keep>{S1}</keep>"
    assert [ln for i, ln in enumerate(out) if i != 1] == [
        ln for i, ln in enumerate(SILENCED) if i != 1
    ]


def test_a_timelapse_from_a_silence_opens_on_the_silence_line(tmp_path):
    ops = [{"type": "timelapse", "lines": ["1~", 3], "factor": 7.0, "text": "作業"}]
    out = _run(tmp_path, ops)
    assert out[1] == f'<keep><speed factor="7.0"><overlay text="作業" duration="1.7"/>{S1}'
    assert out[3] == "おか</speed></keep>"
    assert out[0] == "あい"


def test_a_cut_from_a_silence_leaves_the_line_before_it_alone(tmp_path):
    out = _run(tmp_path, [{"type": "cut", "lines": ["1~", 3]}])
    assert out[:4] == ["あい", f"<cut>{S1}", "うえ", "おか</cut>"]


def test_every_op_is_in_the_file(tmp_path):
    ops = [
        {"type": "keep", "lines": ["3~", "4~"]},
        {"type": "speed", "lines": ["3~", "4~"], "factor": 5.0},
        {"type": "overlay", "lines": ["3~", "3~"], "text": "待ち", "duration": 2.0},
    ]
    out = _run(tmp_path, ops)
    # Each op prepends its opener in application order, so the last is outermost.
    assert out[4] == f'<overlay text="待ち" duration="2.0"/><speed factor="5.0"><keep>{S3}'
    assert out[6] == f"{S4}</keep></speed>"


def test_a_silence_with_no_line_is_not_applied(caplog):
    # 0.5 s after line 2: shorter than silence_line_min, so no line to hold it.
    op = DirectorOp(type="keep", lines=(2, 2), gap_start=True, gap_end=True)
    lines, unapplied = apply_ops(SILENCED, [op], {})
    assert lines == SILENCED
    ((dropped, reason),) = unapplied
    assert dropped is op
    assert "silence after line 2" in reason and "no silence line" in reason


def test_an_edit_op_cannot_address_a_silence():
    op = DirectorOp(type="edit", lines=(1, 1), gap_start=True, gap_end=True)
    _, unapplied = apply_ops(SILENCED, [op], {}, call_llm=lambda *a: "")
    ((_, reason),) = unapplied
    assert "cannot address a silence" in reason


def test_a_clip_never_ends_on_a_silence_the_op_did_not_address():
    # keep [2, 4] is physical 3..6; line 4 (physical 6) already holds a keep,
    # so the clip stops at physical 5 — the silence after 3 — and is trimmed
    # back to line 3, exactly where it stopped before silence lines existed.
    lines = list(SILENCED)
    lines[5] = "<keep>きく</keep>"
    op = DirectorOp(type="keep", lines=(2, 4))
    out, unapplied = apply_ops(lines, [op], {})
    assert unapplied == []
    assert out[2:6] == ["<keep>うえ", "おか</keep>", S3, "<keep>きく</keep>"]


def test_an_overlay_skips_a_silence_it_did_not_address():
    lines = list(SILENCED)
    lines[3] = '<overlay text="a" duration="1.0"/>おか'
    op = DirectorOp(type="overlay", lines=(3, 4), text="b", duration=1.0)
    out, _ = apply_ops(lines, [op], {})
    assert out[4] == S3
    assert out[5] == '<overlay text="b" duration="1.0"/>きく'


def test_a_line_before_a_kept_silence_can_now_be_cut(tmp_path):
    # Spec §7: the old path held line 1 for keep ["1~", 3] (it "occupied" its
    # whole lines), so cut [1, 1] was dropped.  The keep starts AFTER line 1.
    ops = [{"type": "keep", "lines": ["1~", 3]}, {"type": "cut", "lines": [1, 1]}]
    out = _run(tmp_path, ops)
    assert out[:2] == ["<cut>あい</cut>", f"<keep>{S1}"]


def test_verify_refuses_a_changed_silence_structure():
    op = DirectorOp(type="keep", lines=(3, 3))
    after = [ln for ln in SILENCED if ln != S3]
    after[2] = "<keep>うえ</keep>"
    assert "silence line" in (verify_op(SILENCED, after, op) or "")


def test_the_written_line_is_the_display_line(tmp_path):
    gaps = tmp_path / "c_gaps.json"
    gaps.write_text(
        json.dumps(gaps_to_dict([Gap(2.0, 10.0, [], "a hand ] enters", static=False)])),
        encoding="utf-8",
    )
    out = _run(tmp_path, [], enabled=False, gaps=gaps)
    transcript = load_segment_transcript(
        SegmentInputs(Segment("c", None), tmp_path / "tf_edits.txt", tmp_path / "c.json", gaps)
    )
    view = build_display_view([(Segment("c", None), transcript)])
    shown = [line.text for line in view.lines if line.is_silence]
    written = [slot.text for slot in parse_edit_lines(out).silences()]
    assert written == shown
    assert written[0] == "[silent 10.0s: a hand \\] enters]"


def test_a_description_mentioning_a_tag_does_not_block_an_op():
    lines = list(SILENCED)
    lines[1] = "[silent 10.0s: a <keep> sign]"
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    out, unapplied = apply_ops(lines, [op], {})
    assert unapplied == []
    assert out[1] == "<keep>[silent 10.0s: a <keep> sign]</keep>"


def test_a_tag_in_a_description_does_not_count_as_the_op_landing():
    lines = list(SILENCED)
    lines[1] = "[silent 10.0s: <keep> and </keep>]"
    op = DirectorOp(type="keep", lines=(2, 2))  # physical: the silence line
    assert "opening tag missing" in (verify_op(lines, lines, op) or "")
