"""The render stage's place in the pipeline: last, after publish, reading
publish.json and shelling out to the host's ImageMagick."""

from __future__ import annotations

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


@pytest.fixture
def ctx(tmp_path):
    sources = [SourceMedia(abs_path=tmp_path / "a.mp4", stem="a", relative="a.mp4")]
    return PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=sources,
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )


def test_render_is_the_last_stage():
    assert st.STAGE_NAMES[-1] == "render"
    assert st.STAGE_NAMES.index("render") == st.STAGE_NAMES.index("publish") + 1
    assert [s.name for s in st.STAGES] == st.STAGE_NAMES


def test_render_required_output(ctx):
    stage = next(s for s in st.STAGES if s.name == "render")
    assert stage.required_outputs(ctx) == [ctx.stage_dir("render") / "render.json"]


def test_the_adapter_points_at_publish_json_and_the_render_dir(ctx, monkeypatch):
    seen = {}

    def fake_run_render(publish_json, output, cfg, **kwargs):
        seen.update(kwargs, publish_json=publish_json, output=output, cfg=cfg)

    monkeypatch.setattr(st, "run_render", fake_run_render)
    next(s for s in st.STAGES if s.name == "render").run(ctx)
    out = ctx.output_dir
    assert seen["publish_json"] == out / "publish" / "publish.json"
    assert seen["output"] == out / "render" / "render.json"
    assert seen["markdown"] == out / "render" / "render.md"
    assert seen["run"] is st.run_magick
    assert seen["cfg"] is ctx.cfg


def test_the_render_stage_opens_no_llm_report(ctx, monkeypatch):
    """It makes no call, so it has nothing to report and must not wipe one."""
    monkeypatch.setattr(st, "run_render", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("render must not touch the LLM report")

    monkeypatch.setattr(st, "recorder_from_config", boom)
    next(s for s in st.STAGES if s.name == "render").run(ctx)
