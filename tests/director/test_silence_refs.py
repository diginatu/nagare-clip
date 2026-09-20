"""Silence references: ``"n~"`` is the silence after source line n.

A keep/timelapse span resolves to "first word of the first line → last word of
the last line", so the silence after its last line is outside it and dropped.
In three real runs the director wrote ops whose notes said they compressed "the
29.9 s wait after line 53" — which the op could not express at all: ``[53,53]``
drops the wait and ``[53,54]`` destroys line 54's explanation.  ``"53~"`` names
the wait itself.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import ops_from_dict, ops_to_dict


def _one(lines):
    ops = ops_from_dict({"ops": [{"type": "timelapse", "lines": lines, "factor": 5.0}]}, 100)
    assert len(ops) == 1, f"{lines!r} was dropped"
    return ops[0]


def test_a_silence_only_range_keeps_its_line_number_and_marks_both_edges():
    # `lines` stays (53, 53) so every consumer that blocks, clips, sorts or
    # reports by line number keeps working unchanged -- only the resolver that
    # turns an op into times reads the flags.
    op = _one(["53~", "53~"])
    assert op.lines == (53, 53)
    assert (op.gap_start, op.gap_end) == (True, True)


def test_a_line_plus_its_trailing_silence():
    op = _one([53, "53~"])
    assert op.lines == (53, 53)
    assert (op.gap_start, op.gap_end) == (False, True)


def test_a_silence_then_the_lines_after_it():
    op = _one(["53~", 55])
    assert op.lines == (53, 55)
    assert (op.gap_start, op.gap_end) == (True, False)


def test_plain_numbers_are_unchanged():
    op = _one([53, 55])
    assert op.lines == (53, 55)
    assert (op.gap_start, op.gap_end) == (False, False)


def test_a_scalar_line_still_means_that_one_line():
    op = _one(53)
    assert op.lines == (53, 53)
    assert (op.gap_start, op.gap_end) == (False, False)


def test_malformed_silence_references_are_dropped():
    for bad in (["~53", 55], ["0~", "0~"], ["abc~", 55], ["53~~", 55], [53, "52~"], ["5 3~", 6]):
        assert ops_from_dict({"ops": [{"type": "cut", "lines": bad}]}, 100) == [], (
            f"{bad!r} should be dropped"
        )


def test_a_silence_edge_does_not_relax_the_range_checks():
    # The upper bound still applies: "100~" is the silence after the last line
    # of a 100-line transcript, but 101 as a line is still out of range.
    assert ops_from_dict({"ops": [{"type": "cut", "lines": ["100~", 101]}]}, 100) == []


def test_round_trip_writes_the_silence_form_back():
    ops = ops_from_dict(
        {"ops": [{"type": "timelapse", "lines": ["53~", "53~"], "factor": 5.0}]}, 100
    )
    assert ops_to_dict(ops)["ops"][0]["lines"] == ["53~", "53~"]


def test_round_trip_of_a_plain_op_is_byte_identical():
    # A _director.json written before this feature must reparse and reserialise
    # to exactly what it was -- the files are hand-edited and kept in the
    # project, so a silent rewrite would be a real loss.
    before = {"ops": [{"type": "cut", "lines": [12, 18], "note": "why"}]}
    assert ops_to_dict(ops_from_dict(before, 100)) == before
