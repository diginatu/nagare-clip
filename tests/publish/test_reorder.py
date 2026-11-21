"""publish under a reordered timeline, through the tools.

A chapter's time comes from ``line_time()`` over ``build_placements()``, which
lays segments end to end in PLAYBACK order.  Computing times in shooting order
and then sorting would be sorting wrong numbers: the cause is the placements,
not the ordering of the entries.
"""

from __future__ import annotations

from nagare_clip.config import get_effective_config
from nagare_clip.director.display import build_display_view
from nagare_clip.director.load import SegmentTranscript
from nagare_clip.director.tools.project import Project
from nagare_clip.order import Segment
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia
from nagare_clip.publish.timeline import build_placements
from nagare_clip.publish.tools.build import build_publish
from nagare_clip.publish.tools.edit import ChapterRef, PublishEdit, resolve_chapters
from nagare_clip.publish.tools.lines import line_time

# One source, three lines of 100s each (display lines 1..3), nothing cut.
TITLES = {1: "おさらい", 2: "実演", 3: "魚"}


def _project(tmp_path) -> Project:
    transcript = SegmentTranscript(
        edit_lines=["a", "b", "c"],
        first_line=1,
        seg_times=[(0.0, 100.0), (100.0, 200.0), (200.0, 300.0)],
        silences=None,
        gaps=[],
        silence_lines=[],
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


def _seg(start, end):
    return {"keep_intervals": [{"start": start, "end": end}]}


def _run(tmp_path, ordered):
    project = _project(tmp_path)
    placements = build_placements(ordered)
    refs = [ChapterRef(n, title) for n, title in TITLES.items()]
    chapters, problems = resolve_chapters(refs, project, placements, min_chapter=10.0)
    assert problems == []
    edit = PublishEdit(["題"], "リード", refs, [])
    return build_publish(edit, chapters, placements, []), project, placements


def test_shooting_order_gives_the_parts_in_transcript_order(tmp_path):
    data, _, _ = _run(tmp_path, [("a", _seg(0.0, 300.0))])
    assert [c["title"] for c in data["chapters"]] == ["おさらい", "実演", "魚"]
    assert [c["timestamp"] for c in data["chapters"]] == ["0:00", "1:40", "3:20"]


def test_a_reordered_timeline_reorders_the_chapters(tmp_path):
    # The middle part plays first: its chapter must come first, and every part
    # must still get one.
    ordered = [
        ("a", _seg(100.0, 200.0)),
        ("a", _seg(0.0, 100.0)),
        ("a", _seg(200.0, 300.0)),
    ]
    data, project, placements = _run(tmp_path, ordered)
    assert [c["title"] for c in data["chapters"]] == ["実演", "おさらい", "魚"]
    assert [c["timestamp"] for c in data["chapters"]] == ["0:00", "1:40", "3:20"]
    assert line_time(placements, project, 2) == 0.0
    assert line_time(placements, project, 1) == 100.0


def test_no_chapter_is_lost_to_the_reorder(tmp_path):
    ordered = [("a", _seg(200.0, 300.0)), ("a", _seg(0.0, 200.0))]
    data, _, _ = _run(tmp_path, ordered)
    assert len(data["chapters"]) == 3
    times = [c["time"] for c in data["chapters"]]
    assert times == sorted(times)
