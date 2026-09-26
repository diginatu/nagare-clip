"""One renderer for a silence line, whoever prints it.

The director's whole-video view and guided_edit's ``_edits.txt`` show the same
wait in the same text; if they had two formatters they could drift, and an
op the director placed on a line it read would land on a line that reads
differently.  Both go through :meth:`SilenceLine.body`, which is
:func:`nagare_clip.edit_lines.silence_body`.
"""

from __future__ import annotations

from nagare_clip import edit_lines
from nagare_clip.director import run as director_run
from nagare_clip.director import silence_lines
from nagare_clip.director.display import build_display_view
from nagare_clip.director.run import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.order import Segment


def test_director_modules_reexport_the_one_renderer_and_helpers():
    assert silence_lines.silence_body is edit_lines.silence_body
    assert silence_lines.gap_spans is edit_lines.gap_spans
    assert silence_lines.DEFAULT_SILENCE_LINE_MIN == edit_lines.DEFAULT_SILENCE_LINE_MIN
    assert director_run.silence_line_min is edit_lines.silence_line_min


def test_body_is_the_edit_file_bracket():
    line = SilenceLine(53, 0.0, 29.9, ("a hand", "a tube"))
    assert line.body() == "[silent 29.9s: a hand / a tube]"
    assert line.body() == edit_lines.silence_body(29.9, ("a hand", "a tube"))


def test_the_display_line_is_the_body():
    silence = SilenceLine(1, 1.0, 7.0, ("x] <keep>",))
    view = build_display_view(
        [
            (
                Segment("B", None),
                SegmentTranscript(["い1", "い2"], 1, [(0.0, 1.0), (7.0, 8.0)], None, [], [silence]),
            )
        ]
    )
    (shown,) = [line for line in view.lines if line.is_silence]
    assert shown.text == silence.body()
    # Escaped in the view too: one renderer, one escaping.
    assert shown.text == "[silent 6.0s: x\\] <keep>]"
    assert edit_lines.split_silence_line(shown.text) == ("", shown.text, "")
