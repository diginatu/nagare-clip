"""What one segment plays with no op at all, and the seconds it is made of.

These survived ``director.whole_project_context``, whose per-segment reference
block they were written for: the preview's whole-video runtime is the sum of
:meth:`SegmentTranscript.default_runtime` over the segments with no accepted
ops, and that runtime is :func:`speech_seconds` summed.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.audio_silence.cuts_file import write_cuts
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
        return lambda segment: SegmentInputs(segment, edits, json_path=js, cuts_txt=cuts_txt)

    def test_the_default_runtime_drops_the_cut_silence(self, tmp_path):
        inputs = self._files(tmp_path, "a", [(0.0, 60.0), (61.0, 91.0)], cuts=[(10.0, 40.0)])
        assert load_segment_transcript(inputs(Segment("a", None))).default_runtime() == 60.0
        assert load_segment_transcript(inputs(Segment("a", (2, 2)))).default_runtime() == 30.0

    def test_an_untimed_line_makes_the_runtime_unknown(self):
        # Counting it as zero would understate the segment, and the video.
        transcript = SegmentTranscript(["a", "b"], 1, [(0.0, 5.0), (None, 9.0)], None, [])
        assert transcript.default_runtime() is None
