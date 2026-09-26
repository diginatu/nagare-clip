"""Silence ops as markers play exactly what the time-resolved path played.

Until this change an op with a ``"n~"`` edge wrote no marker: guided_edit
skipped it and ``intervals/op_times.py`` resolved it to seconds straight from
``_director.json``.  ``data/silence_equivalence.json`` is that path's output
for every case in :mod:`silence_equivalence_cases`, frozen from the pre-change
code (commit 9c8bb12: old ``run_guided_edit`` + ``resolve_op_times`` +
``run_intervals(extra=…)``) before it was deleted.  The new path — guided_edit
writes every op as a marker, on the silence lines it inserts, and
``run_intervals`` reads only ``_edits.txt`` — must reproduce it.

Overlays are compared in timeline order: the old path appended the resolved
overlays after the marker-derived ones, the new one lists them as they occur
in the file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import nagare_clip.intervals.run as stage_run
from nagare_clip.config import get_effective_config
from nagare_clip.edit_lines import parse_edit_lines
from nagare_clip.guided_edit.run import run_guided_edit
from nagare_clip.intervals.run import run_intervals

from .silence_equivalence_cases import CASES, INTERVALS_CFG, TEXT_FILTER_LINES, WHISPERX

FROZEN = json.loads(
    (Path(__file__).parent / "data" / "silence_equivalence.json").read_text(encoding="utf-8")
)


def _new_path(tmp_path: Path, monkeypatch, ops: list[dict]) -> tuple[dict, list[str]]:
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    (tmp_path / "c.json").write_text(json.dumps(WHISPERX), encoding="utf-8")
    (tmp_path / "tf_edits.txt").write_text("\n".join(TEXT_FILTER_LINES) + "\n", encoding="utf-8")
    (tmp_path / "c_director.json").write_text(json.dumps({"ops": ops}), encoding="utf-8")
    (tmp_path / "config.yml").write_text(
        yaml.safe_dump({"intervals": INTERVALS_CFG, "guided_edit": {"enabled": True}}),
        encoding="utf-8",
    )
    cfg = get_effective_config(tmp_path / "config.yml", {})
    run_guided_edit(
        tmp_path / "tf_edits.txt",
        tmp_path / "c_director.json",
        tmp_path / "c_edits.txt",
        cfg,
        json_path=tmp_path / "c.json",
    )
    run_intervals(tmp_path / "c_edits.txt", tmp_path / "c.json", tmp_path / "c_intervals.json", cfg)
    data = json.loads((tmp_path / "c_intervals.json").read_text(encoding="utf-8"))
    edits = (tmp_path / "c_edits.txt").read_text(encoding="utf-8").splitlines()
    return data, edits


def _by_start(overlays: list[dict]) -> list[dict]:
    return sorted(overlays, key=lambda o: (o["start"], o["text"]))


@pytest.mark.xfail(strict=True, reason="the marker path lands with the guided_edit step")
@pytest.mark.parametrize("name", sorted(CASES))
def test_markers_on_silence_lines_play_what_op_times_played(tmp_path, monkeypatch, name):
    data, edits = _new_path(tmp_path, monkeypatch, CASES[name])
    frozen = FROZEN[name]
    assert data["keep_intervals"] == frozen["keep_intervals"]
    assert data.get("speed_ranges", []) == frozen["speed_ranges"]
    assert _by_start(data.get("overlays", [])) == _by_start(frozen["overlays"])
    # ...and it got there through _edits.txt: the silence lines are in the file.
    assert parse_edit_lines(edits).silences()


def test_the_frozen_cases_are_the_cases():
    assert set(FROZEN) == set(CASES)
