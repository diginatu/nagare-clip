"""Tests for the cross-video context the director injects (build_director_context)."""

from __future__ import annotations

import re

from nagare_clip.director.context import (
    SEAM_NOTE,
    Seam,
    build_director_context,
    seam_lines,
)
from nagare_clip.order import Segment
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


def _project():
    return ProjectSummary(
        summary="A two-video tutorial.",
        parts=[
            PartSummary("a", (1, 4), "intro"),
            PartSummary("a", (5, 9), "demo"),
            PartSummary("b", (1, 3), "wrap"),
        ],
    )


def _directions():
    return [
        PartDirection("a", (1, 4), "keep"),
        PartDirection("a", (5, 9), "speed up"),
        PartDirection("b", (1, 3), "remove"),
    ]


class TestBuildDirectorContext:
    def test_includes_global_summary_and_own_parts(self):
        ctx = build_director_context(_project(), _directions(), Segment("a", None))
        assert "A two-video tutorial." in ctx
        # own parts with line ranges, summaries, and directions
        assert "1-4" in ctx and "intro" in ctx and "keep" in ctx
        assert "5-9" in ctx and "demo" in ctx and "speed up" in ctx
        # the other video appears as sibling context, not as an "own" part
        assert "wrap" in ctx

    def test_other_video_direction_not_attached_to_own(self):
        # The "remove" direction belongs to video b, so when building for "a"
        # it must not appear against a's parts.
        ctx_a = build_director_context(_project(), _directions(), Segment("a", None))
        own_section = ctx_a.split("Other videos:")[0]
        assert "remove" not in own_section

    def test_empty_overview_returns_empty(self):
        assert build_director_context(ProjectSummary("", []), [], Segment("a", None)) == ""

    def test_unknown_stem_with_summary_still_renders_global(self):
        ctx = build_director_context(_project(), _directions(), Segment("zzz", None))
        assert "A two-video tutorial." in ctx


def test_this_video_summary_line():
    ps = ProjectSummary(
        summary="overall",
        parts=[PartSummary(stem="A", lines=(1, 2), summary="a1")],
        video_summaries={"A": "video A overview"},
    )
    ctx = build_director_context(ps, [], Segment("A", None))
    assert "Summary: video A overview" in ctx


def test_sibling_uses_video_summary_when_present():
    ps = ProjectSummary(
        summary="overall",
        parts=[
            PartSummary(stem="A", lines=(1, 2), summary="a1"),
            PartSummary(stem="B", lines=(1, 3), summary="b-first-part"),
        ],
        video_summaries={"B": "video B overview"},
    )
    ctx = build_director_context(ps, [], Segment("A", None))
    assert "- B: video B overview" in ctx
    assert "b-first-part" not in ctx


def test_byte_identical_without_video_summaries():
    parts = [
        PartSummary(stem="A", lines=(1, 2), summary="a1"),
        PartSummary(stem="B", lines=(1, 3), summary="b1"),
    ]
    ps = ProjectSummary(summary="overall", parts=parts)  # video_summaries == {}
    ctx = build_director_context(ps, [], Segment("A", None))
    assert "Summary:" not in ctx
    assert "- B: b1" in ctx  # sibling falls back to first-part summary


# --- position in the finished timeline ---------------------------------------


def _seven():
    """A three-video project: parts for a and b, none for c."""
    return ProjectSummary(
        summary="A three-video build.",
        parts=[
            PartSummary("a", (1, 4), "intro"),
            PartSummary("b", (1, 3), "middle"),
            PartSummary("c", (1, 2), "wrap"),
        ],
        video_summaries={"a": "video a", "b": "video b", "c": "video c"},
    )


class TestTimelinePosition:
    """The sources are concatenated in `all_stems` order, so the director can be
    told which slice of the finished video it is editing."""

    def test_last_video_is_named_as_the_last(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("c", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert 'This segment ("c") — segment 3 of 3, the LAST in the finished timeline:' in ctx

    def test_first_video_is_named_as_the_first(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert 'This segment ("a") — segment 1 of 3, the FIRST in the finished timeline:' in ctx

    def test_middle_video_gets_only_its_index(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("b", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert 'This segment ("b") — segment 2 of 3:' in ctx
        assert "FIRST" not in ctx and "LAST" not in ctx

    def test_siblings_split_into_earlier_and_later_in_timeline_order(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("b", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert "Earlier in the finished video (already edited):\n- 1. a: video a" in ctx
        assert "Later in the finished video:\n- 3. c: video c" in ctx
        assert "Other videos:" not in ctx

    def test_last_video_has_nothing_later(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("c", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert "Later in the finished video:\n- (none)" in ctx

    def test_first_video_has_nothing_earlier(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert "Earlier in the finished video (already edited):\n- (none)" in ctx

    def test_concatenation_is_stated_once(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("b", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert (
            "All segments below are concatenated into ONE finished video in this order; "
            "you are editing only segment 2 of them." in ctx
        )

    def test_sibling_without_a_summary_renders_as_the_stem_alone(self):
        project = ProjectSummary(summary="S", parts=[PartSummary("a", (1, 2), "intro")])
        ctx = build_director_context(
            project, [], Segment("a", None), all_segments=[Segment("a", None), Segment("z", None)]
        )
        assert ctx.endswith("Later in the finished video:\n- 2. z")

    def test_unknown_stem_falls_back_to_the_flat_sibling_list(self):
        """A stem the orchestrator does not list has no position to state."""
        ctx = build_director_context(
            _seven(), [], Segment("b", None), all_segments=[Segment("x", None), Segment("y", None)]
        )
        assert ctx == build_director_context(_seven(), [], Segment("b", None))

    def test_single_video_project_has_no_position_block(self):
        """With one source there is no timeline order to explain."""
        project = ProjectSummary(summary="S", parts=[PartSummary("a", (1, 2), "intro")])
        assert build_director_context(
            project, [], Segment("a", None), all_segments=[Segment("a", None)]
        ) == (build_director_context(project, [], Segment("a", None)))

    def test_without_all_stems_the_block_is_byte_identical_to_before(self):
        ctx = build_director_context(_seven(), _directions(), Segment("a", None))
        assert "Other videos:" in ctx
        assert "finished timeline" not in ctx and "Earlier in" not in ctx


# --- captions already committed upstream -------------------------------------


class TestPriorCaptions:
    def test_captions_are_listed_in_order(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("c", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
            prior_captions=["前回の装置", "水浸し"],
        )
        assert (
            "Captions already shown earlier in the finished video:\n- 前回の装置\n- 水浸し" in ctx
        )

    def test_no_block_when_there_are_none(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        )
        assert "Captions already shown" not in ctx

    def test_only_the_most_recent_are_kept(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("c", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
            prior_captions=["one", "two", "three"],
            max_prior_captions=2,
        )
        assert "- two\n- three" in ctx
        assert "- one" not in ctx

    def test_zero_limit_keeps_every_caption(self):
        ctx = build_director_context(
            _seven(),
            [],
            Segment("c", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
            prior_captions=["one", "two", "three"],
            max_prior_captions=0,
        )
        assert "- one\n- two\n- three" in ctx


class TestSplitDirections:
    """A plan direction may cover only part of a summary part (a split part)."""

    def test_narrow_direction_renders_under_its_part_with_its_range(self):
        ctx = build_director_context(
            _project(),
            [
                PartDirection("a", (5, 6), "digression — remove"),
                PartDirection("a", (7, 9), "the demo itself — feature"),
            ],
            Segment("a", None),
        )
        line = next(ln for ln in ctx.splitlines() if ln.startswith("- lines 5-9"))
        assert "direction" in line
        assert "lines 5-6: digression — remove" in ctx
        assert "lines 7-9: the demo itself — feature" in ctx

    def test_a_lone_narrow_direction_still_states_its_range(self):
        ctx = build_director_context(
            _project(), [PartDirection("a", (7, 9), "the demo itself")], Segment("a", None)
        )
        assert "lines 7-9: the demo itself" in ctx
        assert "- lines 5-9: demo → direction: the demo itself" not in ctx

    def test_whole_part_direction_renders_as_before(self):
        ctx = build_director_context(_project(), _directions(), Segment("a", None))
        assert "- lines 1-4: intro → direction: keep" in ctx

    def test_direction_of_another_part_is_not_attached(self):
        ctx = build_director_context(
            _project(), [PartDirection("a", (1, 2), "only the opening")], Segment("a", None)
        )
        demo = next(ln for ln in ctx.splitlines() if ln.startswith("- lines 5-9"))
        assert "only the opening" not in demo


# --- seam context: what plays either side of this video -----------------------


class TestSeamLines:
    """The neighbour's lines to show, taken straight off its _edits.txt.

    Text only: a line number here would be the NEIGHBOUR's coordinate, and every
    op the director emits addresses its own transcript — a number it could copy
    would silently edit an unrelated line of this video.
    """

    def test_before_seam_takes_the_last_lines(self):
        lines = ["one", "two", "three", "four", "five"]
        assert seam_lines(lines, 2, last=True) == ["four", "five"]

    def test_after_seam_takes_the_first_lines(self):
        lines = ["one", "two", "three"]
        assert seam_lines(lines, 2, last=False) == ["one", "two"]

    def test_blank_lines_are_skipped(self):
        lines = ["one", "  ", "three", ""]
        assert seam_lines(lines, 2, last=True) == ["one", "three"]

    def test_editing_markers_are_stripped(self):
        lines = ["<keep>{{ほんじつ->本日}}は</keep>"]
        assert seam_lines(lines, 1, last=True) == ["本日は"]

    def test_a_count_beyond_the_file_yields_every_line(self):
        assert seam_lines(["one"], 5, last=True) == ["one"]

    def test_a_non_positive_count_yields_nothing(self):
        assert seam_lines(["one", "two"], 0, last=True) == []


class TestSeamContext:
    def test_before_seam_renders_the_neighbours_closing_lines(self):
        ctx = build_director_context(
            _project(),
            [],
            Segment("b", None),
            all_segments=[Segment("a", None), Segment("b", None)],
            seam_before=Seam("a", ["また続きになります", "今日は終わりじゃあねー"]),
        )
        assert "Immediately BEFORE this segment in the finished video (a, its last lines):" in ctx
        assert "- また続きになります" in ctx
        assert "- 今日は終わりじゃあねー" in ctx

    def test_after_seam_renders_the_neighbours_opening_lines(self):
        ctx = build_director_context(
            _project(),
            [],
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None)],
            seam_after=Seam("b", ["こんにちはデジナです"]),
        )
        assert "Immediately AFTER this segment (b, its first lines):" in ctx
        assert "- こんにちはデジナです" in ctx

    def test_no_line_number_is_rendered_beside_a_seam_line(self):
        """A number here is the neighbour's coordinate; copied into an op it would
        land on this video's line of that number instead."""
        ctx = build_director_context(
            _project(),
            [],
            Segment("b", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
            seam_before=Seam("a", ["じゃあねー"]),
            seam_after=Seam("c", ["こんにちは"]),
        )
        seam = ctx[ctx.index(SEAM_NOTE) :]
        assert not any(re.match(r"- ?\d+[:.]", line) for line in seam.splitlines())

    def test_the_first_video_renders_only_the_after_side(self):
        ctx = build_director_context(
            _project(),
            [],
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None)],
            seam_after=Seam("b", ["つづき"]),
        )
        assert "Immediately AFTER this segment" in ctx
        assert "Immediately BEFORE this segment" not in ctx

    def test_the_rule_states_what_the_seam_is_for_once(self):
        ctx = build_director_context(
            _project(),
            [],
            Segment("b", None),
            all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
            seam_before=Seam("a", ["じゃあねー"]),
            seam_after=Seam("c", ["こんにちは"]),
        )
        assert ctx.count(SEAM_NOTE) == 1

    def test_no_seams_leaves_the_block_byte_identical(self):
        before = build_director_context(
            _project(),
            _directions(),
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None)],
        )
        after = build_director_context(
            _project(),
            _directions(),
            Segment("a", None),
            all_segments=[Segment("a", None), Segment("b", None)],
            seam_before=None,
            seam_after=None,
        )
        assert after == before

    def test_a_seam_alone_still_renders_a_block(self):
        """A project with no summary at all still gets the seam context."""
        ctx = build_director_context(
            ProjectSummary("", []), [], Segment("a", None), seam_after=Seam("b", ["こんにちは"])
        )
        assert "- こんにちは" in ctx


def test_the_seam_note_says_the_lines_are_not_op_addressable():
    """The seam text sits beside this video's numbered transcript; the director
    must read it as context it cannot aim an op at."""
    note = SEAM_NOTE.lower()
    assert "not part of your transcript" in note
    # ops address this video's own numbering, which the seam text has no place in
    assert "your own numbered lines" in note


def test_part_list_says_a_plan_range_is_not_an_op_boundary():
    """The plan hands the director line-ranged directions, and the director
    copies their boundaries. Measured on a real 9-segment project: 19 of 56 op
    start lines sat exactly on a plan part boundary, and all three timelapses
    began on one -- including two whose first line was the speaker announcing
    the work, which the timelapse then rendered unintelligible.

    The prompt already tells the director to play a range back before settling
    it, but that rule sits at ~55% of the assembled prompt while these ranges
    arrive at ~82%; the more concrete, later text won. So the correction has to
    travel WITH the ranges, immediately above the list, not in the Rules block.

    It also carries the fix for a second leak: PLAN_PROMPT forbids the word
    "keep" in a direction, yet 7 of the 9 real directions used it anyway
    ("keep light", "keep a short visual moment"). The director's existing
    guardrail names only featured/retained/emphasised -- not the literal word
    it exists to neutralise."""
    summary = ProjectSummary(
        summary="overall",
        parts=[PartSummary("a", (1, 10), "work")],
        video_summaries={"a": "vid"},
    )
    directions = [PartDirection("a", (1, 10), "timelapse (4x+) — long assembly")]
    text = build_director_context(summary, directions, Segment(stem="a", lines=None))

    marker = "section boundaries, not op boundaries"
    assert marker in text
    # It must land immediately before the ranges it governs, not after them.
    assert text.index(marker) < text.index("- lines 1-10:")
    # And it must name "keep" itself, which the keep bullet's list omits.
    assert '"keep"' in text.split(marker)[1].split("- lines")[0]
