"""played_transcript's *when*: the publish view's finished times."""

from __future__ import annotations

from nagare_clip.director.display import build_display_view
from nagare_clip.director.load import SegmentTranscript
from nagare_clip.director.played import UNTIMED, played_transcript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.order import Segment


def _talk() -> SegmentTranscript:
    return SegmentTranscript(
        edit_lines=["one", "two", "three"],
        first_line=1,
        seg_times=[(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)],
        silences=None,
        gaps=[],
        silence_lines=[],
    )


def _untimed_with_silence() -> SegmentTranscript:
    """A source the preview cannot time, whose one line is a silence line."""
    return SegmentTranscript(
        edit_lines=[],
        first_line=1,
        seg_times=None,
        silences=None,
        gaps=[],
        silence_lines=[SilenceLine(after_line=0, start=0.0, end=45.0)],
    )


def test_a_line_with_no_finished_time_is_one_marker_row_not_a_stamp():
    t = _talk()
    view = build_display_view([(Segment("a", None), t)])
    out = played_transcript(view, [t], {}, when=lambda n: None if n in (2, 3) else f"T{n}")
    assert "T1  1: one" in out
    assert "    ✂ dropped lines 2-3 — not in the finished video" in out
    assert "✂  " not in out  # never a ✂ in the time column
    assert "two" not in out and "three" not in out


def test_without_when_nothing_changes_for_dropped_lines():
    t = _talk()
    view = build_display_view([(Segment("a", None), t)])
    out = played_transcript(view, [t], {})
    assert "1: one" in out and "2: two" in out and "3: three" in out
    assert "dropped" not in out


def test_the_untimed_fallback_lists_a_silence_line_that_plays():
    quiet, talk = _untimed_with_silence(), _talk()
    view = build_display_view([(Segment("q", None), quiet), (Segment("a", None), talk)])
    out = played_transcript(view, [quiet, talk], {}, when=lambda n: f"T{n}")
    assert UNTIMED in out
    assert "T1  1: [silent 45.0s]" in out


def test_the_untimed_fallback_skips_a_silence_line_that_does_not_play():
    quiet, talk = _untimed_with_silence(), _talk()
    view = build_display_view([(Segment("q", None), quiet), (Segment("a", None), talk)])
    out = played_transcript(view, [quiet, talk], {}, when=lambda n: None if n == 1 else f"T{n}")
    assert "silent 45.0s" not in out and "dropped" not in out


def test_the_untimed_fallback_without_when_still_omits_silence_lines():
    quiet, talk = _untimed_with_silence(), _talk()
    view = build_display_view([(Segment("q", None), quiet), (Segment("a", None), talk)])
    out = played_transcript(view, [quiet, talk], {})
    assert "silent 45.0s" not in out
