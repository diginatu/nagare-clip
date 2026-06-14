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
