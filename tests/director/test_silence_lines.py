"""Silence lines: the wait between two transcript lines, shown as its own line.

The gap a silence line reports is the one the pipeline actually drops — the
space between two consecutive :func:`build_speech_spans` entries — so the
interval the director reads is byte-for-byte the interval ``"n~"`` resolves to.
"""

from __future__ import annotations

from nagare_clip.director.silence_lines import SilenceLine, build_silence_lines
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.intervals.sync_json import extract_keep_ranges

# Three lines: a 10.0 s wait after line 1, 0.5 s after line 2.
WHISPERX = {
    "segments": [
        {"words": [{"word": "あ", "start": 1.0, "end": 1.4}]},
        {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        {"words": [{"word": "う", "start": 12.5, "end": 13.0}]},
    ]
}


def _lines(data=WHISPERX, **kw):
    return build_silence_lines(data, **kw)[0]


def test_a_long_wait_between_two_lines_becomes_a_silence_line():
    (line,) = _lines()
    assert (line.after_line, line.start, line.end) == (1, 1.4, 11.4)
    assert line.duration == 10.0


def test_a_short_wait_does_not():
    # 12.0 -> 12.5 after line 2 is half a second: the bracket's business.
    assert [line.after_line for line in _lines()] == [1]


def test_the_threshold_is_configurable():
    assert _lines(min_seconds=10.0) == _lines()
    assert _lines(min_seconds=10.1) == []


def test_the_interval_is_the_one_a_silence_reference_resolves_to():
    # The whole point: what the director reads and what "1~" edits are the
    # same seconds, not two independent derivations of "the silence".
    # "1~" is a marker on the silence line guided_edit writes after line 1.
    (line,) = _lines()
    edits = ["あ", f"<keep>{line.body()}</keep>", "い", "う"]
    assert extract_keep_ranges(edits, WHISPERX) == [(line.start, line.end)]


def test_a_stretched_final_word_does_not_shorten_the_silence():
    # WhisperX runs a line's last word far into the wait; build_speech_spans
    # caps it, and the silence the pipeline drops starts at the cap.  On the
    # real project this differs for 5 of 49 gaps, by 6 to 26 seconds.
    data = {
        "segments": [
            {"words": [{"word": "あ", "start": 1.0, "end": 9.0}]},
            {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        ]
    }
    (line,) = _lines(data)
    assert line.start < 9.0


def test_a_description_whose_midpoint_falls_in_the_silence_becomes_its_text():
    gap = Gap(2.0, 8.0, description="a hand enters from the right")
    lines, leftover = build_silence_lines(WHISPERX, anchored_gaps=[(1, gap)])
    assert lines[0].descriptions == ("a hand enters from the right",)
    assert leftover == []


def test_several_descriptions_join_with_a_slash_in_time_order():
    early = Gap(2.0, 3.0, description="first")
    late = Gap(7.0, 8.0, description="second")
    (line,) = _lines(anchored_gaps=[(1, late), (1, early)])
    assert line.descriptions == ("first", "second")
    assert "first / second" in line.render()


def test_a_description_of_a_silence_inside_a_line_is_left_where_it_was():
    # Its midpoint is inside line 1's own span, which the bracket already
    # reports as `Ys silence`; it is not the wait after the line.
    inside = Gap(1.0, 1.3, description="inside the line")
    lines, leftover = build_silence_lines(WHISPERX, anchored_gaps=[(1, inside)])
    assert lines[0].descriptions == ()
    assert leftover == [(1, inside)]


def test_a_description_straddling_the_boundary_belongs_to_the_side_it_sits_on():
    # ffmpeg silencedetect and WhisperX disagree about where the wait starts,
    # so a described gap can overlap the silence line while its midpoint is
    # still inside the line's own span (2 of the real project's 59).  The
    # midpoint decides — the same rule anchor_gaps uses — so a description is
    # never claimed by a silence that holds only its tail.
    straddling = Gap(0.6, 1.6, description="mostly inside line 1")
    lines, leftover = build_silence_lines(WHISPERX, anchored_gaps=[(1, straddling)])
    assert lines[0].descriptions == ()
    assert leftover == [(1, straddling)]


def test_a_silence_line_with_no_description_still_renders():
    # The wait is real even when nothing visibly happens in it.
    (line,) = _lines()
    assert line.render() == "    [silent 10.0s after line 1]"


def test_a_described_silence_line_renders_its_description():
    (line,) = _lines(anchored_gaps=[(1, Gap(2.0, 8.0, description="デモが動く"))])
    assert line.render() == "    [silent 10.0s after line 1: デモが動く]"


def test_a_segment_only_owns_the_silences_between_its_own_lines():
    # `lines=(a, b)` is the segment's range: the silence after its last line
    # leads into the NEXT segment (gap_context.anchor_gaps' rule), and one
    # before its first line cannot be addressed as "n~" from inside it.
    data = {
        "segments": [
            {"words": [{"word": "あ", "start": 1.0, "end": 1.4}]},
            {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
            {"words": [{"word": "う", "start": 22.0, "end": 23.0}]},
            {"words": [{"word": "え", "start": 33.0, "end": 34.0}]},
        ]
    }
    assert [line.after_line for line in _lines(data)] == [1, 2, 3]
    assert [line.after_line for line in _lines(data, lines=(2, 3))] == [2]


def test_an_untimed_line_has_no_silence_around_it():
    data = {
        "segments": [
            {"words": [{"word": "あ", "start": 1.0, "end": 1.4}]},
            {"words": []},
            {"words": [{"word": "う", "start": 22.0, "end": 23.0}]},
        ]
    }
    assert _lines(data) == []


def test_silence_lines_are_a_value_with_a_duration():
    line = SilenceLine(after_line=53, start=501.897, end=531.779)
    assert round(line.duration, 3) == 29.882
    assert line.render() == "    [silent 29.9s after line 53]"
