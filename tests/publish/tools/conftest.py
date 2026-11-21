"""Shared fixtures for the publish tools tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.director.display import build_display_view
from nagare_clip.director.load import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.director.tools.project import Project
from nagare_clip.order import Segment
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


def make_project(tmp_path: Path) -> Project:
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


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path)
