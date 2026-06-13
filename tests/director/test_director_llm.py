"""Tests for director response parsing/validation (pure, no network)."""

from __future__ import annotations

from nagare_clip.director.director_llm import (
    DirectorOp,
    clean_for_display,
    format_numbered_transcript,
    generate_director_ops,
    ops_to_dict,
    parse_director_response,
)


class TestParseValid:
    def test_parses_all_op_types(self):
        resp = """{"ops": [
          {"type": "cut", "lines": [2, 4], "note": "boring"},
          {"type": "speed", "lines": [5, 6], "factor": 2.0, "note": ""},
          {"type": "overlay", "lines": [1, 1], "text": "ポイント"},
          {"type": "keep", "lines": [7, 8]},
          {"type": "edit", "lines": [3, 3], "note": "drop restatement"}
        ]}"""
        ops = parse_director_response(resp, num_lines=10)
        assert [o.type for o in ops] == ["cut", "speed", "overlay", "keep", "edit"]
        assert ops[0] == DirectorOp(type="cut", lines=(2, 4), note="boring")
        assert ops[1].factor == 2.0
        assert ops[2].text == "ポイント"

    def test_strips_markdown_fence(self):
        resp = '```json\n{"ops": [{"type": "cut", "lines": [1, 2]}]}\n```'
        ops = parse_director_response(resp, num_lines=5)
        assert len(ops) == 1 and ops[0].type == "cut"

    def test_single_int_line_becomes_range(self):
        resp = '{"ops": [{"type": "keep", "lines": 3}]}'
        ops = parse_director_response(resp, num_lines=5)
        assert ops[0].lines == (3, 3)


class TestParseInvalidDropped:
    def test_unknown_type_dropped(self):
        resp = '{"ops": [{"type": "frobnicate", "lines": [1, 2]}, {"type": "cut", "lines": [1, 1]}]}'
        ops = parse_director_response(resp, num_lines=5)
        assert [o.type for o in ops] == ["cut"]

    def test_out_of_range_lines_dropped(self):
        resp = '{"ops": [{"type": "cut", "lines": [4, 9]}, {"type": "cut", "lines": [0, 1]}]}'
        ops = parse_director_response(resp, num_lines=5)
        assert ops == []

    def test_inverted_range_dropped(self):
        resp = '{"ops": [{"type": "cut", "lines": [4, 2]}]}'
        assert parse_director_response(resp, num_lines=5) == []

    def test_speed_without_positive_factor_dropped(self):
        resp = '{"ops": [{"type": "speed", "lines": [1, 2], "factor": 0}, {"type": "speed", "lines": [1, 2]}]}'
        assert parse_director_response(resp, num_lines=5) == []

    def test_overlay_without_text_dropped(self):
        resp = '{"ops": [{"type": "overlay", "lines": [1, 2], "text": ""}, {"type": "overlay", "lines": [1, 2]}]}'
        assert parse_director_response(resp, num_lines=5) == []

    def test_malformed_json_returns_empty(self):
        assert parse_director_response("not json", num_lines=5) == []

    def test_missing_ops_key_returns_empty(self):
        assert parse_director_response('{"foo": 1}', num_lines=5) == []


class TestHelpers:
    def test_clean_for_display_applies_patches_and_strips_tags(self):
        lines = ["あ{{えーと->}}い", "<keep>うえ</keep>"]
        assert clean_for_display(lines) == ["あい", "うえ"]

    def test_format_numbered_transcript(self):
        assert format_numbered_transcript(["あ", "い"]) == "1: あ\n2: い"

    def test_ops_to_dict_roundtrips_fields(self):
        ops = [
            DirectorOp(type="speed", lines=(2, 3), note="n", factor=2.0),
            DirectorOp(type="overlay", lines=(1, 1), text="x"),
            DirectorOp(type="cut", lines=(4, 5)),
        ]
        d = ops_to_dict(ops)
        assert d["ops"][0] == {"type": "speed", "lines": [2, 3], "factor": 2.0, "note": "n"}
        assert d["ops"][1] == {"type": "overlay", "lines": [1, 1], "text": "x"}
        assert d["ops"][2] == {"type": "cut", "lines": [4, 5]}


class TestGenerate:
    def test_generate_uses_clean_numbered_input_and_parses(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": [{"type": "cut", "lines": [1, 2]}]}'

        ops = generate_director_ops(
            ["あ{{えー->}}い", "うえ"], {"prompt": "P"}, call_llm=fake_llm
        )
        assert captured["user"] == "1: あい\n2: うえ"
        assert ops[0].type == "cut" and ops[0].lines == (1, 2)

    def test_generate_returns_empty_on_llm_failure(self):
        def boom(messages, cfg):
            raise ConnectionError("down")

        assert generate_director_ops(["あ"], {"prompt": "P"}, call_llm=boom) == []
