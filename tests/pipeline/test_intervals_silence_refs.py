"""The intervals stage resolves a source's `"n~"` ops and hands them down.

Without this wiring the resolver exists but nothing calls it, so a silence
reference in a hand-edited _director.json would be silently ignored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nagare_clip.pipeline.stages as stages


@pytest.fixture
def project(tmp_path: Path):
    """A one-source project with a 3.9 s wait between its two lines."""
    for name in ("guided_edit", "sentence_split", "intervals", "director", "audio_silence"):
        (tmp_path / name).mkdir()
    (tmp_path / "guided_edit" / "clip_edits.txt").write_text("あい\nうえ\n", encoding="utf-8")
    (tmp_path / "sentence_split" / "clip.json").write_text(
        json.dumps(
            {
                "duration": 10.0,
                "segments": [
                    {
                        "words": [
                            {"word": "あ", "start": 0.5, "end": 0.8},
                            {"word": "い", "start": 0.8, "end": 1.1},
                        ]
                    },
                    {
                        "words": [
                            {"word": "う", "start": 5.0, "end": 5.3},
                            {"word": "え", "start": 5.3, "end": 5.6},
                        ]
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run(project: Path, monkeypatch, ops):
    (project / "director" / "clip_director.json").write_text(
        json.dumps({"ops": ops}), encoding="utf-8"
    )
    seen: dict = {}

    def fake_run_intervals(edits, json_path, output, cfg, *, cuts_txt=None, extra=None):
        seen["extra"] = extra
        output.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(stages, "run_intervals", fake_run_intervals)
    monkeypatch.setattr(stages, "write_order_note", lambda ctx: None)
    monkeypatch.setattr(stages, "_write_manifest", lambda ctx: None)
    monkeypatch.setattr(stages, "write_cut_report", lambda ctx: None)

    class _Src:
        stem = "clip"

    class _Ctx:
        sources = [_Src()]
        cfg: dict = {}

        def stage_dir(self, name):
            return project / name

    stages._intervals_run(_Ctx())
    return seen["extra"]


def test_a_silence_op_reaches_run_intervals_as_a_time_range(project, monkeypatch):
    extra = _run(
        project,
        monkeypatch,
        [{"type": "timelapse", "lines": ["1~", "1~"], "factor": 5.0, "text": "待ち"}],
    )
    assert extra.keeps == [(1.1, 5.0)]
    assert extra.speeds == [(1.1, 5.0, 5.0)]
    assert extra.overlays and extra.overlays[0][2] == "待ち"


def test_a_project_without_silence_ops_passes_nothing_extra(project, monkeypatch):
    extra = _run(project, monkeypatch, [{"type": "cut", "lines": [1, 1]}])
    assert (extra.keeps, extra.speeds, extra.overlays) == ([], [], [])


def test_a_missing_director_file_is_not_an_error(project, monkeypatch):
    seen: dict = {}

    def fake_run_intervals(edits, json_path, output, cfg, *, cuts_txt=None, extra=None):
        seen["extra"] = extra
        output.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(stages, "run_intervals", fake_run_intervals)
    monkeypatch.setattr(stages, "write_order_note", lambda ctx: None)
    monkeypatch.setattr(stages, "_write_manifest", lambda ctx: None)
    monkeypatch.setattr(stages, "write_cut_report", lambda ctx: None)

    class _Src:
        stem = "clip"

    class _Ctx:
        sources = [_Src()]
        cfg: dict = {}

        def stage_dir(self, name):
            return project / name

    stages._intervals_run(_Ctx())
    assert seen["extra"] is None
