"""The director op contract: parse_op/ops_from_dict/ops_to_dict and the transcript helpers."""

from __future__ import annotations

import json

from nagare_clip.director.ops import (
    TIMELAPSE_MIN_FACTOR,
    DirectorOp,
    ops_from_dict,
    ops_to_dict,
    parse_op,
)
from nagare_clip.director.transcript import (
    clean_for_display,
    format_numbered_transcript,
    format_numbered_transcript_timed,
)


def _parse(response, num_lines, *_args, **_kw):
    """Every op of a ``{"ops": [...]}`` document, as ops_from_dict reads it."""
    return ops_from_dict(json.loads(response), num_lines)


class TestParseValid:
    def test_parses_all_op_types(self):
        resp = """{"ops": [
          {"type": "cut", "lines": [2, 4], "note": "boring"},
          {"type": "timelapse", "lines": [5, 6], "factor": 8.0, "note": ""},
          {"type": "overlay", "lines": [1, 1], "text": "ポイント", "duration": 2.5},
          {"type": "keep", "lines": [7, 8]},
          {"type": "edit", "lines": [3, 3], "old": "x", "new": ""}
        ]}"""
        ops = _parse(resp, num_lines=10)
        assert [o.type for o in ops] == ["cut", "timelapse", "overlay", "keep", "edit"]
        assert ops[0] == DirectorOp(type="cut", lines=(2, 4), note="boring")
        assert ops[1].factor == 8.0
        assert ops[2].text == "ポイント"
        assert ops[2].duration == 2.5

    def test_single_int_line_becomes_range(self):
        resp = '{"ops": [{"type": "keep", "lines": 3}]}'
        ops = _parse(resp, num_lines=5)
        assert ops[0].lines == (3, 3)


class TestParseInvalidDropped:
    def test_unknown_type_dropped(self):
        resp = (
            '{"ops": [{"type": "frobnicate", "lines": [1, 2]}, {"type": "cut", "lines": [1, 1]}]}'
        )
        ops = _parse(resp, num_lines=5)
        assert [o.type for o in ops] == ["cut"]

    def test_out_of_range_lines_dropped(self):
        resp = '{"ops": [{"type": "cut", "lines": [4, 9]}, {"type": "cut", "lines": [0, 1]}]}'
        ops = _parse(resp, num_lines=5)
        assert ops == []

    def test_inverted_range_dropped(self):
        resp = '{"ops": [{"type": "cut", "lines": [4, 2]}]}'
        assert _parse(resp, num_lines=5) == []

    def test_speed_without_positive_factor_dropped(self):
        resp = '{"ops": [{"type": "speed", "lines": [1, 2], "factor": 0}, {"type": "speed", "lines": [1, 2]}]}'
        assert _parse(resp, num_lines=5) == []

    def test_overlay_without_text_dropped(self):
        resp = (
            '{"ops": [{"type": "overlay", "lines": [1, 2], "text": "", "duration": 2.0},'
            ' {"type": "overlay", "lines": [1, 2], "duration": 2.0}]}'
        )
        assert _parse(resp, num_lines=5) == []

    def test_overlay_without_positive_duration_dropped(self):
        # The duration is the whole point of the op: it states how long the
        # text is on screen, so an overlay without one is not applicable.
        resp = (
            '{"ops": [{"type": "overlay", "lines": [1, 2], "text": "x"},'
            ' {"type": "overlay", "lines": [1, 2], "text": "x", "duration": 0},'
            ' {"type": "overlay", "lines": [1, 2], "text": "x", "duration": "3.0"}]}'
        )
        assert _parse(resp, num_lines=5) == []

    def test_overlay_text_line_endings_normalised(self):
        # A multi-line caption is kept (Blender renders the break), but only
        # "\n" — a stray CR would survive into the marker and split the
        # _edits.txt line on readers that honour it.
        resp = (
            '{"ops": [{"type": "overlay", "lines": [1, 1],'
            ' "text": "A\\r\\nB\\rC", "duration": 2.0}]}'
        )
        ops = _parse(resp, num_lines=5)
        assert [o.text for o in ops] == ["A\nB\nC"]

    def test_overlay_text_of_only_line_breaks_dropped(self):
        resp = '{"ops": [{"type": "overlay", "lines": [1, 1], "text": "\\n \\n", "duration": 2.0}]}'
        assert _parse(resp, num_lines=5) == []


class TestHelpers:
    def test_clean_for_display_applies_patches_and_strips_tags(self):
        lines = ["あ{{えーと->}}い", "<keep>うえ</keep>"]
        assert clean_for_display(lines) == ["あい", "うえ"]

    def test_format_numbered_transcript(self):
        assert format_numbered_transcript(["あ", "い"]) == "1: あ\n2: い"

    def test_ops_to_dict_roundtrips_fields(self):
        ops = [
            DirectorOp(type="speed", lines=(2, 3), note="n", factor=2.0),
            DirectorOp(type="overlay", lines=(1, 1), text="x", duration=3.0),
            DirectorOp(type="cut", lines=(4, 5)),
        ]
        d = ops_to_dict(ops)
        assert d["ops"][0] == {"type": "speed", "lines": [2, 3], "factor": 2.0, "note": "n"}
        assert d["ops"][1] == {
            "type": "overlay",
            "lines": [1, 1],
            "text": "x",
            "duration": 3.0,
        }
        assert d["ops"][2] == {"type": "cut", "lines": [4, 5]}


class TestTimedTranscript:
    def test_annotates_dur_and_gap_last_line_no_gap(self):
        seg = [(1.0, 3.0), (4.0, 6.5)]  # gap after line1 = 1.0s
        out = format_numbered_transcript_timed(["あい", "うえ"], seg)
        assert out == "1: あい  [2.0s, gap 1.0s]\n2: うえ  [2.5s]"

    def test_missing_times_degrade_to_plain_line(self):
        seg = [(None, None), (4.0, 6.0)]
        out = format_numbered_transcript_timed(["あ", "い"], seg)
        # line1 has no dur -> no bracket (trailing spaces stripped); line2 last -> dur only
        assert out == "1: あ\n2: い  [2.0s]"


class TestTimedTranscriptSilences:
    def test_silence_split_bracket_rendered(self):
        from nagare_clip.director.transcript import format_numbered_transcript_timed

        out = format_numbered_transcript_timed(
            ["long line", "short line"],
            [(0.0, 75.8), (80.0, 84.2)],
            silences=[62.9, 0.0],
        )
        lines = out.split("\n")
        # speech = 75.8 - 62.9 = 12.9; gap to next = 80.0 - 75.8 = 4.2
        assert lines[0] == "1: long line  [12.9s speech, 62.9s silence, gap 4.2s]"
        assert lines[1] == "2: short line  [4.2s]"

    def test_no_silences_byte_identical_to_before(self):
        from nagare_clip.director.transcript import format_numbered_transcript_timed

        args = (["a", "b"], [(0.0, 4.2), (5.0, 9.0)])
        assert format_numbered_transcript_timed(*args) == format_numbered_transcript_timed(
            *args, silences=None
        )

    def test_speech_clamped_non_negative(self):
        from nagare_clip.director.transcript import format_numbered_transcript_timed

        # silence bigger than the span (rounding artifacts) must not render negative speech
        out = format_numbered_transcript_timed(["x"], [(0.0, 5.0)], silences=[5.5])
        assert "-" not in out.split("  ")[1]


class TestTimelapseOp:
    """A timelapse is one op carrying the range, the factor and the caption.
    The factor floor is part of what the word means, so it holds on both the
    LLM path and the hand-edited _director.json path (spec 2026-08-01)."""

    def _resp(self, **fields):
        op = {"type": "timelapse", "lines": [10, 20]}
        op.update(fields)
        return json.dumps({"ops": [op]})

    def test_timelapse_at_the_floor_is_accepted(self):
        ops = _parse(self._resp(factor=TIMELAPSE_MIN_FACTOR, text="配管の取り付け"), num_lines=40)
        assert len(ops) == 1
        assert ops[0].type == "timelapse"
        assert ops[0].lines == (10, 20)
        assert ops[0].factor == TIMELAPSE_MIN_FACTOR
        assert ops[0].text == "配管の取り付け"
        assert ops[0].duration is None

    def test_timelapse_above_the_floor_is_accepted(self):
        ops = _parse(self._resp(factor=8.0, text="作業"), num_lines=40)
        assert [o.factor for o in ops] == [8.0]

    def test_timelapse_below_the_floor_is_dropped(self):
        # 2.0x is a mild speed-up, not a timelapse; accepting it would smuggle
        # an uncapped keep over talking past max_keep_lines.
        drops: list[str] = []
        raw = json.loads(self._resp(factor=2.0, text="作業"))["ops"][0]
        assert parse_op(raw, 40, drops) is None
        assert "timelapse" in drops[0] and str(TIMELAPSE_MIN_FACTOR) in drops[0]

    def test_timelapse_without_factor_is_dropped(self):
        assert _parse(self._resp(text="作業"), num_lines=40) == []

    def test_timelapse_text_is_optional(self):
        # No caption is legitimate: the op still fixes continuity.
        ops = _parse(self._resp(factor=6.0), num_lines=40)
        assert len(ops) == 1
        assert ops[0].text is None

    def test_timelapse_blank_text_counts_as_absent(self):
        ops = _parse(self._resp(factor=6.0, text="   "), num_lines=40)
        assert [o.text for o in ops] == [None]

    def test_timelapse_text_newlines_are_normalised(self):
        ops = _parse(self._resp(factor=6.0, text="一行目\r\n二行目"), num_lines=40)
        assert ops[0].text == "一行目\n二行目"

    def test_timelapse_ignores_a_supplied_duration(self):
        # The caption's on-screen time is derived, never stated.
        ops = _parse(self._resp(factor=6.0, text="作業", duration=2.0), num_lines=40)
        assert ops[0].duration is None

    def test_hand_written_timelapse_gets_the_same_floor(self):
        good = {"ops": [{"type": "timelapse", "lines": [10, 20], "factor": 5.0}]}
        bad = {"ops": [{"type": "timelapse", "lines": [10, 20], "factor": 2.0}]}
        assert len(ops_from_dict(good, num_lines=40)) == 1
        assert ops_from_dict(bad, num_lines=40) == []

    def test_timelapse_round_trips_through_ops_to_dict(self):
        ops = _parse(self._resp(factor=8.0, text="配管の取り付け", note="長い作業"), num_lines=40)
        data = ops_to_dict(ops)
        assert data["ops"] == [
            {
                "type": "timelapse",
                "lines": [10, 20],
                "factor": 8.0,
                "text": "配管の取り付け",
                "note": "長い作業",
            }
        ]
        assert [o.lines for o in ops_from_dict(data, num_lines=40)] == [(10, 20)]

    def test_a_hand_written_bare_speed_op_is_untouched_by_the_floor(self):
        # speed stays a primitive on the hand-edit path: any positive factor,
        # above or below 4.0.  (The LLM may no longer emit one at all --
        # see TestSpeedIsOffTheDirectorsMenu.)
        data = {
            "ops": [
                {"type": "speed", "lines": [1, 2], "factor": 1.5},
                {"type": "speed", "lines": [3, 4], "factor": 8.0},
            ]
        }
        assert [o.factor for o in ops_from_dict(data, num_lines=40)] == [1.5, 8.0]


class TestOpsFromDictWithoutLineBound:
    """Reading ANOTHER video's `_director.json` (the director stage does this to
    list the captions already committed upstream) has no transcript to validate
    line numbers against, so `num_lines=None` skips the range check."""

    def test_none_bound_keeps_lines_beyond_any_known_length(self):
        data = {"ops": [{"type": "overlay", "lines": [900, 900], "text": "T", "duration": 2.0}]}
        assert [o.lines for o in ops_from_dict(data, num_lines=None)] == [(900, 900)]

    def test_none_bound_still_rejects_a_malformed_range(self):
        data = {"ops": [{"type": "cut", "lines": [9, 3]}, {"type": "cut", "lines": [0, 2]}]}
        assert ops_from_dict(data, num_lines=None) == []

    def test_integer_bound_still_enforces_the_range(self):
        data = {"ops": [{"type": "cut", "lines": [900, 900]}]}
        assert ops_from_dict(data, num_lines=40) == []


def test_collect_overlay_texts_takes_overlay_and_timelapse_captions():
    from nagare_clip.director.ops import DirectorOp, collect_overlay_texts

    ops = [
        DirectorOp(type="overlay", lines=(1, 1), text="水浸し！", duration=3.0),
        DirectorOp(type="timelapse", lines=(2, 4), factor=8.0, text="配管作業"),
        DirectorOp(type="cut", lines=(5, 6)),
        DirectorOp(type="overlay", lines=(7, 7), text="水浸し！", duration=3.0),
    ]
    assert collect_overlay_texts(ops) == ["水浸し！", "配管作業"]


class TestEditOp:
    """An edit op is one exact patch: one line, "old" and "new" as text."""

    def test_round_trips_through_the_file_shape(self):
        raw = {"type": "edit", "lines": [3, 3], "old": "えーと", "new": ""}
        (op,) = ops_from_dict({"ops": [raw]}, 10)
        assert (op.old, op.new) == ("えーと", "")
        assert ops_to_dict([op])["ops"] == [raw]

    def test_the_prose_form_is_dropped(self):
        drops: list[str] = []
        assert parse_op({"type": "edit", "lines": [3, 3], "note": "drop it"}, 10, drops) is None
        assert "'old'" in drops[0]

    def test_more_than_one_line_is_dropped(self):
        drops: list[str] = []
        raw = {"type": "edit", "lines": [3, 4], "old": "a", "new": ""}
        assert parse_op(raw, 10, drops) is None
        assert "exactly one line" in drops[0]

    def test_patch_syntax_is_dropped(self):
        drops: list[str] = []
        raw = {"type": "edit", "lines": [3, 3], "old": "a", "new": "b->c"}
        assert parse_op(raw, 10, drops) is None
        assert "patch text" in drops[0]


def test_the_preview_header_speaks_of_this_edit():
    from nagare_clip.director.preview import PLAN_HEADER, STATE_HEADER

    for text in (PLAN_HEADER, STATE_HEADER):
        assert "conversation" not in text and "reply" not in text and "YOUR" not in text
