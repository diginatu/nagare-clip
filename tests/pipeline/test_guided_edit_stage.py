"""The guided_edit adapter hands run_guided_edit what the silence lines need.

The descriptions on a silence line come from gap_context's gaps file, exactly
as the director's view got them; without it the lines guided_edit writes would
read differently from the ones the director placed its ops on.
"""

from __future__ import annotations

from pathlib import Path

import nagare_clip.pipeline.stages as stages


def test_guided_edit_gets_the_sources_timings_and_gaps(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake(edits, director, output, cfg, *, json_path=None, gaps_path=None, recorder=None):
        seen.update(edits=edits, json_path=json_path, gaps_path=gaps_path)

    monkeypatch.setattr(stages, "run_guided_edit", fake)

    class _Rec:
        def clear(self):
            pass

        def rebuild_index(self):
            pass

    monkeypatch.setattr(stages, "_recorder", lambda ctx, name: _Rec())

    class _Src:
        stem = "clip"

    class _Ctx:
        sources = [_Src()]
        cfg: dict = {}

        def stage_dir(self, name):
            return tmp_path / name

    stages._guided_edit_run(_Ctx())
    assert seen == {
        "edits": tmp_path / "text_filter" / "clip_edits.txt",
        "json_path": tmp_path / "sentence_split" / "clip.json",
        "gaps_path": tmp_path / "gap_context" / "clip_gaps.json",
    }
