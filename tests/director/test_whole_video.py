"""Pure pieces of ``director.whole_project_context``."""

from __future__ import annotations

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.director.context import (
    PriorEdits,
    format_prior_edits,
    qualify_line_numbers,
    whole_video_block,
)
from nagare_clip.director.director_llm import DirectorOp, speech_seconds
from nagare_clip.order import Segment


class TestQualifyLineNumbers:
    def test_numbered_lines_gain_the_segment_index(self):
        assert qualify_line_numbers("12: あ  [1.0s]\n13: い", 4) == "[4]12: あ  [1.0s]\n[4]13: い"

    def test_annotation_lines_stay_unnumbered(self):
        text = "1: あ\n    [silent gap: 1: 見える数字]\n2: い"
        assert (
            qualify_line_numbers(text, 2) == "[2]1: あ\n    [silent gap: 1: 見える数字]\n[2]2: い"
        )

    def test_a_number_inside_the_text_is_left_alone(self):
        assert qualify_line_numbers("5: 10: 30に始める", 1) == "[1]5: 10: 30に始める"


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


class TestWholeVideoBlock:
    def test_segments_then_the_total(self):
        block = whole_video_block(
            [
                (Segment("a", None), "[1]1: あ", 90.0),
                (Segment("b", (4, 6)), "[2]4: い", 48.0),
            ]
        )
        assert block.split("\n\n")[1:] == [
            "[1] a — default runtime 1.5 min\n[1]1: あ",
            "[2] b [4-6] — default runtime 0.8 min\n[2]4: い",
            "Whole video — default runtime 2.3 min",
        ]

    def test_an_unknown_runtime_is_not_invented(self):
        block = whole_video_block(
            [(Segment("a", None), "[1]1: あ", None), (Segment("b", None), "[2]1: い", 60.0)]
        )
        # A total over only the timed segments would understate the video.
        assert block.split("\n\n")[1:] == [
            "[1] a\n[1]1: あ",
            "[2] b — default runtime 1.0 min\n[2]1: い",
        ]

    def test_the_note_comes_first(self):
        from nagare_clip.director.context import WHOLE_VIDEO_NOTE

        assert whole_video_block([(Segment("a", None), "[1]1: あ", 6.0)]).startswith(
            WHOLE_VIDEO_NOTE + "\n\n[1] a"
        )


class TestFormatPriorEdits:
    def _one(self, *ops):
        return format_prior_edits([PriorEdits(3, Segment("v", (10, 90)), list(ops))])

    def test_a_timelapse_shows_range_factor_and_caption(self):
        op = DirectorOp(type="timelapse", lines=(10, 85), factor=18.0, text="配管交換", note="n")
        assert self._one(op) == ["[3] v [10-90]: timelapse [3]10-[3]85 x18 「配管交換」"]

    def test_a_fractional_factor_is_kept(self):
        op = DirectorOp(type="timelapse", lines=(10, 12), factor=4.5)
        assert self._one(op) == ["[3] v [10-90]: timelapse [3]10-[3]12 x4.5"]

    def test_a_single_line_op_names_one_line(self):
        op = DirectorOp(type="overlay", lines=(86, 86), text="a", duration=2.0)
        assert self._one(op) == ["[3] v [10-90]: overlay [3]86 「a」"]

    def test_a_multiline_caption_stays_on_one_line(self):
        op = DirectorOp(type="overlay", lines=(86, 86), text="上\n下", duration=2.0)
        assert self._one(op) == ["[3] v [10-90]: overlay [3]86 「上 / 下」"]

    def test_notes_are_not_shown_and_ops_are_line_ordered(self):
        ops = [
            DirectorOp(type="cut", lines=(20, 21), note="長い説明"),
            DirectorOp(type="keep", lines=(11, 12), note="見せ場"),
        ]
        assert self._one(*ops) == ["[3] v [10-90]: keep [3]11-[3]12; cut [3]20-[3]21"]

    def test_a_segment_with_no_ops_says_so(self):
        assert self._one() == ["[3] v [10-90]: (no edits)"]


def test_the_config_flag_validates():
    cfg = get_effective_config(None, {"director": {"whole_project_context": True}})
    assert cfg["director"]["whole_project_context"] is True


class TestWholeVideoReference:
    def _files(self, tmp_path, stem, times, cuts=()):
        import json

        from nagare_clip.audio_silence.cuts_file import write_cuts
        from nagare_clip.director.run import SegmentInputs

        edits = tmp_path / f"{stem}_edits.txt"
        edits.write_text("\n".join(f"{stem}{i}" for i in range(len(times))) + "\n", "utf-8")
        js = tmp_path / f"{stem}.json"
        js.write_text(json.dumps({"segments": [{"start": s, "end": e} for s, e in times]}))
        cuts_txt = tmp_path / f"{stem}_cuts.txt"
        write_cuts(cuts_txt, list(cuts))
        return lambda segment: SegmentInputs(segment, edits, json_path=js, cuts_txt=cuts_txt)

    def test_the_default_runtime_drops_the_cut_silence(self, tmp_path):
        from nagare_clip.director.run import load_segment_transcript

        inputs = self._files(tmp_path, "a", [(0.0, 60.0), (61.0, 91.0)], cuts=[(10.0, 40.0)])
        assert load_segment_transcript(inputs(Segment("a", None))).default_runtime() == 60.0
        assert load_segment_transcript(inputs(Segment("a", (2, 2)))).default_runtime() == 30.0

    def test_a_missing_transcript_keeps_its_place(self, tmp_path):
        from nagare_clip.director.run import SegmentInputs, whole_video_reference

        b = self._files(tmp_path, "b", [(0.0, 6.0)])
        block = whole_video_reference(
            [SegmentInputs(Segment("a", None), tmp_path / "missing.txt"), b(Segment("b", None))]
        )
        assert block.split("\n\n")[1:] == [
            "[1] a",
            "[2] b — default runtime 0.1 min\n[2]1: b0  [6.0s]",
        ]

    def test_an_untimed_line_makes_the_runtime_unknown(self):
        # Counting it as zero would understate the segment, and the total with it.
        from nagare_clip.director.run import SegmentTranscript

        transcript = SegmentTranscript(["a", "b"], 1, [(0.0, 5.0), (None, 9.0)], None, [])
        assert transcript.default_runtime() is None
