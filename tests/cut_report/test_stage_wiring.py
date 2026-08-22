"""The note the pipeline writes, and when it writes it.

It lives in llm_report/notes/ like the plan/director divergence note, so it
survives a later stage's rebuild of index.md and lands in the one file the
human already reads.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.blender.warnings_file import WARNINGS_FILENAME
from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import rebuild_index
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia
from nagare_clip.pipeline.stages import CUT_REPORT_NOTE, write_cut_report


@pytest.fixture
def ctx(tmp_path):
    out = tmp_path / "output"
    return PipelineContext(
        cfg=get_effective_config(None),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "src_video",
        output_dir=out,
        sources=[
            SourceMedia(abs_path=tmp_path / f"{s}.mp4", relative=f"{s}.mp4", stem=s)
            for s in ("one", "two")
        ],
        from_index=0,
        to_index=0,
    )


def _write_intervals(ctx, sources):
    d = ctx.stage_dir("intervals")
    d.mkdir(parents=True, exist_ok=True)
    for stem, data in sources:
        (d / f"{stem}_intervals.json").write_text(json.dumps(data), encoding="utf-8")


class TestNote:
    def test_written_into_the_llm_report_notes_dir(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        write_cut_report(ctx)
        note = ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE
        assert note.is_file()
        assert "## finished cut" in note.read_text(encoding="utf-8")

    def test_rebuild_index_inlines_it(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        write_cut_report(ctx)
        rebuild_index(ctx.llm_report_dir)
        index = (ctx.llm_report_dir / "index.md").read_text(encoding="utf-8")
        assert "## finished cut" in index

    def test_sources_are_read_in_the_pipelines_concatenation_order(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        write_cut_report(ctx)
        text = (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).read_text(encoding="utf-8")
        # 100s + 100s of source; only both files read gives 3.3 min.
        assert "3.3 min" in text

    def test_a_missing_intervals_file_is_skipped_not_fatal(self, ctx, two_sources):
        _write_intervals(ctx, two_sources[:1])
        write_cut_report(ctx)
        assert (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).is_file()

    def test_no_intervals_at_all_removes_a_stale_note(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        write_cut_report(ctx)
        for p in ctx.stage_dir("intervals").glob("*.json"):
            p.unlink()
        write_cut_report(ctx)
        assert not (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).exists()

    def test_disabled_writes_nothing_and_clears_a_stale_note(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        write_cut_report(ctx)
        ctx.cfg["cut_report"]["enabled"] = False
        write_cut_report(ctx)
        assert not (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).exists()

    def test_unparseable_intervals_json_does_not_fail_the_stage(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        (ctx.stage_dir("intervals") / "one_intervals.json").write_text("{", encoding="utf-8")
        write_cut_report(ctx)  # must not raise
        assert (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).is_file()


class TestBlenderWarnings:
    def test_picked_up_from_the_blender_stage_dir(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        d = ctx.stage_dir("blender")
        d.mkdir(parents=True, exist_ok=True)
        (d / WARNINGS_FILENAME).write_text(
            json.dumps({"warnings": ["Strip 109: interval clamped"]}), encoding="utf-8"
        )
        write_cut_report(ctx)
        text = (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).read_text(encoding="utf-8")
        assert "Strip 109: interval clamped" in text

    def test_absent_warnings_file_is_not_an_error(self, ctx, two_sources):
        _write_intervals(ctx, two_sources)
        write_cut_report(ctx)
        text = (ctx.llm_report_dir / "notes" / CUT_REPORT_NOTE).read_text(encoding="utf-8")
        assert "blender-warning" not in text
