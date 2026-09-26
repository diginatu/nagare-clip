"""The playback preview of an op that addresses a silence.

The numbers are PXL_20260426_090431216's: line 53 「その状態で」 ends at
501.897 s, line 54 starts at 531.779 s, and the 29.882 s between them is the
wait `timelapse ["53~","53~"] x5` compresses — 5.98 s on screen, proven end to
end through Blender.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.director.preview import preview_segment
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.gap_context.gaps import Gap

from .test_preview import PUMP_DESC, WATER, WATER_FIRST, WATER_GAPS, _block

SILENCE_53 = SilenceLine(53, 501.897, 531.779, (PUMP_DESC,))


def _preview(ops, silence_lines=(SILENCE_53,), gaps=None):
    return preview_segment(
        [r[3] for r in WATER],
        ops,
        seg_times=[(r[0], r[1]) for r in WATER],
        silences=[r[2] for r in WATER],
        anchored_gaps=WATER_GAPS if gaps is None else gaps,
        first_line=WATER_FIRST,
        silence_lines=list(silence_lines),
    )


def _timelapse(**kw):
    return DirectorOp(
        type="timelapse", lines=(53, 53), factor=5.0, text="予備水を用意している", **kw
    )


class TestASilenceOnlyOp:
    def test_it_plays_the_wait_and_only_the_wait(self):
        op = _timelapse(gap_start=True, gap_end=True)
        block = _block(_preview([op]).text, "timelapse [53,53] x5.0 「予備水を用意している」")
        # 29.882 s at x5 = 5.98 s on screen.
        assert "  plays 29.9 s of footage in 6.0 s" in block
        assert "caption on screen 6.0 s" in block

    def test_it_says_which_footage_that_is(self):
        op = _timelapse(gap_start=True, gap_end=True)
        block = _block(_preview([op]).text, "timelapse [53,53] x5.0 「予備水を用意している」")
        assert "  covers the silence after line 53 (501.9-531.8 s)" in block
        # ...named exactly as the transcript names it, description and all.
        assert f"    [silent 29.9s after line 53: {PUMP_DESC}]" in block

    def test_it_makes_no_speech_unintelligible(self):
        # The whole point of the form: line 53's 0.9 s of speech is OUTSIDE the
        # op, so nothing is sped up that anybody has to follow.
        op = _timelapse(gap_start=True, gap_end=True)
        block = _block(_preview([op]).text, "timelapse [53,53] x5.0 「予備水を用意している」")
        assert "unintelligible" not in block
        assert "その状態で" not in block

    def test_the_segment_runtime_counts_the_wait_at_the_factor(self):
        # Default runtime plus the 29.9 s wait restored at x5, minus nothing:
        # line 53 still plays its speech at 1x.
        plain = _preview([])
        op = _timelapse(gap_start=True, gap_end=True)
        with_op = _preview([op])
        assert round(with_op.runtime_seconds - plain.runtime_seconds, 1) == 6.0


class TestALinePlusItsTrailingSilence:
    def test_the_lines_speech_is_inside_it(self):
        op = _timelapse(gap_end=True)
        block = _block(_preview([op]).text, "timelapse [53,53] x5.0 「予備水を用意している」")
        assert "  covers line 53 and the silence after it (501.0-531.8 s)" in block
        # 500.953 -> 531.779 = 30.8 s, at x5 = 6.2 s.
        assert "  plays 30.8 s of footage in 6.2 s (default for line 53: 0.9 s)" in block

    def test_and_that_speech_is_priced(self):
        op = _timelapse(gap_end=True)
        block = _block(_preview([op]).text, "timelapse [53,53] x5.0 「予備水を用意している」")
        assert "  speech at x5.0 — unintelligible: line 53 0.9 s 「その状態で」" in block


class TestNamingTheSilence:
    def test_a_boundary_gap_is_named_as_the_transcript_names_it(self):
        # The op ends at line 53's last word, so the wait after it is dropped —
        # the fact this preview exists for.  It now uses the same words the
        # transcript does, so the director can connect the two (and address it
        # as "53~").
        op = _timelapse()
        block = _block(_preview([op]).text, "timelapse [53,53] x5.0 「予備水を用意している」")
        assert "  the silence after line 53 is outside this op — dropped" in block
        assert f"    [silent 29.9s after line 53: {PUMP_DESC}]" in block
        assert "gap before line 54" not in block

    def test_a_silence_with_no_line_of_its_own_is_still_priced(self):
        # 561.567 -> 564.787 after line 54 is 3.2 s, under the threshold: no
        # silence line, so the preview falls back to the segment times rather
        # than going quiet about it.
        op = DirectorOp(type="keep", lines=(54, 54))
        block = _block(_preview([op]).text, "keep [54,54]")
        assert "  the silence after line 54 is outside this op — dropped" in block
        assert "    [silent 3.2s after line 54]" in block

    def test_a_silence_another_op_holds_is_not_reported_as_dropped(self):
        # The preview's whole job at a boundary is what BECOMES of the silence;
        # a "53~" op next door keeps it, and saying "dropped" would send the
        # director looking for a problem that is already solved.
        rescue = DirectorOp(type="keep", lines=(53, 53), gap_start=True, gap_end=True)
        op = DirectorOp(type="timelapse", lines=(54, 55), factor=5.0, text="T")
        block = _block(_preview([rescue, op]).text, "timelapse [54,55] x5.0 「T」")
        assert (
            "  the silence after line 53 is outside this op — covered by keep [53,53] (op 1)"
            in block
        )


class TestWhatCannotBeApplied:
    def test_a_silence_after_the_segments_last_line_is_refused(self):
        # Line 55 is the last this segment plays; the wait after it belongs to
        # whatever plays next, and there is no next line here to end at — the
        # same refusal guided_edit makes: there is no silence line to hold it.
        op = DirectorOp(type="keep", lines=(55, 55), gap_start=True, gap_end=True)
        block = _block(_preview([op]).text, "keep [55,55]")
        assert "  this op's silence is outside the segment — it cannot be applied" in block
        assert " of footage " not in block


class TestWhereTheNumbersComeFrom:
    def test_the_silence_lines_interval_wins_over_the_segment_bounds(self):
        # WhisperX stretches a line's last word into the wait; the silence line
        # carries the capped interval the pipeline actually drops, and the
        # preview must price THAT, not the segment bound.
        capped = SilenceLine(53, 510.0, 531.779, ())
        op = _timelapse(gap_start=True, gap_end=True)
        block = _block(
            _preview([op], silence_lines=(capped,)).text,
            "timelapse [53,53] x5.0 「予備水を用意している」",
        )
        assert "  plays 21.8 s of footage in 4.4 s" in block

    def test_a_description_of_silence_inside_a_line_is_not_the_wait_after_it(self):
        inside = [(10, Gap(540.0, 545.0, description="inside line 54"))]
        op = DirectorOp(type="keep", lines=(53, 54))
        block = _block(
            _preview([op], silence_lines=(), gaps=inside).text,
            "keep [53,54]",
        )
        assert "the silence after line 54 is outside this op" in block
        assert "inside line 54" not in block
