"""An op addressing a silence is never written as a text marker.

There are no words in a silence to wrap, and a tag placed at a line's edge
falls the wrong way (``intervals/sync_json.py``), so the range would come out
inverted and be dropped.  These ops are resolved to times instead
(``intervals/op_times.py``); guided_edit must leave them alone — while still
letting them block and clip, because they still occupy their lines.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.apply import apply_ops, resolve_span_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops

LINES = ["あいう", "えおか", "きくけ"]


def _applied(ops):
    """apply_ops returns (edit lines, unapplied ops); only the lines matter here."""
    return apply_ops(LINES, ops, {"enabled": False})[0]


def test_a_silence_keep_leaves_no_marker():
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    assert _applied([op]) == LINES


def test_a_silence_timelapse_leaves_no_marker():
    # Expanded first, as guided_edit does: otherwise this passes for the wrong
    # reason, because an unexpanded timelapse is never placed anyway.
    op = DirectorOp(
        type="timelapse", lines=(1, 1), gap_start=True, gap_end=True, factor=5.0, text="待ち"
    )
    seg_times = [(0.5, 1.1), (5.0, 5.6), (6.0, 6.5)]
    expanded = expand_timelapse_ops([op], seg_times)
    assert [o.type for o in expanded] == ["overlay", "speed", "keep"]
    assert _applied(expanded) == LINES


def test_a_plain_keep_still_gets_its_marker():
    # The control: nothing about the silence path may touch ordinary ops.
    op = DirectorOp(type="keep", lines=(1, 1))
    assert "<keep>" in _applied([op])[0]


def test_a_silence_cut_still_gets_its_marker():
    # Cuts stay on the marker path even with a silence edge: keeps are grown
    # back over caption words there, which a time-only cut would lose.
    op = DirectorOp(type="cut", lines=(1, 1), gap_start=True, gap_end=True)
    assert "<cut>" in _applied([op])[0]


def test_a_silence_op_still_blocks_a_cut_from_its_line():
    # It occupies line 1 whether or not it becomes a marker, so a cut over
    # lines 1-2 must still be clipped to line 2 -- otherwise the cut would
    # delete the audio of a span the silence op is keeping.
    keep = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    cut = DirectorOp(type="cut", lines=(1, 2))
    placements = resolve_span_ops(list(LINES), [keep, cut])
    assert placements[1].lines == (2, 2)
