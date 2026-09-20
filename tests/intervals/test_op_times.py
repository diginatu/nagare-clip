"""Ops that address a silence resolve to the exact time range the pipeline drops.

A `"n~"` edge cannot be written as a text marker in ``_edits.txt`` — there are
no words there to wrap — so these ops are resolved to times in code and handed
to ``run_intervals`` alongside the marker-derived ranges.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.intervals.op_times import resolve_op_times

# Three one-word lines with a 10 s wait between the first and the second.
WHISPERX = {
    "segments": [
        {"words": [{"word": "あ", "start": 1.0, "end": 1.4}]},
        {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        {"words": [{"word": "う", "start": 12.5, "end": 13.0}]},
    ]
}


def test_a_silence_only_keep_covers_exactly_the_dropped_gap():
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    assert resolve_op_times([op], WHISPERX).keeps == [(1.4, 11.4)]


def test_a_line_plus_its_trailing_silence_starts_at_the_words():
    op = DirectorOp(type="keep", lines=(1, 1), gap_end=True)
    assert resolve_op_times([op], WHISPERX).keeps == [(1.0, 11.4)]


def test_a_silence_then_the_lines_after_it_ends_at_the_words():
    op = DirectorOp(type="keep", lines=(1, 2), gap_start=True)
    assert resolve_op_times([op], WHISPERX).keeps == [(1.4, 12.0)]


def test_a_timelapse_yields_keep_speed_and_overlay_over_the_same_span():
    op = DirectorOp(
        type="timelapse", lines=(1, 1), gap_start=True, gap_end=True, factor=5.0, text="待ち"
    )
    times = resolve_op_times([op], WHISPERX)
    assert times.keeps == [(1.4, 11.4)]
    assert times.speeds == [(1.4, 11.4, 5.0)]
    # (start, duration, text), duration in EDITED seconds: 10.0 / 5.0.
    assert times.overlays == [(1.4, 2.0, "待ち")]


def test_a_timelapse_without_a_caption_still_speeds_the_span():
    op = DirectorOp(type="timelapse", lines=(1, 1), gap_start=True, gap_end=True, factor=5.0)
    times = resolve_op_times([op], WHISPERX)
    assert times.speeds == [(1.4, 11.4, 5.0)]
    assert times.overlays == []


def test_ops_without_a_silence_edge_are_left_to_the_markers():
    # The text markers already place these exactly; resolving them here too
    # would apply each one twice.
    ops = [
        DirectorOp(type="keep", lines=(1, 2)),
        DirectorOp(type="timelapse", lines=(1, 2), factor=5.0, text="x"),
        DirectorOp(type="cut", lines=(1, 1)),
    ]
    times = resolve_op_times(ops, WHISPERX)
    assert (times.keeps, times.speeds, times.overlays) == ([], [], [])


def test_a_cut_is_never_resolved_to_a_time_range():
    # Cuts stay on the marker path: keeps are grown back over caption words
    # there, and a time-only cut would lose that.
    # The factor is there on purpose: without it a cut falls out of the
    # resolver on the missing-factor branch instead of on the type gate, and
    # this test would pass even with "cut" wired into RESOLVED_TYPES.
    op = DirectorOp(type="cut", lines=(1, 1), gap_start=True, gap_end=True, factor=5.0)
    times = resolve_op_times([op], WHISPERX)
    assert (times.keeps, times.speeds, times.overlays) == ([], [], [])


def test_the_word_end_is_the_capped_one_the_pipeline_uses():
    # A word whose raw end runs deep into the wait.  build_speech_spans caps
    # every word, and the silence the pipeline drops starts at the capped end,
    # so the kept range must start there too.  On the real project the raw and
    # capped ends differ for 5 of 49 gaps, by 6 to 26 seconds.
    data = {
        "segments": [
            {"words": [{"word": "あ", "start": 1.0, "end": 9.0}]},
            {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        ]
    }
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    ((start, _end),) = resolve_op_times([op], data).keeps
    assert start < 9.0


def test_a_trailing_silence_on_the_last_line_is_skipped():
    # There is no next line to end at; dropping the op is better than guessing
    # an end time.
    op = DirectorOp(type="keep", lines=(3, 3), gap_start=True, gap_end=True)
    assert resolve_op_times([op], WHISPERX).keeps == []


def test_a_line_with_no_timed_words_is_skipped():
    data = {"segments": [{"words": [{"word": "あ"}]}, {"words": [{"word": "い", "start": 5.0}]}]}
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    assert resolve_op_times([op], data).keeps == []
