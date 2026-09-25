"""The director sees one SEGMENT, numbered as in the whole source.

Line numbers stay absolute: guided_edit and intervals apply ops to the whole
source file, so an offset introduced here would have to be undone there.
"""

from __future__ import annotations

import json

from nagare_clip.director.director_llm import (
    format_numbered_transcript,
    format_numbered_transcript_timed,
    try_parse_director_response,
)

TIMES = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]


class TestAbsoluteNumbering:
    def test_the_transcript_starts_at_the_segment_first_line(self):
        assert format_numbered_transcript(["あ", "い"], first_line=31) == "31: あ\n32: い"

    def test_numbering_defaults_to_one(self):
        assert format_numbered_transcript(["あ", "い"]) == "1: あ\n2: い"

    def test_the_timed_transcript_starts_there_too(self):
        out = format_numbered_transcript_timed(["あ", "い"], TIMES[:2], first_line=31)
        assert out.splitlines()[0].startswith("31: あ")
        assert out.splitlines()[1].startswith("32: い")


class TestOpRangeValidation:
    def _parse(self, lines, **kw):
        return try_parse_director_response(
            json.dumps({"ops": [{"type": "cut", "lines": lines}]}), **kw
        )

    def test_an_op_inside_the_segment_is_kept(self):
        ops = self._parse([32, 33], num_lines=33, first_line=31)
        assert [op.lines for op in ops] == [(32, 33)]

    def test_an_op_before_the_segment_is_dropped(self):
        # Absolute numbering means a line the segment does not cover is
        # unrepresentable, not something to detect and clip later.
        assert self._parse([30, 32], num_lines=33, first_line=31) == []

    def test_an_op_past_the_segment_is_dropped(self):
        assert self._parse([32, 34], num_lines=33, first_line=31) == []

    def test_without_a_first_line_the_old_bound_still_applies(self):
        assert [op.lines for op in self._parse([1, 2], num_lines=3)] == [(1, 2)]
        assert self._parse([1, 9], num_lines=3) == []
