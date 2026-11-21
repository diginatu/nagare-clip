"""publish/chapters: the four conditions YouTube needs to render chapters.

first entry exactly 0:00, at least three entries, ascending order, every
chapter at least 10 seconds. The publish tools render the first entry as 0:00
and validate the rest (a short or duplicate chapter is a ``write`` problem,
never merged away) -- and always write the list, because YouTube auto-links
timestamps regardless.
"""

from __future__ import annotations

from nagare_clip.publish.chapters import (
    Chapter,
    _spans,
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


def test_first_chapter_is_measured_from_zero_not_from_its_own_start():
    """The opening of the first line is usually cut, so it starts late; the
    rendered chapter still runs from 0:00, so that is the span that counts."""
    assert _spans([8.0, 14.0, 60.0], 120.0) == [14.0, 46.0, 60.0]


def test_spans_of_nothing_are_empty():
    assert _spans([], 100.0) == []


def test_render_forces_the_first_entry_to_zero():
    chapters = [Chapter(11.4, "intro"), Chapter(65.0, "build")]
    assert render_chapter_lines(chapters) == ["0:00 intro", "1:05 build"]


def test_render_of_nothing_is_empty():
    assert render_chapter_lines([]) == []


def test_issues_are_empty_when_youtube_will_render_chapters():
    chapters = [Chapter(0.0, "a"), Chapter(30.0, "b"), Chapter(60.0, "c")]
    assert chapter_issues(chapters, total=90.0) == []


def test_issues_report_too_few_chapters():
    chapters = [Chapter(0.0, "a"), Chapter(30.0, "b")]
    issues = chapter_issues(chapters, total=60.0)
    assert len(issues) == 1
    assert "3" in issues[0]


def test_issues_report_a_short_span():
    chapters = [Chapter(0.0, "a"), Chapter(2.0, "b")]
    issues = chapter_issues(chapters, total=4.0)
    assert any("10" in issue for issue in issues)
