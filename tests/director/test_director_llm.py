"""Tests for director response parsing/validation (pure, no network)."""

from __future__ import annotations

import json

import pytest

from nagare_clip.director.director_llm import (
    DirectorOp,
    clean_for_display,
    format_numbered_transcript,
    format_numbered_transcript_timed,
    generate_director_ops,
    keep_limit_note,
    ops_from_dict,
    ops_to_dict,
    parse_director_response,
    try_parse_director_response,
)


def _seq_llm(items, temps=None):
    """Fake call_llm that yields *items* in order; an ``Exception`` item is
    raised. Records each call's temperature into *temps* if given."""
    box = {"i": 0}

    def fake(_messages, cfg):
        if temps is not None:
            temps.append(cfg.get("temperature"))
        item = items[box["i"]]
        box["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    fake.calls = box
    return fake


class TestParseValid:
    def test_parses_all_op_types(self):
        resp = """{"ops": [
          {"type": "cut", "lines": [2, 4], "note": "boring"},
          {"type": "speed", "lines": [5, 6], "factor": 2.0, "note": ""},
          {"type": "overlay", "lines": [1, 1], "text": "ポイント", "duration": 2.5},
          {"type": "keep", "lines": [7, 8]},
          {"type": "edit", "lines": [3, 3], "note": "drop restatement"}
        ]}"""
        ops = parse_director_response(resp, num_lines=10)
        assert [o.type for o in ops] == ["cut", "speed", "overlay", "keep", "edit"]
        assert ops[0] == DirectorOp(type="cut", lines=(2, 4), note="boring")
        assert ops[1].factor == 2.0
        assert ops[2].text == "ポイント"
        assert ops[2].duration == 2.5

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
        resp = (
            '{"ops": [{"type": "frobnicate", "lines": [1, 2]}, {"type": "cut", "lines": [1, 1]}]}'
        )
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
        resp = (
            '{"ops": [{"type": "overlay", "lines": [1, 2], "text": "", "duration": 2.0},'
            ' {"type": "overlay", "lines": [1, 2], "duration": 2.0}]}'
        )
        assert parse_director_response(resp, num_lines=5) == []

    def test_overlay_without_positive_duration_dropped(self):
        # The duration is the whole point of the op: it states how long the
        # text is on screen, so an overlay without one is not applicable.
        resp = (
            '{"ops": [{"type": "overlay", "lines": [1, 2], "text": "x"},'
            ' {"type": "overlay", "lines": [1, 2], "text": "x", "duration": 0},'
            ' {"type": "overlay", "lines": [1, 2], "text": "x", "duration": "3.0"}]}'
        )
        assert parse_director_response(resp, num_lines=5) == []

    def test_overlay_text_line_endings_normalised(self):
        # A multi-line caption is kept (Blender renders the break), but only
        # "\n" — a stray CR would survive into the marker and split the
        # _edits.txt line on readers that honour it.
        resp = (
            '{"ops": [{"type": "overlay", "lines": [1, 1],'
            ' "text": "A\\r\\nB\\rC", "duration": 2.0}]}'
        )
        ops = parse_director_response(resp, num_lines=5)
        assert [o.text for o in ops] == ["A\nB\nC"]

    def test_overlay_text_of_only_line_breaks_dropped(self):
        resp = '{"ops": [{"type": "overlay", "lines": [1, 1], "text": "\\n \\n", "duration": 2.0}]}'
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


class TestGenerate:
    def test_generate_uses_clean_numbered_input_and_parses(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": [{"type": "cut", "lines": [1, 2]}]}'

        ops = generate_director_ops(["あ{{えー->}}い", "うえ"], {"prompt": "P"}, call_llm=fake_llm)
        assert captured["user"] == "1: あい\n2: うえ"
        assert ops[0].type == "cut" and ops[0].lines == (1, 2)

    def test_generate_returns_empty_on_llm_failure(self):
        def boom(messages, cfg):
            raise ConnectionError("down")

        assert generate_director_ops(["あ"], {"prompt": "P"}, call_llm=boom) == []

    def test_empty_overview_context_leaves_system_prompt_unchanged(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["system"] = messages[0]["content"]
            return '{"ops": []}'

        generate_director_ops(["あ"], {"prompt": "P"}, call_llm=fake_llm)
        assert captured["system"] == "P"

    def test_overview_context_appended_to_system_prompt(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["system"] = messages[0]["content"]
            return '{"ops": []}'

        generate_director_ops(["あ"], {"prompt": "P"}, call_llm=fake_llm, overview_context="CTX")
        assert captured["system"] == "P\n\nCTX"


class TestKeepWidthLimit:
    """`keep` restores every silence in its range, so a wide one can double the
    finished runtime while adding no speech (speech is kept by default anyway).
    `director.max_keep_lines` caps the LLM-emitted op width."""

    def _resp(self, a, b):
        return json.dumps({"ops": [{"type": "keep", "lines": [a, b], "note": "n"}]})

    def test_keep_within_limit_survives(self):
        ops = parse_director_response(self._resp(3, 4), num_lines=40, max_keep_lines=4)
        assert [o.lines for o in ops] == [(3, 4)]

    def test_keep_at_exactly_the_limit_survives(self):
        ops = parse_director_response(self._resp(3, 6), num_lines=40, max_keep_lines=4)
        assert [o.lines for o in ops] == [(3, 6)]

    def test_wide_keep_is_dropped(self):
        # The real-run shape: keep [97, 131] = 35 lines of restored silence.
        assert parse_director_response(self._resp(97, 131), num_lines=140, max_keep_lines=4) == []

    def test_drop_is_reported(self):
        drops: list[str] = []
        try_parse_director_response(
            self._resp(97, 131), num_lines=140, drops=drops, max_keep_lines=4
        )
        assert len(drops) == 1
        assert "35 lines" in drops[0] and "max_keep_lines=4" in drops[0]

    def test_zero_means_no_limit(self):
        ops = parse_director_response(self._resp(1, 99), num_lines=140, max_keep_lines=0)
        assert [o.lines for o in ops] == [(1, 99)]

    def test_only_keep_ops_are_capped(self):
        resp = json.dumps(
            {
                "ops": [
                    {"type": "cut", "lines": [1, 40]},
                    {"type": "speed", "lines": [41, 90], "factor": 2.0},
                    {"type": "keep", "lines": [91, 130]},
                ]
            }
        )
        ops = parse_director_response(resp, num_lines=140, max_keep_lines=4)
        assert [o.type for o in ops] == ["cut", "speed"]

    def test_hand_edited_director_json_is_not_capped(self):
        """`_director.json` is a hand-editable intermediate: a human who writes
        a wide keep into it means it, so `ops_from_dict` never applies the cap."""
        data = {"ops": [{"type": "keep", "lines": [1, 99]}]}
        assert [o.lines for o in ops_from_dict(data, num_lines=140)] == [(1, 99)]

    def test_generate_reads_the_limit_from_cfg(self):
        fake = _seq_llm([self._resp(1, 99)])
        ops = generate_director_ops(
            ["あ"] * 140, {"prompt": "P", "max_keep_lines": 4}, call_llm=fake
        )
        assert ops == []

    def test_generate_without_cfg_key_is_unlimited(self):
        fake = _seq_llm([self._resp(1, 99)])
        ops = generate_director_ops(["あ"] * 140, {"prompt": "P"}, call_llm=fake)
        assert [o.lines for o in ops] == [(1, 99)]

    def test_limit_is_stated_in_the_system_prompt(self):
        """The number the LLM is told must be the number the parser enforces."""
        captured = {}

        def fake_llm(messages, cfg):
            captured["system"] = messages[0]["content"]
            return '{"ops": []}'

        generate_director_ops(
            ["あ"], {"prompt": "P", "max_keep_lines": 3}, call_llm=fake_llm, overview_context="CTX"
        )
        assert captured["system"] == f"P\n\n{keep_limit_note(3)}\n\nCTX"
        assert "at most 3 line(s)" in captured["system"]

    def test_no_note_when_unlimited(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["system"] = messages[0]["content"]
            return '{"ops": []}'

        generate_director_ops(["あ"], {"prompt": "P", "max_keep_lines": 0}, call_llm=fake_llm)
        assert captured["system"] == "P"


class TestTryParse:
    def test_hard_failure_returns_none(self):
        assert try_parse_director_response("not json", num_lines=5) is None
        assert try_parse_director_response('{"foo": 1}', num_lines=5) is None

    def test_valid_empty_ops_returns_empty_list(self):
        assert try_parse_director_response('{"ops": []}', num_lines=5) == []

    def test_valid_ops_returned(self):
        ops = try_parse_director_response(
            '{"ops": [{"type": "cut", "lines": [1, 1]}]}', num_lines=5
        )
        assert ops is not None and ops[0].type == "cut"


class TestRetry:
    def test_retries_on_llm_error_then_succeeds(self):
        fake = _seq_llm([ConnectionError("x"), '{"ops": [{"type": "cut", "lines": [1, 1]}]}'])
        ops = generate_director_ops(["あ"], {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert fake.calls["i"] == 2
        assert ops[0].type == "cut"

    def test_retries_on_unparseable_then_succeeds(self):
        fake = _seq_llm(["garbage", "still bad", '{"ops": [{"type": "keep", "lines": [1, 1]}]}'])
        ops = generate_director_ops(["あ"], {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert fake.calls["i"] == 3
        assert ops[0].type == "keep"

    def test_all_attempts_fail_returns_empty(self):
        fake = _seq_llm([ConnectionError("x")] * 3)
        ops = generate_director_ops(["あ"], {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert ops == []
        assert fake.calls["i"] == 3

    def test_valid_empty_ops_does_not_retry(self):
        fake = _seq_llm(['{"ops": []}'])
        ops = generate_director_ops(["あ"], {"prompt": "P", "max_retries": 2}, call_llm=fake)
        assert ops == []
        assert fake.calls["i"] == 1

    def test_max_retries_zero_is_single_attempt(self):
        fake = _seq_llm([ConnectionError("x")])
        ops = generate_director_ops(["あ"], {"prompt": "P", "max_retries": 0}, call_llm=fake)
        assert ops == []
        assert fake.calls["i"] == 1

    def test_temperature_nudged_per_attempt(self):
        temps: list = []
        fake = _seq_llm([ConnectionError("x")] * 4, temps=temps)
        generate_director_ops(
            ["あ"],
            {
                "prompt": "P",
                "temperature": 0.2,
                "max_retries": 3,
                "retry_temp_step": 0.2,
                "retry_temp_cap": 0.8,
            },
            call_llm=fake,
        )
        assert temps == [
            pytest.approx(0.2),
            pytest.approx(0.4),
            pytest.approx(0.6),
            pytest.approx(0.8),
        ]


import yaml as _yaml

from nagare_clip.llm_report import Recorder


def _outcome(tmp_path, stage, unit):
    text = (tmp_path / stage / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)["outcome"]


class TestDirectorRecorder:
    def test_records_unparseable_then_ok(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        fake = _seq_llm(["nonsense", '{"ops": []}'])
        ops = generate_director_ops(
            ["a", "b"],
            {"max_retries": 2},
            call_llm=fake,
            recorder=rec,
            unit="vid",
        )
        assert ops == []
        assert _outcome(tmp_path, "director", "vid") == "ok-empty"
        body = (tmp_path / "director" / "vid.md").read_text(encoding="utf-8")
        assert "nonsense" in body  # failed attempt's response preserved

    def test_records_dropped_items(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        # one valid op, one with out-of-range lines (dropped)
        resp = '{"ops": [{"type":"cut","lines":[1,1]},{"type":"cut","lines":[9,9]}]}'
        fake = _seq_llm([resp])
        ops = generate_director_ops(
            ["a", "b"],
            {"max_retries": 0},
            call_llm=fake,
            recorder=rec,
            unit="vid",
        )
        assert len(ops) == 1
        assert _outcome(tmp_path, "director", "vid") == "dropped-items"


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

    def test_generate_uses_timed_format_when_seg_times_given(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(
            ["あ", "い"],
            {"prompt": "P"},
            call_llm=fake_llm,
            seg_times=[(1.0, 3.0), (4.0, 6.5)],
        )
        assert captured["user"] == "1: あ  [2.0s, gap 1.0s]\n2: い  [2.5s]"

    def test_generate_falls_back_byte_identical_without_seg_times(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(["あ", "い"], {"prompt": "P"}, call_llm=fake_llm)
        assert captured["user"] == "1: あ\n2: い"

    def test_generate_falls_back_on_length_mismatch(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(
            ["あ", "い"],
            {"prompt": "P"},
            call_llm=fake_llm,
            seg_times=[(1.0, 3.0)],  # only 1 entry for 2 lines
        )
        assert captured["user"] == "1: あ\n2: い"


def test_generate_director_ops_annotates_the_transcript_with_gaps():
    from nagare_clip.gap_context.gaps import Gap

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    generate_director_ops(
        ["いち", "に"],
        {"prompt": "P", "max_retries": 0},
        call_llm=fake_llm,
        seg_times=[(0.0, 10.0), (20.0, 25.0)],
        gaps=[Gap(start=10.0, end=20.0, frames=[], description="ビルドが走る")],
    )
    assert seen["user"] == (
        "1: いち  [10.0s, gap 10.0s]\n    [silent gap 10.0s: ビルドが走る]\n2: に  [5.0s]"
    )


def test_generate_director_ops_transcript_is_byte_identical_without_gaps():
    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    generate_director_ops(
        ["いち", "に"],
        {"prompt": "P", "max_retries": 0},
        call_llm=fake_llm,
        seg_times=[(0.0, 10.0), (20.0, 25.0)],
    )
    assert seen["user"] == "1: いち  [10.0s, gap 10.0s]\n2: に  [5.0s]"


class TestTimedTranscriptSilences:
    def test_silence_split_bracket_rendered(self):
        from nagare_clip.director.director_llm import format_numbered_transcript_timed

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
        from nagare_clip.director.director_llm import format_numbered_transcript_timed

        args = (["a", "b"], [(0.0, 4.2), (5.0, 9.0)])
        assert format_numbered_transcript_timed(*args) == format_numbered_transcript_timed(
            *args, silences=None
        )

    def test_speech_clamped_non_negative(self):
        from nagare_clip.director.director_llm import format_numbered_transcript_timed

        # silence bigger than the span (rounding artifacts) must not render negative speech
        out = format_numbered_transcript_timed(["x"], [(0.0, 5.0)], silences=[5.5])
        assert "-" not in out.split("  ")[1]
