"""Tests for the cross-video context the director injects (build_director_context)."""

from __future__ import annotations

from nagare_clip.director.context import build_director_context
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
