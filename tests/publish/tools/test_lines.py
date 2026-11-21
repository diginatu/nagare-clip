"""Display line -> source span -> finished-timeline time."""

from __future__ import annotations

import json
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.director.display import build_display_view
from nagare_clip.director.load import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.director.tools.project import Project
from nagare_clip.order import Segment
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia
from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools.lines import LineSpan, finished_placements, line_span, line_time


def _project(tmp_path: Path) -> Project:
    transcript = SegmentTranscript(
        edit_lines=["one", "two"],
        first_line=1,
        seg_times=[(1.0, 2.0), (5.0, 6.0)],
        silences=None,
        gaps=[],
        silence_lines=[SilenceLine(after_line=1, start=2.0, end=5.0)],
    )
    view = build_display_view([(Segment("a", None), transcript)])
    ctx = PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=[SourceMedia(tmp_path / "a.mp4", "a", "a.mp4")],
        from_index=0,
        to_index=0,
    )
    return Project(ctx=ctx, view=view, transcripts=[transcript], inputs=[])


def test_speech_line_span(tmp_path):
    assert line_span(_project(tmp_path), 1) == LineSpan("a", 1.0, 2.0)
    assert line_span(_project(tmp_path), 3) == LineSpan("a", 5.0, 6.0)


def test_silence_line_span(tmp_path):
    assert line_span(_project(tmp_path), 2) == LineSpan("a", 2.0, 5.0)


def test_out_of_range_is_none(tmp_path):
    assert line_span(_project(tmp_path), 99) is None
    assert line_span(_project(tmp_path), 0) is None


def test_line_time_uses_first_surviving_moment(tmp_path):
    p = _project(tmp_path)
    pl = [Placement("a", 1.5, 2.0, 0.0, 0.5, 1.0), Placement("a", 5.0, 6.0, 0.5, 1.5, 1.0)]
    assert line_time(pl, p, 1) == 0.0
    assert line_time(pl, p, 3) == 0.5
    assert line_time(pl, p, 2) is None


def test_finished_placements_none_without_intervals(tmp_path):
    assert finished_placements(_project(tmp_path)) is None


def test_finished_placements_from_intervals(tmp_path):
    p = _project(tmp_path)
    d = p.ctx.stage_dir("intervals")
    d.mkdir(parents=True)
    (d / "a_intervals.json").write_text(
        json.dumps({"keep_intervals": [{"start": 1.0, "end": 2.0}]})
    )
    pl = finished_placements(p)
    assert pl is not None and [(x.stem, x.tl_start, x.tl_end) for x in pl] == [("a", 0.0, 1.0)]
