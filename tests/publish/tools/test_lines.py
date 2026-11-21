"""Display line -> source span -> finished-timeline time."""

from __future__ import annotations

import json

from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools.lines import LineSpan, finished_placements, line_span, line_time


def test_speech_line_span(project):
    assert line_span(project, 1) == LineSpan("a", 1.0, 2.0)
    assert line_span(project, 3) == LineSpan("a", 5.0, 6.0)


def test_silence_line_span(project):
    assert line_span(project, 2) == LineSpan("a", 2.0, 5.0)


def test_out_of_range_is_none(project):
    assert line_span(project, 99) is None
    assert line_span(project, 0) is None


def test_line_time_uses_first_surviving_moment(project):
    p = project
    pl = [Placement("a", 1.5, 2.0, 0.0, 0.5, 1.0), Placement("a", 5.0, 6.0, 0.5, 1.5, 1.0)]
    assert line_time(pl, p, 1) == 0.0
    assert line_time(pl, p, 3) == 0.5
    assert line_time(pl, p, 2) is None


def test_finished_placements_none_without_intervals(project):
    assert finished_placements(project) is None


def test_finished_placements_from_intervals(project):
    p = project
    d = p.ctx.stage_dir("intervals")
    d.mkdir(parents=True)
    (d / "a_intervals.json").write_text(
        json.dumps({"keep_intervals": [{"start": 1.0, "end": 2.0}]})
    )
    pl = finished_placements(p)
    assert pl is not None and [(x.stem, x.tl_start, x.tl_end) for x in pl] == [("a", 0.0, 1.0)]
