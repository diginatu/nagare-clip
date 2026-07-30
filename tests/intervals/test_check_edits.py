"""Tests for the _edits.txt integrity checker.

The checker validates a human-edited ``_edits.txt`` against the original
WhisperX JSON and reports *all* problems at once (line-numbered), so the
human can fix everything in one pass instead of discovering errors one at a
time via the intervals stage's fail-fast ``ValueError``.
"""

from __future__ import annotations

import pytest

from nagare_clip.intervals.check_edits import check_edits, main
from nagare_clip.intervals.sync_json import sync_text_to_json


def _seg(text: str) -> dict:
    # Word timings are irrelevant to syntax/integrity checks; the checker
    # only inspects segment ``text``.
    return {"text": text, "words": []}


def _json(*texts: str) -> dict:
    return {"segments": [_seg(t) for t in texts]}


# A 3-segment fixture reused across cases.
def _fixture() -> dict:
    return _json("hello", "world", "foo bar")


def _messages(problems):
    return [p.message for p in problems]


def _on_line(problems, line):
    return [p for p in problems if p.line == line]


class TestSyncParityGuard:
    """check_edits must never bless a file that ``sync_text_to_json`` rejects.

    Historical failure: a ``<cut>`` span covering a line whose text_filter
    output left a trailing space after a ``{{old->}}`` patch passed every
    per-line check, but the intervals stage raised ``ValueError``. The guard
    runs the real sync on an otherwise-clean file and reports its rejection
    as a Problem instead of letting the pipeline crash later.
    """

    def _parity(self, lines: list[str], json_data: dict) -> None:
        problems = check_edits(lines, json_data)
        try:
            sync_text_to_json(json_data, lines)
            sync_ok = True
        except ValueError:
            sync_ok = False
        assert bool(problems) == (not sync_ok), (
            f"checker/sync disagree: problems={problems!r} sync_ok={sync_ok}"
        )

    def test_cut_span_with_keep_overlap_and_trailing_space(self):
        # The exact real-world pattern: guided_edit's cut opens mid-file,
        # closes on a line also wrapped in <keep>, and an intermediate line
        # carries a trailing space after a deletion patch.
        self._parity(
            ["<cut>あいう", "かき{{く->}} ", "<keep>けこさ</cut></keep>"],
            _json("あいう", "かきく", "けこさ"),
        )

    def test_cross_line_cut_with_inner_patches(self):
        self._parity(
            ["あ<cut>いう", "{{かき->}}く", "け</cut>こ"],
            _json("あいう", "かきく", "けこ"),
        )


class TestCleanFile:
    def test_unchanged_file_has_no_problems(self):
        assert check_edits(["hello", "world", "foo bar"], _fixture()) == []

    def test_valid_patch_has_no_problems(self):
        # "he" prefix + {{llo->llo!}} → "hello!"; decomposes cleanly.
        problems = check_edits(["he{{llo->llo!}}", "world", "foo bar"], _fixture())
        assert problems == []

    def test_valid_deletion_with_empty_new_allowed(self):
        # {{world->}} is a deletion; empty `new` is explicitly permitted.
        problems = check_edits(["hello", "{{world->}}", "foo bar"], _fixture())
        assert problems == []

    def test_valid_keep_speed_overlay_tags_no_problems(self):
        lines = [
            "<keep>hello</keep>",
            '<speed factor="2.0">world</speed>',
            '<overlay text="note" duration="3.0"/>foo bar',
        ]
        assert check_edits(lines, _fixture()) == []


class TestLineCount:
    def test_too_few_lines_reported(self):
        problems = check_edits(["hello", "world"], _fixture())
        msgs = _messages(problems)
        assert any("2" in m and "3" in m for m in msgs)

    def test_too_many_lines_reported(self):
        problems = check_edits(["hello", "world", "foo bar", "extra"], _fixture())
        assert any("4" in m and "3" in m for m in _messages(problems))


class TestEmbeddedNewline:
    """An edit line must never contain a newline.

    In-memory the list length still matches the segment count, so every other
    check passes — but the file is written with ``"\\n".join(lines)``, so the
    newline becomes an extra physical line and shifts every later line off its
    segment. This is the last guard before that hits disk.
    """

    def test_newline_inside_a_line_reported(self):
        problems = check_edits(["hello", "wo\nrld", "foo bar"], _fixture())
        on_2 = _messages(_on_line(problems, 2))
        assert any("newline" in m for m in on_2)

    def test_clean_file_reports_nothing(self):
        assert check_edits(["hello", "world", "foo bar"], _fixture()) == []


class TestPatchSyntax:
    def test_unbalanced_braces_reported(self):
        # Missing one closing brace → not a valid patch.
        problems = check_edits(["hel{{lo->lo!}", "world", "foo bar"], _fixture())
        assert any("malformed" in m for m in _messages(_on_line(problems, 1)))

    def test_empty_patch_reported(self):
        # {{->}} is a no-op (both sides empty).
        problems = check_edits(["hello{{->}}", "world", "foo bar"], _fixture())
        assert any("empty" in m for m in _messages(_on_line(problems, 1)))

    def test_syntax_error_suppresses_decomposition_noise(self):
        # A malformed patch should produce the syntax problem only, not also a
        # confusing decomposition message on the same line.
        problems = _on_line(check_edits(["hel{{lo->lo!}", "world", "foo bar"], _fixture()), 1)
        assert len(problems) == 1


class TestDecomposition:
    def test_text_changed_without_marker_reported(self):
        # seg 1 ("world") edited to "word" with no patch marker.
        problems = check_edits(["hello", "word", "foo bar"], _fixture())
        assert _on_line(problems, 2)
        assert any("marker" in m for m in _messages(_on_line(problems, 2)))

    def test_old_side_mismatch_reported(self):
        # old="hallo" does not match original "hello".
        problems = check_edits(["{{hallo->X}}", "world", "foo bar"], _fixture())
        assert any("old" in m.lower() for m in _messages(_on_line(problems, 1)))

    def test_old_mismatch_message_shows_literal_double_braces(self):
        # The message must show {{old->new}}, not a mangled {old->new}.
        problems = check_edits(["{{hallo->X}}", "world", "foo bar"], _fixture())
        assert any("{{old->new}}" in m for m in _messages(_on_line(problems, 1)))

    def test_keep_text_mismatch_reported(self):
        # Text outside markers (inside <keep>) altered without a patch.
        problems = check_edits(["<keep>helo</keep>", "world", "foo bar"], _fixture())
        assert _on_line(problems, 1)


class TestTagBalance:
    def test_nested_keep_reported(self):
        lines = ["<keep>hello", "<keep>world</keep>", "foo bar</keep>"]
        problems = check_edits(lines, _fixture())
        assert any("nested" in m for m in _messages(_on_line(problems, 2)))

    def test_unmatched_close_reported(self):
        problems = check_edits(["hello", "world</keep>", "foo bar"], _fixture())
        assert any("unmatched" in m for m in _messages(_on_line(problems, 2)))

    def test_unclosed_open_reported_at_opening_line(self):
        problems = check_edits(["<keep>hello", "world", "foo bar"], _fixture())
        assert any("unclosed" in m for m in _messages(_on_line(problems, 1)))

    def test_zero_speed_factor_reported(self):
        problems = check_edits(['<speed factor="0">hello</speed>', "world", "foo bar"], _fixture())
        assert any("factor" in m for m in _messages(_on_line(problems, 1)))

    def test_empty_overlay_text_reported(self):
        problems = check_edits(
            ['<overlay text="" duration="3.0"/>hello', "world", "foo bar"], _fixture()
        )
        assert any("overlay" in m for m in _messages(_on_line(problems, 1)))

    def test_zero_overlay_duration_reported(self):
        problems = check_edits(
            ['<overlay text="x" duration="0"/>hello', "world", "foo bar"], _fixture()
        )
        assert any("duration" in m for m in _messages(_on_line(problems, 1)))

    def test_malformed_overlay_tag_reported(self):
        # A quote inside text="..." breaks the [^"]*' regex → not recognised.
        problems = check_edits(
            ['<overlay text="a"b" duration="3.0"/>hello', "world", "foo bar"],
            _fixture(),
        )
        assert any("malformed" in m for m in _messages(_on_line(problems, 1)))

    def test_overlay_without_duration_reported(self):
        # The old wrapping form is gone: duration is mandatory, so a bare
        # opener is a malformed tag rather than the start of a span.
        problems = check_edits(
            ['<overlay text="x">hello</overlay>', "world", "foo bar"], _fixture()
        )
        assert any("malformed" in m for m in _messages(_on_line(problems, 1)))

    def test_overlay_closing_tag_reported(self):
        problems = check_edits(["hello", "world</overlay>", "foo bar"], _fixture())
        assert any("malformed" in m for m in _messages(_on_line(problems, 2)))


class TestCutTag:
    def test_valid_cut_no_problems(self):
        lines = ["<cut>hello</cut>", "world", "foo bar"]
        assert check_edits(lines, _fixture()) == []

    def test_valid_cross_line_cut_no_problems(self):
        # <cut> opened on line 1, closed on line 3 (spans the middle segment).
        lines = ["he<cut>llo", "world", "foo</cut> bar"]
        assert check_edits(lines, _fixture()) == []

    def test_nested_cut_reported(self):
        lines = ["<cut>hello", "<cut>world</cut>", "foo bar</cut>"]
        problems = check_edits(lines, _fixture())
        assert any("nested" in m for m in _messages(_on_line(problems, 2)))

    def test_unmatched_cut_close_reported(self):
        problems = check_edits(["hello", "world</cut>", "foo bar"], _fixture())
        assert any("unmatched" in m for m in _messages(_on_line(problems, 2)))

    def test_unclosed_cut_reported_at_opening_line(self):
        problems = check_edits(["<cut>hello", "world", "foo bar"], _fixture())
        assert any("unclosed" in m for m in _messages(_on_line(problems, 1)))


class TestProblemOrdering:
    def test_problems_sorted_by_line(self):
        lines = ["word", "{{x->y}}", "foo bar"]  # seg0 no-marker, seg1 old mismatch
        problems = check_edits(lines, _fixture())
        lines_reported = [p.line for p in problems if p.line is not None]
        assert lines_reported == sorted(lines_reported)


class TestCli:
    def _write(self, tmp_path, lines, texts):
        import json

        edits = tmp_path / "x_edits.txt"
        edits.write_text("\n".join(lines), encoding="utf-8")
        jpath = tmp_path / "x.json"
        jpath.write_text(json.dumps(_json(*texts)), encoding="utf-8")
        return edits, jpath

    def test_clean_file_exits_zero(self, tmp_path, monkeypatch):
        edits, jpath = self._write(
            tmp_path, ["hello", "world", "foo bar"], ("hello", "world", "foo bar")
        )
        monkeypatch.setattr(
            "sys.argv",
            ["check_edits", "--edits-txt", str(edits), "--json", str(jpath)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_dirty_file_exits_one_and_lists_problems(self, tmp_path, monkeypatch, capsys):
        edits, jpath = self._write(
            tmp_path, ["word", "world", "foo bar"], ("hello", "world", "foo bar")
        )
        monkeypatch.setattr(
            "sys.argv",
            ["check_edits", "--edits-txt", str(edits), "--json", str(jpath)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "line 1" in out
