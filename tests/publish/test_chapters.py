"""Chapters: anchoring on the finished timeline + YouTube's four conditions."""

from __future__ import annotations

from nagare_clip.publish.chapters import (
    apply_titles,
    build_chapters,
    chapter_issues,
    format_chapters,
    format_timestamp,
)
from nagare_clip.publish.timeline import build_edit_map, total_duration
from nagare_clip.summary.summarize import PartSummary


def _placed(keeps, stem="a", speeds=None):
    data = {"keep_intervals": [{"start": s, "end": e} for s, e in keeps]}
    if speeds:
        data["speed_ranges"] = [{"start": s, "end": e, "factor": f} for s, e, f in speeds]
    return build_edit_map([(stem, data)])


def _part(stem, start, end, summary="s"):
    return PartSummary(stem=stem, lines=(1, 2), summary=summary, start=start, end=end)


def test_timestamps_come_from_the_finished_timeline():
    placed = _placed([(0.0, 60.0), (120.0, 180.0)])
    parts = [_part("a", 0.0, 60.0, "one"), _part("a", 120.0, 180.0, "two")]
    chapters = build_chapters(parts, placed, total_duration(placed))
    # the second part starts 120s into the source but 60s into the cut
    assert [(c.start, c.title) for c in chapters] == [(0.0, "one"), (60.0, "two")]


def test_first_entry_is_forced_to_zero():
    # the opening 12s were cut, so the first part starts at 12s of source
    placed = _placed([(12.0, 60.0), (60.0, 200.0)])
    parts = [_part("a", 5.0, 60.0, "one"), _part("a", 60.0, 200.0, "two")]
    chapters = build_chapters(parts, placed, total_duration(placed))
    assert chapters[0].start == 0.0
    assert format_chapters(chapters)[0] == "0:00 one"


def test_part_cut_in_its_entirety_drops_out():
    placed = _placed([(0.0, 60.0), (200.0, 300.0)])
    parts = [
        _part("a", 0.0, 60.0, "one"),
        _part("a", 61.0, 199.0, "gone"),
        _part("a", 200.0, 300.0, "three"),
    ]
    titles = [c.title for c in build_chapters(parts, placed, total_duration(placed))]
    assert titles == ["one", "three"]


def test_short_chapter_merges_into_the_previous_one():
    # part two survives only 4s of finished video -> absorbed by part one
    placed = _placed([(0.0, 100.0), (100.0, 104.0), (200.0, 300.0)])
    parts = [
        _part("a", 0.0, 100.0, "one"),
        _part("a", 100.0, 104.0, "short"),
        _part("a", 200.0, 300.0, "three"),
    ]
    chapters = build_chapters(parts, placed, total_duration(placed))
    assert [c.title for c in chapters] == ["one", "three"]
    assert chapter_issues(chapters, total_duration(placed)) == [
        "only 2 timestamp(s); YouTube needs at least 3"
    ]


def test_short_first_chapter_merges_into_the_next():
    placed = _placed([(0.0, 4.0), (4.0, 100.0), (100.0, 200.0)])
    parts = [
        _part("a", 0.0, 4.0, "tiny intro"),
        _part("a", 4.0, 100.0, "two"),
        _part("a", 100.0, 200.0, "three"),
    ]
    chapters = build_chapters(parts, placed, total_duration(placed))
    assert [(c.start, c.title) for c in chapters] == [(0.0, "two"), (100.0, "three")]


def test_timelapse_compressed_part_still_gets_its_own_chapter_when_long_enough():
    # 0.4 min (24s) of source at 4x = 6s of output -> under the 10s floor
    placed = _placed([(0.0, 100.0), (100.0, 124.0)], speeds=[(100.0, 124.0, 4.0)])
    parts = [_part("a", 0.0, 100.0, "one"), _part("a", 100.0, 124.0, "timelapsed")]
    chapters = build_chapters(parts, placed, total_duration(placed))
    assert [c.title for c in chapters] == ["one"]


def test_min_chapter_zero_keeps_every_part():
    placed = _placed([(0.0, 100.0), (100.0, 104.0)])
    parts = [_part("a", 0.0, 100.0, "one"), _part("a", 100.0, 104.0, "short")]
    chapters = build_chapters(parts, placed, total_duration(placed), min_chapter=0.0)
    assert [c.title for c in chapters] == ["one", "short"]


def test_format_timestamp_switches_to_hours():
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(65.9) == "1:05"
    assert format_timestamp(3725) == "1:02:05"


def test_chapter_issues_reports_every_unmet_condition():
    placed = _placed([(0.0, 400.0)])
    parts = [
        _part("a", 0.0, 100.0, "one"),
        _part("a", 100.0, 200.0, "two"),
        _part("a", 200.0, 300.0, "three"),
    ]
    chapters = build_chapters(parts, placed, 400.0)
    assert chapter_issues(chapters, 400.0) == []
    # a hand-broken list: too few, not starting at 0:00, not ascending
    broken = [chapters[1], chapters[1]]
    issues = chapter_issues(broken, 400.0)
    assert issues[0].startswith("only 2 timestamp(s)")
    assert "first timestamp is not 0:00" in issues
    assert any("is not after the previous one" in i for i in issues)


def test_apply_titles_maps_by_position_and_keeps_the_rest():
    placed = _placed([(0.0, 300.0)])
    parts = [_part("a", 0.0, 100.0, "one"), _part("a", 100.0, 300.0, "two")]
    chapters = build_chapters(parts, placed, 300.0)
    out = apply_titles(chapters, {2: "short title", 9: "out of range"})
    assert [c.title for c in out] == ["one", "short title"]
    # part_index still points back at the summary.json part it came from
    assert [c.part_index for c in out] == [1, 2]
