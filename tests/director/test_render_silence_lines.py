"""The rendered transcript with silence lines in it.

One silence, one number: the wait is stated in the silence line, and the
bracket of the line it follows drops its `gap` part.  Rendered through the
whole-video view, which is what the director reads.
"""

from __future__ import annotations

from nagare_clip.director.display import build_display_view
from nagare_clip.director.run import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.order import Segment

LINES = ["その状態で", "この状態で今予備水持ってきたんで", "で、あとは下ろすだけ。"]
TIMES = [(500.9, 501.9), (531.8, 561.6), (564.8, 566.4)]
SILENCES = [0.2, 13.4, 1.1]
PUMP = "a hand enters from the right holding a clear tube"


def _render(silence_lines=(), gaps=()):
    transcript = SegmentTranscript(
        edit_lines=LINES,
        first_line=53,
        seg_times=TIMES,
        silences=SILENCES,
        gaps=list(gaps),
        silence_lines=list(silence_lines),
    )
    view = build_display_view([(Segment("A", None), transcript)])
    # Drop the "[1] ..." segment header; the lines are what is under test.
    return view.render().split("\n")[1:]


def test_the_silence_line_follows_the_line_it_names():
    rendered = _render([SilenceLine(53, 501.897, 531.779, (PUMP,))])
    assert rendered[0] == "1: その状態で  [1.0s]"
    assert rendered[1] == f"2: [silent 29.9s: {PUMP}]"
    assert rendered[2].startswith("3: この状態で")


def test_the_gap_disappears_from_the_bracket_it_was_counted_in():
    # Without the silence line, line 53's bracket carries `gap 29.9s`; with it,
    # the same 29.9 s must not be printed twice.
    assert "gap 29.9s" in "\n".join(_render())
    with_line = "\n".join(_render([SilenceLine(53, 501.897, 531.779)]))
    assert "gap 29.9s" not in with_line
    assert "[silent 29.9s]" in with_line


def test_a_gap_with_no_silence_line_keeps_its_bracket():
    # 561.6 -> 564.8 after line 54 is 3.2 s: under the threshold, so it is
    # still the bracket's to report.
    assert "gap 3.2s" in "\n".join(_render([SilenceLine(53, 501.897, 531.779)]))


def test_silence_inside_a_line_stays_in_its_bracket():
    rendered = _render([SilenceLine(53, 501.897, 531.779)])
    assert (
        rendered[2]
        == "3: この状態で今予備水持ってきたんで  [16.4s speech, 13.4s silence, gap 3.2s]"
    )


def test_a_leftover_gap_annotation_stays_under_its_own_line():
    # A description of silence INSIDE line 54 is not the wait after it: both
    # can be shown, each under the line it belongs to.
    inside = (2, Gap(540.0, 545.0, description="inside line 54"))
    rendered = _render([SilenceLine(54, 561.6, 564.8)], gaps=[inside])
    assert rendered[1].startswith("2: この状態で")
    assert rendered[2] == "    [silent gap: inside line 54]"
    assert rendered[3] == "3: [silent 3.2s]"
