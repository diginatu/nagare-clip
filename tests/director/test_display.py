"""The global display view: the whole finished video under one numbering.

The fixture is the shape the real project has and the one every "same stem =
same segment" shortcut dies on: source ``A`` plays as TWO non-adjacent
segments, and its later lines play FIRST (``PXL_20260502_085157585`` is split
into 1-30, 84-97 and 31-83, in that playback order).
"""

from __future__ import annotations

from nagare_clip.director.display import DisplayLine, build_display_view
from nagare_clip.director.run import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.order import Segment

# Segment 1: A's lines 4-6, with a 7.0 s wait after line 5.
SEG_A_LATE = SegmentTranscript(
    edit_lines=["あ4", "あ5", "あ6"],
    first_line=4,
    seg_times=[(0.0, 1.0), (2.0, 3.0), (10.0, 11.0)],
    silences=None,
    gaps=[],
    silence_lines=[SilenceLine(5, 3.0, 10.0, ("ポンプを置く",))],
)
# Segment 2: the whole of source B, with a 6.0 s wait after its line 1.
SEG_B = SegmentTranscript(
    edit_lines=["い1", "い2"],
    first_line=1,
    seg_times=[(0.0, 1.0), (7.0, 8.0)],
    silences=None,
    gaps=[],
    silence_lines=[SilenceLine(1, 1.0, 7.0, ())],
)
# Segment 3: A's lines 1-3 — the same source as segment 1, playing later.
SEG_A_EARLY = SegmentTranscript(
    edit_lines=["あ1", "あ2", "あ3"],
    first_line=1,
    seg_times=[(0.0, 1.0), (1.5, 2.0), (2.5, 3.0)],
    silences=None,
    gaps=[],
    silence_lines=[],
)

SEGMENTS = [
    (Segment("A", (4, 6)), SEG_A_LATE),
    (Segment("B", None), SEG_B),
    (Segment("A", (1, 3)), SEG_A_EARLY),
]


def _view():
    return build_display_view(SEGMENTS)


class TestNumbering:
    def test_one_continuous_numbering_over_the_whole_video(self):
        view = _view()
        # 3 + 1 silence, 2 + 1 silence, 3 — numbered 1..10 with no restart.
        assert [line.number for line in view.lines] == list(range(1, 11))

    def test_silence_lines_are_numbered_alongside_the_speech(self):
        view = _view()
        assert [line.is_silence for line in view.lines] == [
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
        ]

    def test_a_line_knows_which_playback_segment_it_belongs_to(self):
        view = _view()
        assert [(line.segment, line.stem) for line in view.lines] == [
            (1, "A"),
            (1, "A"),
            (1, "A"),
            (1, "A"),
            (2, "B"),
            (2, "B"),
            (2, "B"),
            (3, "A"),
            (3, "A"),
            (3, "A"),
        ]

    def test_a_silence_line_names_the_line_it_follows(self):
        view = _view()
        silence = view.lines[2]
        assert (silence.source_line, silence.is_silence) == (5, True)
        assert "7.0s" in silence.text and "ポンプを置く" in silence.text

    def test_the_source_numbering_is_the_segments_own(self):
        view = _view()
        # Segment 3 is source A's lines 1-3 although it plays last.
        assert [line.source_line for line in view.lines[7:]] == [1, 2, 3]


class TestToSource:
    def test_a_speech_range_maps_to_plain_source_lines(self):
        assert _view().to_source(1, 2) == ("A", (4, 5), False, False)

    def test_a_range_beginning_on_a_silence_line_is_a_gap_start(self):
        # Display 3 is the wait after source line 5, display 4 is line 6.
        assert _view().to_source(3, 4) == ("A", (5, 6), True, False)

    def test_a_range_ending_on_a_silence_line_is_a_gap_end(self):
        assert _view().to_source(2, 3) == ("A", (5, 5), False, True)

    def test_a_silence_line_alone_is_both(self):
        assert _view().to_source(3, 3) == ("A", (5, 5), True, True)

    def test_a_range_crossing_a_segment_join_is_refused(self):
        # Display 4 is the last line of segment 1, display 5 the first of
        # segment 2: different footage, so the op would be meaningless.
        assert _view().to_source(4, 5) is None
        assert _view().to_source(7, 8) is None
        assert _view().to_source(1, 10) is None

    def test_the_same_stem_twice_is_still_two_segments(self):
        # Segments 1 and 3 are both source A; a range over both is refused,
        # and segment 3 maps to A's OWN line numbers, not to a continuation.
        assert _view().to_source(4, 8) is None
        assert _view().to_source(8, 10) == ("A", (1, 3), False, False)

    def test_a_range_outside_the_video_is_refused(self):
        view = _view()
        assert view.to_source(0, 2) is None
        assert view.to_source(9, 11) is None
        assert view.to_source(5, 4) is None


class TestSegmentJoinAfter:
    def test_true_exactly_where_the_footage_changes(self):
        view = _view()
        assert [view.segment_join_after(n) for n in range(1, 11)] == [
            False,
            False,
            False,
            True,  # segment 1 ends here
            False,
            False,
            True,  # segment 2 ends here
            False,
            False,
            False,  # the video ends here: nothing follows, so no join
        ]


class TestFromSource:
    def test_a_source_line_maps_back_to_its_display_number(self):
        view = _view()
        assert view.from_source(1, 5) == 2
        assert view.from_source(1, 5, silence=True) == 3
        assert view.from_source(3, 1) == 8

    def test_an_unknown_line_has_no_display_number(self):
        view = _view()
        assert view.from_source(3, 1, silence=True) is None
        assert view.from_source(4, 1) is None


class TestRender:
    def test_every_line_is_numbered_once_in_playback_order(self):
        text = _view().render()
        numbered = [line for line in text.split("\n") if line[:1].isdigit()]
        assert [line.split(":", 1)[0] for line in numbered] == [str(n) for n in range(1, 11)]

    def test_a_silence_line_reads_as_a_line_of_the_transcript(self):
        text = _view().render()
        assert "3: [silent 7.0s: ポンプを置く]" in text
        assert "6: [silent 6.0s]" in text

    def test_a_speech_line_keeps_its_timing_bracket(self):
        # Including the gap to the next line, which no silence line claims.
        assert "1: あ4  [1.0s, gap 1.0s]" in _view().render()
        # ...and NOT where a silence line states it: line 2's wait is line 3.
        assert "2: あ5  [1.0s]" in _view().render()

    def test_each_segment_is_announced_with_its_source(self):
        text = _view().render()
        assert "A [4-6]" in text and "B" in text and "A [1-3]" in text
        # The header sits above the segment's first line, not inside it.
        head = text.split("1: あ4")[0]
        assert "A [4-6]" in head


class TestAnnotations:
    def test_a_gap_description_no_silence_line_claimed_is_kept(self):
        # A silence INSIDE a line: the line's own bracket reports it, and the
        # description is still shown — 12 of the real project's 59 are these.
        transcript = SegmentTranscript(
            edit_lines=["あ1", "あ2"],
            first_line=1,
            seg_times=[(0.0, 10.0), (11.0, 12.0)],
            silences=[6.0, 0.0],
            gaps=[(1, Gap(2.0, 8.0, [], "手が映る"))],
            silence_lines=[],
        )
        view = build_display_view([(Segment("A", None), transcript)])
        assert view.lines[0].annotations == ("手が映る",)
        assert "    [silent gap: 手が映る]" in view.render()

    def test_a_description_before_the_first_line_is_kept_too(self):
        transcript = SegmentTranscript(
            edit_lines=["あ1"],
            first_line=1,
            seg_times=[(5.0, 6.0)],
            silences=None,
            gaps=[(0, Gap(0.0, 4.0, [], "タンクが映る"))],
            silence_lines=[],
        )
        view = build_display_view([(Segment("A", None), transcript)])
        assert view.lines[0].annotations == ()
        assert "    [silent gap: タンクが映る]" in view.render()

    def test_two_descriptions_on_one_line_both_show_in_order(self):
        transcript = SegmentTranscript(
            edit_lines=["あ1", "あ2"],
            first_line=1,
            seg_times=[(0.0, 20.0), (21.0, 22.0)],
            silences=[15.0, 0.0],
            gaps=[(1, Gap(2.0, 8.0, [], "一つ目")), (1, Gap(9.0, 15.0, [], "二つ目"))],
            silence_lines=[],
        )
        rendered = build_display_view([(Segment("A", None), transcript)]).render()
        assert "    [silent gap: 一つ目]\n    [silent gap: 二つ目]" in rendered


class TestUntimedSegment:
    def test_a_segment_with_no_timings_still_numbers_its_lines(self):
        transcript = SegmentTranscript(
            edit_lines=["あ1", "あ2"],
            first_line=1,
            seg_times=None,
            silences=None,
            gaps=[],
            silence_lines=[],
        )
        view = build_display_view([(Segment("A", None), transcript), SEGMENTS[1]])
        assert [line.number for line in view.lines] == [1, 2, 3, 4, 5]
        assert view.to_source(1, 2) == ("A", (1, 2), False, False)
        assert "1: あ1" in view.render()


class TestDisplayLine:
    def test_it_is_a_frozen_value(self):
        line = DisplayLine(1, 1, "A", 4, False, "あ4")
        assert (line.number, line.segment, line.stem, line.source_line) == (1, 1, "A", 4)
