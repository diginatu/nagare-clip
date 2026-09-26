"""The ``_edits.txt`` line contract: speech lines, silence lines, and back.

guided_edit writes the director's silence lines into ``_edits.txt`` so an op on
``"n~"`` becomes an ordinary marker.  This module is the one renderer and the
one parser of that file; every reader goes through :func:`parse_edit_lines`.
"""

from __future__ import annotations

from nagare_clip.edit_lines import (
    expected_silences,
    gap_spans,
    insert_silence_lines,
    parse_edit_lines,
    silence_body,
    silence_problems,
    split_silence_line,
)

# A 10.0 s wait after line 1, 0.5 s after line 2, 8.0 s after line 3.
WHISPERX = {
    "segments": [
        {"words": [{"word": "あ", "start": 1.0, "end": 1.4}]},
        {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        {"words": [{"word": "う", "start": 12.5, "end": 13.0}]},
        {"words": [{"word": "え", "start": 21.0, "end": 22.0}]},
    ]
}

SPEECH = ["あ", "い", "う", "え"]
FILE = ["あ", "[silent 10.0s]", "い", "う", "[silent 8.0s: a hand]", "え"]


# --- rendering ---------------------------------------------------------------


def test_silence_body_formats_seconds_and_descriptions():
    assert silence_body(29.94) == "[silent 29.9s]"
    assert silence_body(3.0, ("a", "b")) == "[silent 3.0s: a / b]"
    assert silence_body(3.0, after_line=53) == "[silent 3.0s after line 53]"


def test_silence_body_escapes_backslash_and_closing_bracket_only():
    body = silence_body(3.0, ("a [x] \\ <keep> {{a->b}}",))
    assert body == "[silent 3.0s: a [x\\] \\\\ <keep> {{a->b}}]"


def test_insert_puts_each_body_after_its_speech_line():
    assert insert_silence_lines(SPEECH, {1: "[silent 10.0s]", 3: "[silent 8.0s: a hand]"}) == FILE


def test_insert_never_writes_a_silence_after_the_last_line():
    assert insert_silence_lines(["a", "b"], {2: "[silent 1.0s]"}) == ["a", "b"]


# --- the silence set -----------------------------------------------------------


def test_gap_spans_are_every_between_line_gap():
    assert gap_spans(WHISPERX) == {1: (1.4, 11.4), 2: (12.0, 12.5), 3: (13.0, 21.0)}


def test_expected_silences_apply_the_threshold():
    assert expected_silences(WHISPERX, 5.0) == {1, 3}
    assert expected_silences(WHISPERX, 0.0) == {1, 2, 3}
    assert expected_silences(WHISPERX, 9.0) == {1}


# --- recognising a silence line ------------------------------------------------


def test_split_plain_silence_line():
    assert split_silence_line("[silent 10.0s]") == ("", "[silent 10.0s]", "")


def test_split_keeps_markers_outside_the_body():
    line = '<keep><speed factor="8.0"><overlay text="x" duration="1.2"/>[silent 3.0s: a]</speed>'
    before, body, after = split_silence_line(line)
    assert before == '<keep><speed factor="8.0"><overlay text="x" duration="1.2"/>'
    assert body == "[silent 3.0s: a]"
    assert after == "</speed>"


def test_the_body_ends_at_the_first_unescaped_bracket():
    body = silence_body(3.0, ("x] </keep> y",))
    assert split_silence_line(f"<keep>{body}</keep>") == ("<keep>", body, "</keep>")


def test_markers_inside_the_body_are_description_text():
    body = silence_body(3.0, ("<keep> {{a->b}}",))
    assert split_silence_line(body) == ("", body, "")


def test_text_outside_the_body_makes_it_a_speech_line():
    assert split_silence_line("えー[silent 3.0s]") is None
    assert split_silence_line("[silent 3.0s]{{a->b}}") is None
    assert split_silence_line("[silent 3.0s] and more") is None


def test_a_body_that_is_not_the_silence_shape_is_speech():
    assert split_silence_line("[silent]") is None
    assert split_silence_line("[silent 3.0s") is None
    assert split_silence_line("[silence 3.0s]") is None
    assert split_silence_line("あ") is None


def test_a_dropped_escape_ends_the_body_early():
    # "x\] y" with its backslash deleted by hand: the body ends at "x]" and
    # " y]" is text outside it.
    assert split_silence_line("[silent 3.0s: x] y]") is None


# --- parse ---------------------------------------------------------------------


def test_parse_numbers_speech_and_anchors_silences():
    f = parse_edit_lines(FILE)
    assert f.speech_lines() == SPEECH
    assert [(s.file_line, s.speech_line) for s in f.silences()] == [(2, 1), (5, 3)]
    assert [s.kind for s in f.slots] == [
        "speech",
        "silence",
        "speech",
        "speech",
        "silence",
        "speech",
    ]


def test_a_legacy_file_has_no_silences():
    f = parse_edit_lines(SPEECH)
    assert f.speech_lines() == SPEECH
    assert f.silences() == []
    assert [s.file_line for s in f.slots] == [1, 2, 3, 4]


def test_file_index_maps_a_line_and_its_silence():
    f = parse_edit_lines(FILE)
    assert f.file_index(1, False) == 1
    assert f.file_index(1, True) == 2
    assert f.file_index(2, False) == 3
    assert f.file_index(3, True) == 5
    assert f.file_index(4, False) == 6
    assert f.file_index(2, True) is None  # no silence line after 2
    assert f.file_index(5, False) is None


def test_speech_line_of_maps_back():
    f = parse_edit_lines(FILE)
    assert [f.speech_line_of(i) for i in range(1, 7)] == [
        (1, False),
        (1, True),
        (2, False),
        (3, False),
        (3, True),
        (4, False),
    ]


def test_marker_text_hides_the_body():
    f = parse_edit_lines(["a", "<keep>" + silence_body(3.0, ("<cut>",)) + "</keep>", "b"])
    assert f.marker_text() == ["a", "<keep></keep>", "b"]


# --- speech projection ---------------------------------------------------------


def test_projection_moves_openers_forward_and_closers_back():
    f = parse_edit_lines(["a<cut>", "</cut>[silent 3.0s]<cut>", "b</cut>"])
    assert f.speech_projection() == ["a<cut></cut>", "<cut>b</cut>"]


def test_projection_drops_a_pair_on_one_silence_line():
    f = parse_edit_lines(["a", "<cut>[silent 3.0s]</cut>", "b"])
    assert f.speech_projection() == ["a", "b"]


def test_projection_of_a_cut_from_silence_to_silence_spares_the_line_before():
    f = parse_edit_lines(["a", "<cut>[silent 3.0s]", "b", "[silent 3.0s]</cut>", "c"])
    assert f.speech_projection() == ["a", "<cut>b</cut>", "c"]


def test_projection_of_a_legacy_file_is_the_file():
    assert parse_edit_lines(["a<keep>", "b</keep>"]).speech_projection() == ["a<keep>", "b</keep>"]


# --- validation ----------------------------------------------------------------


def _problems(lines, expected=frozenset({1, 3}), min_seconds=5.0):
    return silence_problems(parse_edit_lines(lines), set(expected), min_seconds)


def test_a_complete_file_has_no_problems():
    assert _problems(FILE) == []


def test_a_legacy_file_is_not_checked():
    assert _problems(SPEECH) == []


def test_a_deleted_silence_line_is_named_where_it_belongs():
    lines = ["あ", "[silent 10.0s]", "い", "う", "え"]
    ((line, message),) = _problems(lines)
    assert line == 5
    assert "missing silence line after speech line 3" in message


def test_an_unexpected_silence_line_is_named():
    lines = ["あ", "[silent 10.0s]", "い", "[silent 0.5s]", "う", "[silent 8.0s]", "え"]
    ((line, message),) = _problems(lines)
    assert line == 4
    assert "unexpected silence line after speech line 2" in message
    assert "5.0s" in message


def test_a_duplicated_silence_line_is_named():
    lines = ["あ", "[silent 10.0s]", "[silent 10.0s]", "い", "う", "[silent 8.0s]", "え"]
    ((line, message),) = _problems(lines)
    assert line == 3
    assert "duplicate silence line after speech line 1" in message


def test_a_moved_silence_line_is_missing_at_one_place_and_unexpected_at_another():
    lines = ["あ", "い", "[silent 10.0s]", "う", "[silent 8.0s]", "え"]
    problems = _problems(lines)
    assert [line for line, _ in problems] == [2, 3]
    assert "missing silence line after speech line 1" in problems[0][1]
    assert "unexpected silence line after speech line 2" in problems[1][1]


def test_a_silence_line_before_the_first_speech_line_is_named():
    lines = ["[silent 1.0s]", "あ", "[silent 10.0s]", "い", "う", "[silent 8.0s]", "え"]
    ((line, message),) = _problems(lines)
    assert line == 1
    assert "before the first speech line" in message


def test_text_added_to_a_silence_line_says_so():
    lines = ["あ", "[silent 10.0s] えー", "い", "う", "[silent 8.0s]", "え"]
    problems = _problems(lines)
    assert (2, problems[0][1]) == problems[0]
    assert "may carry only markers" in problems[0][1]


def test_an_edited_description_or_duration_is_not_a_problem():
    lines = ["あ", "[silent 99.0s: my own words]", "い", "う", "[silent 8.0s]", "え"]
    assert _problems(lines) == []
