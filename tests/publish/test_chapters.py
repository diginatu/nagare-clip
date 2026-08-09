"""publish/chapters: the four conditions YouTube needs to render chapters.

first entry exactly 0:00, at least three entries, ascending order, every
chapter at least 10 seconds. The stage satisfies them rather than hoping the
timeline mapping happens to land that way -- but it always writes the list,
qualifying or not, because YouTube auto-links timestamps regardless.
"""

from __future__ import annotations

from nagare_clip.publish.chapters import (
    Chapter,
    build_chapters,
    chapter_issues,
    format_timestamp,
    render_chapter_lines,
)


def test_format_timestamp_uses_m_ss_under_an_hour():
    assert format_timestamp(0.0) == "0:00"
    assert format_timestamp(5.4) == "0:05"
    assert format_timestamp(65.0) == "1:05"
    assert format_timestamp(600.0) == "10:00"


def test_format_timestamp_uses_h_mm_ss_past_an_hour():
    assert format_timestamp(3600.0) == "1:00:00"
    assert format_timestamp(3725.0) == "1:02:05"


def test_format_timestamp_truncates_rather_than_rounds_up():
    """Rounding 9.9s up to 0:10 would point past the chapter's own start."""
    assert format_timestamp(9.9) == "0:09"


def test_chapters_are_kept_when_all_are_long_enough():
    entries = [(0.0, "intro"), (30.0, "build"), (90.0, "test")]
    chapters = build_chapters(entries, total=150.0)
    assert [(c.time, c.title) for c in chapters] == entries


def test_short_chapter_merges_into_the_previous_one():
    """A part compressed inside a timelapse lands well under 10s: it is the
    part whose own span is short that gives way, and the previous title
    stretches over it."""
    entries = [(0.0, "intro"), (30.0, "build"), (86.0, "timelapse tail"), (90.0, "test")]
    chapters = build_chapters(entries, total=150.0)
    assert [c.title for c in chapters] == ["intro", "build", "test"]
    assert [c.time for c in chapters] == [0.0, 30.0, 90.0]


def test_short_final_chapter_merges_into_the_previous_one():
    """The last part of a source is often a few seconds of source before
    compression -- measured from the total, not from a following entry."""
    entries = [(0.0, "intro"), (30.0, "build"), (90.0, "outro")]
    chapters = build_chapters(entries, total=94.0)
    assert [c.title for c in chapters] == ["intro", "build"]


def test_short_first_chapter_merges_forward():
    """Chapter one has no previous neighbour, so it gives way to the next --
    which then inherits 0:00 when the list is rendered."""
    entries = [(0.0, "cold open"), (4.0, "build"), (60.0, "test")]
    chapters = build_chapters(entries, total=120.0)
    assert [c.title for c in chapters] == ["build", "test"]
    assert render_chapter_lines(chapters)[0] == "0:00 build"


def test_first_chapter_is_measured_from_zero_not_from_its_own_start():
    """The opening of part one is usually cut, so it starts late; the rendered
    chapter still runs from 0:00, so that is the span that must clear 10s."""
    entries = [(8.0, "intro"), (14.0, "build"), (60.0, "test")]
    chapters = build_chapters(entries, total=120.0)
    assert [c.title for c in chapters] == ["intro", "build", "test"]


def test_merging_is_transitive():
    """Dropping a short chapter lengthens the one before it, so a run of short
    chapters collapses into the first survivor -- and "d", too short at 2s
    while "c" was there, clears the bar once "c" is gone."""
    entries = [(0.0, "a"), (12.0, "b"), (14.0, "c"), (16.0, "d"), (60.0, "e")]
    chapters = build_chapters(entries, total=120.0)
    assert [(c.time, c.title) for c in chapters] == [(0.0, "a"), (16.0, "d"), (60.0, "e")]


def test_non_ascending_entries_are_dropped():
    entries = [(0.0, "a"), (60.0, "b"), (30.0, "stale"), (120.0, "c")]
    chapters = build_chapters(entries, total=180.0)
    assert [c.title for c in chapters] == ["a", "b", "c"]


def test_everything_too_short_collapses_to_one_chapter():
    entries = [(0.0, "a"), (2.0, "b"), (4.0, "c")]
    chapters = build_chapters(entries, total=6.0)
    assert [c.title for c in chapters] == ["c"]


def test_empty_input_gives_no_chapters():
    assert build_chapters([], total=100.0) == []


def test_render_forces_the_first_entry_to_zero():
    chapters = [Chapter(11.4, "intro"), Chapter(65.0, "build")]
    assert render_chapter_lines(chapters) == ["0:00 intro", "1:05 build"]


def test_render_of_nothing_is_empty():
    assert render_chapter_lines([]) == []


def test_issues_are_empty_when_youtube_will_render_chapters():
    chapters = build_chapters([(0.0, "a"), (30.0, "b"), (60.0, "c")], total=90.0)
    assert chapter_issues(chapters, total=90.0) == []


def test_issues_report_too_few_chapters():
    chapters = build_chapters([(0.0, "a"), (30.0, "b")], total=60.0)
    issues = chapter_issues(chapters, total=60.0)
    assert len(issues) == 1
    assert "3" in issues[0]


def test_issues_report_a_short_span_that_merging_could_not_fix():
    chapters = build_chapters([(0.0, "a"), (2.0, "b")], total=4.0)
    issues = chapter_issues(chapters, total=4.0)
    assert any("10" in issue for issue in issues)
