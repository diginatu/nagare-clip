"""The project context the whole-video conversation puts in its cached prefix.

The per-segment director received the project's overall summary and the plan's
directions for its own lines, with the "these are section boundaries" sentence
immediately above them.  The conversation numbers the whole video once, so the
same material has to arrive once, for every segment, **in display numbers** —
a source number shown to a model that reads display numbers is a trap.

The fixture is ``test_display``'s: source ``A`` plays as two non-adjacent
segments and its later lines play FIRST, so a source number is never a display
number and "same stem = same segment" dies.
"""

from __future__ import annotations

import logging

from nagare_clip.director.context import SECTION_BOUNDARY_NOTE, project_context_block
from nagare_clip.director.display import build_display_view
from nagare_clip.director.run import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.order import Segment
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

# Segment 1: A's lines 4-6, with a wait after line 5 (display 1,2,[3],4).
SEG_A_LATE = SegmentTranscript(
    edit_lines=["あ4", "あ5", "あ6"],
    first_line=4,
    seg_times=[(0.0, 1.0), (2.0, 3.0), (10.0, 11.0)],
    silences=None,
    gaps=[],
    silence_lines=[SilenceLine(5, 3.0, 10.0, ("ポンプを置く",))],
)
# Segment 2: the whole of source B, two lines (display 5,[6],7).
SEG_B = SegmentTranscript(
    edit_lines=["い1", "い2"],
    first_line=1,
    seg_times=[(0.0, 1.0), (7.0, 8.0)],
    silences=None,
    gaps=[],
    silence_lines=[SilenceLine(1, 1.0, 7.0, ())],
)
# Segment 3: A's lines 1-3 — the same source as segment 1, playing later
# (display 8,9,10).
SEG_A_EARLY = SegmentTranscript(
    edit_lines=["あ1", "あ2", "あ3"],
    first_line=1,
    seg_times=[(0.0, 1.0), (1.5, 2.0), (2.5, 3.0)],
    silences=None,
    gaps=[],
    silence_lines=[],
)

SEGMENTS = [Segment("A", (4, 6)), Segment("B", None), Segment("A", (1, 3))]
TRANSCRIPTS = [SEG_A_LATE, SEG_B, SEG_A_EARLY]


def _view():
    return build_display_view(list(zip(SEGMENTS, TRANSCRIPTS)))


def _summary(text="A pump repair, in three stretches."):
    return ProjectSummary(
        summary=text,
        parts=[PartSummary("A", (1, 6), "the work"), PartSummary("B", (1, 2), "the aside")],
        video_summaries={"A": "video A overview", "B": "video B overview"},
    )


def _block(directions, summary=None):
    return project_context_block(
        summary if summary is not None else _summary(), directions, SEGMENTS, _view()
    )


class TestWhatItCarries:
    def test_the_overall_summary_appears_once(self):
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        assert text.count("A pump repair, in three stretches.") == 1

    def test_every_segment_is_listed_with_its_label_in_playback_order(self):
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        heads = [line for line in text.splitlines() if line.startswith("[")]
        assert heads == ["[1] A [4-6]:", "[2] B:", "[3] A [1-3]:"]

    def test_a_segment_with_no_direction_says_so(self):
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        after_b = text.split("[2] B:\n")[1].splitlines()[0]
        assert after_b == "- (no directions)"

    def test_nothing_at_all_renders_nothing(self):
        assert project_context_block(ProjectSummary("", []), [], SEGMENTS, _view()) == ""


class TestDisplayNumbers:
    """The one thing this block must not get wrong."""

    def test_a_direction_is_rendered_in_display_numbers(self):
        # A's source lines 4-6 are display 1-4 (a silence line sits inside).
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        assert "- lines 1-4: timelapse the assembly" in text

    def test_the_source_numbers_never_appear_as_the_range(self):
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        assert "lines 4-6" not in text

    def test_the_later_segment_of_the_same_source_gets_its_own_numbers(self):
        # A's source lines 1-3 play THIRD, as display 8-10.
        text = _block([PartDirection("A", (1, 3), "let it run")])
        assert "- lines 8-10: let it run" in text
        assert "lines 1-3" not in text

    def test_one_direction_over_both_stretches_is_clipped_to_each(self):
        text = _block([PartDirection("A", (1, 6), "the whole repair")])
        assert "- lines 1-4: the whole repair" in text
        assert "- lines 8-10: the whole repair" in text

    def test_a_directions_range_ends_on_speech_not_on_a_silence_line(self):
        # Source line 5 is display 2; display 3 is the silence after it.
        text = _block([PartDirection("A", (4, 5), "the first words")])
        assert "- lines 1-2: the first words" in text

    def test_directions_are_listed_in_display_order(self):
        text = _block(
            [
                PartDirection("A", (6, 6), "then the last of it"),
                PartDirection("A", (4, 5), "the first words"),
            ]
        )
        assert text.index("the first words") < text.index("then the last of it")


class TestTheBoundaryNote:
    """The sentence a measured run needed: plan ranges are not op ranges.

    A real 9-segment run copied plan part boundaries into 19 of 56 op starts
    and into all three timelapses -- two of which therefore opened on the line
    where the speaker announces the work and played that announcement at 8-20x,
    unintelligible.  The sentence has to travel WITH the ranges, immediately
    above the list, not in the prompt's rules block, which is where it lost to
    the more concrete, later text.
    """

    def test_it_sits_immediately_above_the_list(self):
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        marker = "section boundaries, not op boundaries"
        assert marker in text
        assert text.index(marker) < text.index("[1] A [4-6]:")
        between = text.split(marker)[1].split("[1] ")[0]
        assert between.strip().endswith("means emphasis, not a keep op.")

    def test_it_names_the_word_keep_itself(self):
        # PLAN_PROMPT forbids "keep" in a direction; 7 of 9 real directions used
        # it anyway, and the director's own guardrail names only
        # featured/retained/emphasised.
        assert '"keep"' in SECTION_BOUNDARY_NOTE
        assert '"keep"' in _block([PartDirection("A", (4, 6), "keep light")])

    def test_no_directions_no_note(self):
        text = _block([])
        assert "section boundaries" not in text


class TestWhatIsDropped:
    """The whole-video view carries every segment's transcript in full, so the
    sibling summaries that described them from outside are redundant."""

    def test_no_earlier_or_later_sibling_block(self):
        text = _block([PartDirection("A", (4, 6), "timelapse the assembly")])
        assert "Earlier in the finished video" not in text
        assert "Later in the finished video" not in text
        assert "video A overview" not in text


class TestUnmappableDirections:
    def test_a_range_with_no_display_line_is_skipped_with_a_warning(self, caplog):
        # B is a whole-source segment of two lines; the plan claims nine.
        with caplog.at_level(logging.WARNING):
            text = _block([PartDirection("B", (1, 9), "the aside, as planned")])
        assert "the aside, as planned" not in text
        assert "lines 1-9" not in text
        assert any("B" in r.getMessage() and "1-9" in r.getMessage() for r in caplog.records)

    def test_a_direction_for_footage_that_does_not_play_is_left_out(self):
        text = _block([PartDirection("C", (1, 2), "cut footage")])
        assert "cut footage" not in text
