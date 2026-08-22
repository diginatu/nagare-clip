"""Tests for the cross-video context the director injects (build_director_context)."""

from __future__ import annotations

from nagare_clip.director.context import (
    SEAM_NOTE,
    Seam,
    build_director_context,
    seam_lines,
)
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
        ctx = build_director_context(_project(), _directions(), "a")
        assert "A two-video tutorial." in ctx
        # own parts with line ranges, summaries, and directions
        assert "1-4" in ctx and "intro" in ctx and "keep" in ctx
        assert "5-9" in ctx and "demo" in ctx and "speed up" in ctx
        # the other video appears as sibling context, not as an "own" part
        assert "wrap" in ctx

    def test_other_video_direction_not_attached_to_own(self):
        # The "remove" direction belongs to video b, so when building for "a"
        # it must not appear against a's parts.
        ctx_a = build_director_context(_project(), _directions(), "a")
        own_section = ctx_a.split("Other videos:")[0]
        assert "remove" not in own_section

    def test_empty_overview_returns_empty(self):
        assert build_director_context(ProjectSummary("", []), [], "a") == ""

    def test_unknown_stem_with_summary_still_renders_global(self):
        ctx = build_director_context(_project(), _directions(), "zzz")
        assert "A two-video tutorial." in ctx


def test_this_video_summary_line():
    ps = ProjectSummary(
        summary="overall",
        parts=[PartSummary(stem="A", lines=(1, 2), summary="a1")],
        video_summaries={"A": "video A overview"},
    )
    ctx = build_director_context(ps, [], "A")
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
    ctx = build_director_context(ps, [], "A")
    assert "- B: video B overview" in ctx
    assert "b-first-part" not in ctx


def test_byte_identical_without_video_summaries():
    parts = [
        PartSummary(stem="A", lines=(1, 2), summary="a1"),
        PartSummary(stem="B", lines=(1, 3), summary="b1"),
    ]
    ps = ProjectSummary(summary="overall", parts=parts)  # video_summaries == {}
    ctx = build_director_context(ps, [], "A")
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
        ctx = build_director_context(_seven(), [], "c", all_stems=["a", "b", "c"])
        assert 'This video ("c") — video 3 of 3, the LAST in the finished timeline:' in ctx

    def test_first_video_is_named_as_the_first(self):
        ctx = build_director_context(_seven(), [], "a", all_stems=["a", "b", "c"])
        assert 'This video ("a") — video 1 of 3, the FIRST in the finished timeline:' in ctx

    def test_middle_video_gets_only_its_index(self):
        ctx = build_director_context(_seven(), [], "b", all_stems=["a", "b", "c"])
        assert 'This video ("b") — video 2 of 3:' in ctx
        assert "FIRST" not in ctx and "LAST" not in ctx

    def test_siblings_split_into_earlier_and_later_in_timeline_order(self):
        ctx = build_director_context(_seven(), [], "b", all_stems=["a", "b", "c"])
        assert "Earlier in the finished video (already edited):\n- 1. a: video a" in ctx
        assert "Later in the finished video:\n- 3. c: video c" in ctx
        assert "Other videos:" not in ctx

    def test_last_video_has_nothing_later(self):
        ctx = build_director_context(_seven(), [], "c", all_stems=["a", "b", "c"])
        assert "Later in the finished video:\n- (none)" in ctx

    def test_first_video_has_nothing_earlier(self):
        ctx = build_director_context(_seven(), [], "a", all_stems=["a", "b", "c"])
        assert "Earlier in the finished video (already edited):\n- (none)" in ctx

    def test_concatenation_is_stated_once(self):
        ctx = build_director_context(_seven(), [], "b", all_stems=["a", "b", "c"])
        assert (
            "All videos below are concatenated into ONE finished video in this order; "
            "you are editing only video 2 of them." in ctx
        )

    def test_sibling_without_a_summary_renders_as_the_stem_alone(self):
        project = ProjectSummary(summary="S", parts=[PartSummary("a", (1, 2), "intro")])
        ctx = build_director_context(project, [], "a", all_stems=["a", "z"])
        assert ctx.endswith("Later in the finished video:\n- 2. z")

    def test_unknown_stem_falls_back_to_the_flat_sibling_list(self):
        """A stem the orchestrator does not list has no position to state."""
        ctx = build_director_context(_seven(), [], "b", all_stems=["x", "y"])
        assert ctx == build_director_context(_seven(), [], "b")

    def test_single_video_project_has_no_position_block(self):
        """With one source there is no timeline order to explain."""
        project = ProjectSummary(summary="S", parts=[PartSummary("a", (1, 2), "intro")])
        assert build_director_context(project, [], "a", all_stems=["a"]) == (
            build_director_context(project, [], "a")
        )

    def test_without_all_stems_the_block_is_byte_identical_to_before(self):
        ctx = build_director_context(_seven(), _directions(), "a")
        assert "Other videos:" in ctx
        assert "finished timeline" not in ctx and "Earlier in" not in ctx


# --- captions already committed upstream -------------------------------------


class TestPriorCaptions:
    def test_captions_are_listed_in_order(self):
        ctx = build_director_context(
            _seven(), [], "c", all_stems=["a", "b", "c"], prior_captions=["前回の装置", "水浸し"]
        )
        assert (
            "Captions already shown earlier in the finished video:\n- 前回の装置\n- 水浸し" in ctx
        )

    def test_no_block_when_there_are_none(self):
        ctx = build_director_context(_seven(), [], "a", all_stems=["a", "b", "c"])
        assert "Captions already shown" not in ctx

    def test_only_the_most_recent_are_kept(self):
        ctx = build_director_context(
            _seven(),
            [],
            "c",
            all_stems=["a", "b", "c"],
            prior_captions=["one", "two", "three"],
            max_prior_captions=2,
        )
        assert "- two\n- three" in ctx
        assert "- one" not in ctx

    def test_zero_limit_keeps_every_caption(self):
        ctx = build_director_context(
            _seven(),
            [],
            "c",
            all_stems=["a", "b", "c"],
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
            "a",
        )
        line = next(ln for ln in ctx.splitlines() if ln.startswith("- lines 5-9"))
        assert "direction" in line
        assert "lines 5-6: digression — remove" in ctx
        assert "lines 7-9: the demo itself — feature" in ctx

    def test_a_lone_narrow_direction_still_states_its_range(self):
        ctx = build_director_context(
            _project(), [PartDirection("a", (7, 9), "the demo itself")], "a"
        )
        assert "lines 7-9: the demo itself" in ctx
        assert "- lines 5-9: demo → direction: the demo itself" not in ctx

    def test_whole_part_direction_renders_as_before(self):
        ctx = build_director_context(_project(), _directions(), "a")
        assert "- lines 1-4: intro → direction: keep" in ctx

    def test_direction_of_another_part_is_not_attached(self):
        ctx = build_director_context(
            _project(), [PartDirection("a", (1, 2), "only the opening")], "a"
        )
        demo = next(ln for ln in ctx.splitlines() if ln.startswith("- lines 5-9"))
        assert "only the opening" not in demo


# --- seam context: what plays either side of this video -----------------------


class TestSeamLines:
    """The neighbour's lines to show, taken straight off its _edits.txt."""

    def test_before_seam_takes_the_last_lines_with_their_numbers(self):
        lines = ["one", "two", "three", "four", "five"]
        assert seam_lines(lines, 2, last=True) == [(4, "four"), (5, "five")]

    def test_after_seam_takes_the_first_lines(self):
        lines = ["one", "two", "three"]
        assert seam_lines(lines, 2, last=False) == [(1, "one"), (2, "two")]

    def test_blank_lines_are_skipped_but_numbering_is_absolute(self):
        lines = ["one", "  ", "three", ""]
        assert seam_lines(lines, 2, last=True) == [(1, "one"), (3, "three")]

    def test_editing_markers_are_stripped(self):
        lines = ["<keep>{{ほんじつ->本日}}は</keep>"]
        assert seam_lines(lines, 1, last=True) == [(1, "本日は")]

    def test_a_count_beyond_the_file_yields_every_line(self):
        assert seam_lines(["one"], 5, last=True) == [(1, "one")]

    def test_a_non_positive_count_yields_nothing(self):
        assert seam_lines(["one", "two"], 0, last=True) == []


class TestSeamContext:
    def test_before_seam_renders_the_neighbours_closing_lines(self):
        ctx = build_director_context(
            _project(),
            [],
            "b",
            all_stems=["a", "b"],
            seam_before=Seam("a", [(74, "また続きになります"), (75, "今日は終わりじゃあねー")]),
        )
        assert "Immediately BEFORE this video in the finished video (a, its last lines):" in ctx
        assert "- 74: また続きになります" in ctx
        assert "- 75: 今日は終わりじゃあねー" in ctx

    def test_after_seam_renders_the_neighbours_opening_lines(self):
        ctx = build_director_context(
            _project(),
            [],
            "a",
            all_stems=["a", "b"],
            seam_after=Seam("b", [(1, "こんにちはデジナです")]),
        )
        assert "Immediately AFTER this video (b, its first lines):" in ctx
        assert "- 1: こんにちはデジナです" in ctx

    def test_the_first_video_renders_only_the_after_side(self):
        ctx = build_director_context(
            _project(), [], "a", all_stems=["a", "b"], seam_after=Seam("b", [(1, "つづき")])
        )
        assert "Immediately AFTER this video" in ctx
        assert "Immediately BEFORE this video" not in ctx

    def test_the_rule_states_what_the_seam_is_for_once(self):
        ctx = build_director_context(
            _project(),
            [],
            "b",
            all_stems=["a", "b", "c"],
            seam_before=Seam("a", [(9, "じゃあねー")]),
            seam_after=Seam("c", [(1, "こんにちは")]),
        )
        assert ctx.count(SEAM_NOTE) == 1

    def test_no_seams_leaves_the_block_byte_identical(self):
        before = build_director_context(_project(), _directions(), "a", all_stems=["a", "b"])
        after = build_director_context(
            _project(),
            _directions(),
            "a",
            all_stems=["a", "b"],
            seam_before=None,
            seam_after=None,
        )
        assert after == before

    def test_a_seam_alone_still_renders_a_block(self):
        """A project with no summary at all still gets the seam context."""
        ctx = build_director_context(
            ProjectSummary("", []), [], "a", seam_after=Seam("b", [(1, "こんにちは")])
        )
        assert "- 1: こんにちは" in ctx


def test_the_seam_note_forbids_addressing_the_neighbours_line_numbers():
    """The neighbour's lines carry ITS numbering, printed beside this video's own
    numbered transcript; without a rule an op could be aimed at the wrong video."""
    assert "not part of your transcript" in SEAM_NOTE.lower()
    assert "never" in SEAM_NOTE.lower()
