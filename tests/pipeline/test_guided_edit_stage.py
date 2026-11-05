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

    def fake(edits, director, output, cfg, *, json_path=None, gaps_path=None, cuts_txt=None):
        seen.update(edits=edits, json_path=json_path, gaps_path=gaps_path, cuts_txt=cuts_txt)

    monkeypatch.setattr(stages, "run_guided_edit", fake)
    # The director-file check and the order note have tests of their own.
    monkeypatch.setattr(stages, "_check_director_files", lambda ctx: None)
    monkeypatch.setattr(stages, "write_order_note", lambda ctx: None)

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
        # Its recorded media duration is where a source with no speech ends.
        "cuts_txt": tmp_path / "audio_silence" / "clip_cuts.txt",
    }
