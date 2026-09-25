"""What one segment plays with no op at all, and the seconds it is made of.

These survived ``director.whole_project_context``, whose per-segment reference
block they were written for: the preview's whole-video runtime is the sum of
:meth:`SegmentTranscript.default_runtime` over the segments with no accepted
ops, and that runtime is :func:`speech_seconds` summed.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.config import DEFAULTS
from nagare_clip.director.director_llm import speech_seconds
from nagare_clip.director.run import SegmentInputs, SegmentTranscript, load_segment_transcript
from nagare_clip.order import Segment


class TestSpeechSeconds:
    def test_a_line_plays_its_span(self):
        assert speech_seconds([(0.0, 2.0), (3.0, 7.5)], None) == [2.0, 4.5]

    def test_cut_silence_does_not_play(self):
        assert speech_seconds([(0.0, 10.0), (11.0, 12.0)], [3.0, 0.0]) == [7.0, 1.0]

    def test_a_short_silence_is_still_cut(self):
        # The bracket folds a sub-second silence back in for display, but the
        # audio_silence cut is applied regardless — the runtime must not count it.
        assert speech_seconds([(0.0, 10.0)], [0.4]) == [pytest.approx(9.6)]

    def test_an_untimed_line_is_unknown(self):
        assert speech_seconds([(None, 2.0), (3.0, 4.0)], None) == [None, 1.0]


class TestDefaultRuntime:
    def _files(self, tmp_path, stem, times, cuts=()):
        edits = tmp_path / f"{stem}_edits.txt"
        edits.write_text("\n".join(f"{stem}{i}" for i in range(len(times))) + "\n", "utf-8")
        js = tmp_path / f"{stem}.json"
        js.write_text(json.dumps({"segments": [{"start": s, "end": e} for s, e in times]}))
        cuts_txt = tmp_path / f"{stem}_cuts.txt"
        write_cuts(cuts_txt, list(cuts))
        # No keep margins: the figure asserted is the cut list's alone.
        ivl = {**DEFAULTS["intervals"], "keep_pre_margin": 0.0, "keep_post_margin": 0.0}
        return lambda segment: SegmentInputs(
            segment, edits, json_path=js, cuts_txt=cuts_txt, intervals_cfg=ivl
        )

    def test_the_default_runtime_drops_the_cut_silence(self, tmp_path):
        inputs = self._files(tmp_path, "a", [(0.0, 60.0), (61.0, 91.0)], cuts=[(10.0, 40.0)])
        assert load_segment_transcript(inputs(Segment("a", None))).default_runtime() == 60.0
        assert load_segment_transcript(inputs(Segment("a", (2, 2)))).default_runtime() == 30.0

    def test_an_untimed_line_makes_the_runtime_unknown(self):
        # Counting it as zero would understate the segment, and the video.
        transcript = SegmentTranscript(["a", "b"], 1, [(0.0, 5.0), (None, 9.0)], None, [])
        assert transcript.default_runtime() is None


def _word_json(lines):
    """WhisperX JSON with one word per character; *lines* are (text, [(s, e)...])."""
    segments = []
    for text, spans in lines:
        segments.append(
            {
                "start": spans[0][0],
                "end": spans[-1][1],
                "text": text,
                "words": [{"word": ch, "start": s, "end": e} for ch, (s, e) in zip(text, spans)],
            }
        )
    return {"segments": segments}


class TestSilenceIsWhatIntervalsDrops:
    """A line's ``Ys silence`` is the footage of that line the render drops.

    Not only the audio_silence cut list: ``intervals`` also drops every word
    gap over ``silence_threshold`` and gives back its keep margins.  A figure
    built from the cut list alone told the director its op-free cut was 28.3
    minutes of a project whose op-free render is a different length entirely.
    """

    def _inputs(self, tmp_path, lines, cuts=(), ivl=None):
        edits = tmp_path / "a_edits.txt"
        edits.write_text("\n".join(text for text, _ in lines) + "\n", "utf-8")
        js = tmp_path / "a.json"
        js.write_text(json.dumps(_word_json(lines)), "utf-8")
        cuts_txt = tmp_path / "a_cuts.txt"
        write_cuts(cuts_txt, list(cuts))
        return SegmentInputs(
            Segment("a", None), edits, json_path=js, cuts_txt=cuts_txt, intervals_cfg=ivl
        )

    def _ivl(self, **over):
        return {**DEFAULTS["intervals"], **over}

    def test_a_word_gap_over_the_threshold_is_silence_without_any_cut(self, tmp_path):
        lines = [("あいうえ", [(0.0, 0.5), (0.5, 1.0), (6.0, 6.5), (6.5, 7.0)])]
        ivl = self._ivl(silence_threshold=1.5, keep_pre_margin=0.0, keep_post_margin=0.0)
        t = load_segment_transcript(self._inputs(tmp_path, lines, ivl=ivl))
        # 5.0 s of gap, less the 0.5 s a caption's min_duration keeps on screen.
        assert t.silences == [pytest.approx(4.5, abs=1e-3)]
        assert t.default_runtime() == pytest.approx(2.5, abs=1e-3)

    def test_speech_plus_silence_is_the_line_span(self, tmp_path):
        lines = [
            ("あいうえ", [(0.0, 0.5), (0.5, 1.0), (6.0, 6.5), (6.5, 7.0)]),
            ("かきくけ", [(9.0, 9.5), (9.5, 10.0), (14.0, 14.5), (14.5, 15.0)]),
        ]
        ivl = self._ivl(silence_threshold=1.5, keep_pre_margin=0.5, keep_post_margin=0.3)
        t = load_segment_transcript(self._inputs(tmp_path, lines, cuts=[(2.0, 5.0)], ivl=ivl))
        for (start, end), sil, speech in zip(
            t.seg_times, t.silences, speech_seconds(t.seg_times, t.silences)
        ):
            assert speech + sil == pytest.approx(end - start)
        # Margins give back 0.3 s after "い" and 0.5 s before "う".
        assert t.silences[0] == pytest.approx(5.0 - 0.8, abs=1e-3)

    def test_a_line_inside_a_cut_that_the_margins_restore_plays(self, tmp_path):
        # Source PXL_20260430_084048507 line 25: a 0.22 s line wholly inside an
        # audio_silence cut, played anyway because both neighbours' keep
        # margins reach over it.  The cut list alone priced it at 0.0 s.
        lines = [
            ("あい", [(8.0, 8.5), (8.5, 9.0)]),
            ("う", [(10.0, 10.22)]),
            ("えお", [(11.0, 11.5), (11.5, 12.0)]),
        ]
        t = load_segment_transcript(self._inputs(tmp_path, lines, cuts=[(9.0, 11.0)]))
        assert speech_seconds(t.seg_times, t.silences)[1] == pytest.approx(0.22, abs=1e-3)

    def test_no_cut_list_still_counts_the_word_gaps(self, tmp_path):
        lines = [("あいうえ", [(0.0, 0.5), (0.5, 1.0), (6.0, 6.5), (6.5, 7.0)])]
        inputs = self._inputs(
            tmp_path, lines, ivl=self._ivl(keep_pre_margin=0.0, keep_post_margin=0.0)
        )
        t = load_segment_transcript(replace(inputs, cuts_txt=None))
        assert t.silences == [pytest.approx(4.5, abs=1e-3)]

    def test_the_timeline_prints_what_renders_for_a_margin_restored_line(self, tmp_path):
        # The same line in the playback preview: its timeline row priced it at
        # 0.0 s while the bracket read [0.2s] and the render played 0.22 s.
        # A cut on line 2 makes it a run of its own, so its seconds are legible.
        from nagare_clip.director.director_llm import DirectorOp
        from nagare_clip.director.preview import preview_segment

        lines = [
            ("あい", [(8.0, 8.5), (8.5, 9.0)]),
            ("う", [(10.0, 10.22)]),
            ("えお", [(11.0, 11.5), (11.5, 12.0)]),
        ]
        t = load_segment_transcript(self._inputs(tmp_path, lines, cuts=[(9.0, 11.0)]))
        ops = [DirectorOp(type="cut", lines=(3, 3))]
        preview = preview_segment(
            t.edit_lines, ops, seg_times=t.seg_times, silences=t.silences, first_line=1
        )
        plain = [run for run in preview.runs if run.kind == "1x"]
        assert [run.lines for run in plain] == [[1, 2]]
        line1 = speech_seconds(t.seg_times, t.silences)[0]
        assert plain[0].seconds == pytest.approx(line1 + 0.22, abs=1e-3)
