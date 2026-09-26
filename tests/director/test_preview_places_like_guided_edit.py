"""The preview places silence ops exactly as guided_edit writes them.

guided_edit now writes a ``"n~"`` op as a marker on the silence line after
``n``; it can be clipped and it blocks others by the lines it really holds.
The preview used to model those ops as holding their whole lines, unclipped
(``is_time_resolved``); it now places them through the same
:func:`resolve_span_ops` on the same silence-lined file.
"""

from __future__ import annotations

import pytest

from nagare_clip.director.director_llm import DirectorOp, ops_from_dict
from nagare_clip.director.preview import preview_segment, resolve_placements
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.edit_lines import gap_spans, insert_silence_lines
from nagare_clip.guided_edit.apply import apply_ops, resolve_span_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops
from nagare_clip.timing import segment_times

from ..intervals.silence_equivalence_cases import CASES, TEXT_FILTER_LINES, WHISPERX
from .test_preview import WATER, WATER_FIRST, _block

SPANS = gap_spans(WHISPERX)
SHOWN = {n: f"[silent {e - s:.1f}s]" for n, (s, e) in SPANS.items() if e - s >= 5.0}
SILENCED = insert_silence_lines(TEXT_FILTER_LINES, SHOWN)
SEG_TIMES = segment_times(WHISPERX)
SILENCE_LINES = [SilenceLine(n, *SPANS[n]) for n in sorted(SHOWN)]


def _expanded(ops):
    return expand_timelapse_ops(ops_from_dict({"ops": ops}, None), SEG_TIMES, SPANS)


@pytest.mark.parametrize("name", sorted(CASES))
def test_resolve_span_ops_lands_what_apply_ops_lands(name):
    ops = [op for op in _expanded(CASES[name]) if op.type != "edit"]
    applied, _ = apply_ops(SILENCED, ops, {})
    placed = resolve_span_ops(SILENCED, ops)
    last = SILENCED
    for i in sorted(placed, key=lambda i: (ops[i].type == "cut", i)):
        if placed[i].applied:
            last = placed[i].candidate
    assert last == applied


def _placements(ops):
    return resolve_placements(TEXT_FILTER_LINES, ops, SEG_TIMES, silence_lines=SILENCE_LINES)


def test_a_silence_op_is_placed_in_source_coordinates():
    op = DirectorOp(type="keep", lines=(1, 3), gap_start=True)
    ((p,),) = _placements([op])
    assert (p.kind, p.lines, p.op.gap_start, p.op.gap_end) == ("keep", (1, 3), True, False)


def test_a_silence_op_holds_only_what_it_marks():
    # keep ["1~", 3] starts AFTER line 1, so a cut of line 1 lands — the old
    # model had the keep occupy line 1 and dropped the cut.
    keep = DirectorOp(type="keep", lines=(1, 3), gap_start=True)
    cut = DirectorOp(type="cut", lines=(1, 1))
    _, (p_cut,) = _placements([keep, cut])
    assert p_cut.lines == (1, 1)


def test_a_silence_with_no_line_is_not_applied():
    op = DirectorOp(type="keep", lines=(2, 2), gap_start=True, gap_end=True)
    ((p,),) = _placements([op])
    assert p.lines is None
    assert "no silence line" in (p.reason or "")


def test_a_second_keep_on_the_same_silence_is_not_applied():
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    (_, (second,)) = _placements([op, op])
    assert second.lines is None


ROWS = WATER[:6]
SILENCE = SilenceLine(WATER_FIRST + 2, ROWS[2][1], ROWS[3][0])  # after the 3rd row


def _water(ops):
    return preview_segment(
        [r[3] for r in ROWS],
        ops,
        seg_times=[(r[0], r[1]) for r in ROWS],
        silences=[r[2] for r in ROWS],
        first_line=WATER_FIRST,
        silence_lines=[SILENCE],
    )


def test_a_cut_from_a_silence_removes_only_the_lines_after_it():
    first = WATER_FIRST
    op = DirectorOp(type="cut", lines=(first + 2, first + 4), gap_start=True)
    preview = _water([op])
    block = _block(preview.text, f"cut [{first + 2},{first + 4}]")
    assert f"removes lines {first + 3}-{first + 4}" in block
    cut_runs = [run.lines for run in preview.runs if run.kind == "cut"]
    assert cut_runs == [[first + 3, first + 4]]


def test_a_refused_silence_op_says_so():
    first = WATER_FIRST
    op = DirectorOp(type="keep", lines=(first + 2, first + 2), gap_start=True, gap_end=True)
    text = _water([op, op]).text
    blocks = text.split(f"keep [{first + 2},{first + 2}]")
    assert "not applied: keep op fully overlaps" in blocks[2]
