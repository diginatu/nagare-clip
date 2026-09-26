"""The project context the conversation puts in its cached prefix.

Facts only: the overall summary and each source's whole-video summary.  The
plan stage's directions are NOT rendered any more — the director writes its own
plan in its first turn — so no instruction and no line range appears here.
"""

from __future__ import annotations

from nagare_clip.director.context import HEADER, project_context_block
from nagare_clip.director.display import build_display_view
from nagare_clip.order import Segment
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

from .test_loop_order import A, B


def _view():
    return build_display_view([(Segment("A"), A), (Segment("B"), B)])


def _summary(overall="", videos=None):
    return ProjectSummary(
        summary=overall,
        parts=[PartSummary(stem="A", lines=(1, 5), summary="パート")],
        video_summaries=videos or {},
    )


def test_nothing_to_say_is_empty():
    assert project_context_block(_summary(), _view()) == ""


def test_the_overall_summary_and_each_sources_summary():
    text = project_context_block(_summary("ポンプの修理", {"B": "Bの全体"}), _view())
    assert text == "\n".join([HEADER, "Overall: ポンプの修理", "[2] B: Bの全体"])


def test_a_source_summary_alone_is_enough():
    assert project_context_block(_summary(videos={"A": "Aの全体"}), _view()) == "\n".join(
        [HEADER, "[1] A: Aの全体"]
    )


def test_part_summaries_and_line_ranges_are_not_rendered():
    text = project_context_block(_summary("x", {"A": "y"}), _view())
    assert "パート" not in text
    assert "lines" not in text
