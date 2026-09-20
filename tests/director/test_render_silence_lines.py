"""The rendered transcript with silence lines in it.

One silence, one number: the wait is stated in the silence line, and the
bracket of the line it follows drops its `gap` part.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import render_transcript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.gap_context.gaps import Gap

LINES = ["その状態で", "この状態で今予備水持ってきたんで", "で、あとは下ろすだけ。"]
TIMES = [(500.9, 501.9), (531.8, 561.6), (564.8, 566.4)]
SILENCES = [0.2, 13.4, 1.1]
PUMP = "a hand enters from the right holding a clear tube"


def _render(silence_lines=None, gaps=None, first_line=53):
    return render_transcript(
        LINES,
        TIMES,
        SILENCES,
        gaps or [],
        first_line,
        silence_lines=silence_lines,
    )


def test_the_silence_line_follows_the_line_it_names():
    line = SilenceLine(53, 501.897, 531.779, (PUMP,))
    rendered = _render([line]).split("\n")
    assert rendered[0] == "53: その状態で  [1.0s]"
    assert rendered[1] == f"    [silent 29.9s after line 53: {PUMP}]"
    assert rendered[2].startswith("54: この状態で")


def test_the_gap_disappears_from_the_bracket_it_was_counted_in():
    # Without the silence line, line 53's bracket carries `gap 29.9s`; with it,
    # the same 29.9 s must not be printed twice.
    plain = _render()
    assert "gap 29.9s" in plain
    with_line = _render([SilenceLine(53, 501.897, 531.779)])
    assert "gap 29.9s" not in with_line
    assert "[silent 29.9s after line 53]" in with_line


def test_a_gap_with_no_silence_line_keeps_its_bracket():
    # 561.6 -> 564.8 after line 54 is 3.2 s: under the threshold, so it is
    # still the bracket's to report.
    rendered = _render([SilenceLine(53, 501.897, 531.779)])
    assert "gap 3.2s" in rendered


def test_silence_inside_a_line_stays_in_its_bracket():
    rendered = _render([SilenceLine(53, 501.897, 531.779)]).split("\n")
    assert (
        rendered[2]
        == "54: この状態で今予備水持ってきたんで  [16.4s speech, 13.4s silence, gap 3.2s]"
    )


def test_a_leftover_gap_annotation_stays_under_its_own_line():
    # A description of silence INSIDE line 54 is not the wait after it: both
    # can be shown, each under the line it belongs to.
    inside = (2, Gap(540.0, 545.0, description="inside line 54"))
    rendered = _render([SilenceLine(54, 561.6, 564.8)], gaps=[inside]).split("\n")
    assert rendered[1].startswith("54: ")
    assert rendered[2] == "    [silent gap: inside line 54]"
    assert rendered[3] == "    [silent 3.2s after line 54]"


def test_a_silence_line_for_a_line_this_view_does_not_show_is_dropped():
    # Appending it instead would read as the silence after the LAST line, which
    # is a different stretch of footage and addressable as a different "n~".
    assert "silent" not in _render([SilenceLine(99, 1.0, 9.0)])


def test_no_silence_lines_renders_exactly_what_it_always_did():
    assert _render([]) == render_transcript(LINES, TIMES, SILENCES, [], 53)
    assert _render(None) == render_transcript(LINES, TIMES, SILENCES, [], 53)


def test_an_untimed_transcript_has_no_silence_lines_to_place():
    # Without times there are no brackets and no gaps; a silence line would
    # claim a wait the rest of the view cannot corroborate.
    out = render_transcript(LINES, None, None, [], 53, silence_lines=[SilenceLine(53, 1.0, 9.0)])
    assert out == "53: その状態で\n54: この状態で今予備水持ってきたんで\n55: で、あとは下ろすだけ。"
