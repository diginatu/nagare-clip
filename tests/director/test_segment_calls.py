"""The director sees one SEGMENT, numbered as in the whole source.

Line numbers stay absolute: guided_edit and intervals apply ops to the whole
source file, so an offset introduced here would have to be undone there.
"""

from __future__ import annotations

import json

from nagare_clip.director.director_llm import (
    format_numbered_transcript,
    format_numbered_transcript_timed,
    generate_director_ops,
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


class TestDirectorResult:
    def _run(self, call_llm, **kw):
        return generate_director_ops(
            ["あ"], {"prompt": "P", "max_retries": 0}, call_llm=call_llm, **kw
        )

    def test_a_valid_response_is_ok(self):
        result = self._run(lambda m, c: '{"ops": [{"type": "cut", "lines": [1, 1]}]}')
        assert result.ok is True
        assert len(result.ops) == 1

    def test_an_empty_op_list_is_a_legitimate_success(self):
        # "No edits" is a real answer, and must stay distinguishable from
        # "the call failed" -- that distinction is what lets a failure be fatal.
        result = self._run(lambda m, c: '{"ops": []}')
        assert result.ok is True
        assert result.ops == []

    def test_an_llm_error_is_not_ok(self):
        def boom(messages, cfg):
            raise RuntimeError("nope")

        result = self._run(boom)
        assert result.ok is False
        assert result.ops == []

    def test_an_unparseable_response_is_not_ok(self):
        assert self._run(lambda m, c: "not json").ok is False


class TestSegmentPrompt:
    def test_the_user_content_is_numbered_from_the_segment_start(self):
        seen = {}

        def fake(messages, cfg):
            seen["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(
            ["あ", "い"], {"prompt": "P", "max_retries": 0}, call_llm=fake, first_line=31
        )
        assert seen["user"] == "31: あ\n32: い"


class TestSegmentOpBounds:
    """The bound generate_director_ops derives is the ABSOLUTE last line."""

    def _ops(self, lines):
        return generate_director_ops(
            ["あ", "い", "う"],
            {"prompt": "P", "max_retries": 0},
            call_llm=lambda m, c: json.dumps({"ops": [{"type": "cut", "lines": lines}]}),
            first_line=31,
        ).ops

    def test_an_op_on_the_segments_last_line_survives(self):
        # With the bound taken as the slice length (3) instead of line 33, every
        # op in a segment starting at 31 would be dropped.
        assert [op.lines for op in self._ops([33, 33])] == [(33, 33)]

    def test_an_op_past_the_segments_last_line_is_dropped(self):
        assert self._ops([33, 34]) == []
