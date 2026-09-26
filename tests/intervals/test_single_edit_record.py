"""``_edits.txt`` is the single human-editable record of every edit.

AGENTS.md, Hard Constraints: *"_edits.txt is the single human-editable record
of every edit. intervals applies only what is in it (plus the audio_silence
_cuts.txt, a detection result the human prunes). No stage may feed intervals
an edit from another source; an edit the file cannot express is a format
change to _edits.txt, not a side channel."*

Nothing written said so until 2026-09-26, so a side channel could be added
without breaking a rule: director ops on a silence were resolved to times
straight from ``_director.json`` and handed to ``run_intervals`` (5fbc4a8,
0e8dafa), invisible in the file a human edits.  These tests are that rule in a
form a change has to argue with: adding an input to ``intervals`` means
editing this file.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import yaml

import nagare_clip
import nagare_clip.intervals.run as stage_run
import nagare_clip.pipeline.stages as stages
from nagare_clip.config import get_effective_config
from nagare_clip.intervals.run import run_intervals

from .silence_equivalence_cases import CASES, INTERVALS_CFG, TEXT_FILTER_LINES, WHISPERX

SRC = Path(nagare_clip.__file__).parent
FORBIDDEN = ("nagare_clip.director", "nagare_clip.guided_edit")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{alias.name}" for alias in node.names)
    return out


def test_intervals_and_the_line_contract_import_nothing_from_the_director_side():
    files = [*sorted((SRC / "intervals").glob("*.py")), SRC / "edit_lines.py"]
    offenders = {
        str(path.relative_to(SRC)): sorted(
            name for name in _imports(path) if name.startswith(FORBIDDEN)
        )
        for path in files
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_run_intervals_takes_the_edits_file_and_the_cut_list_only():
    # A new parameter here is a new way to feed intervals an edit: extend
    # _edits.txt instead (see the module docstring).
    assert list(inspect.signature(run_intervals).parameters) == [
        "edits_txt",
        "json_path",
        "output",
        "cfg",
        "cuts_txt",
    ]


def _project(tmp_path: Path, with_director: bool) -> Path:
    for name in ("guided_edit", "sentence_split", "intervals", "director", "audio_silence"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "sentence_split" / "clip.json").write_text(json.dumps(WHISPERX), encoding="utf-8")
    (tmp_path / "guided_edit" / "clip_edits.txt").write_text(
        "\n".join(TEXT_FILTER_LINES) + "\n", encoding="utf-8"
    )
    if with_director:
        ops = [op for case in CASES.values() for op in case]
        (tmp_path / "director" / "clip_director.json").write_text(
            json.dumps({"ops": ops}), encoding="utf-8"
        )
    return tmp_path


def _intervals_output(tmp_path: Path, monkeypatch, with_director: bool) -> str:
    project = _project(tmp_path, with_director)
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    monkeypatch.setattr(stages, "write_order_note", lambda ctx: None)
    monkeypatch.setattr(stages, "_write_manifest", lambda ctx: None)
    monkeypatch.setattr(stages, "write_cut_report", lambda ctx: None)
    (project / "config.yml").write_text(
        yaml.safe_dump({"intervals": INTERVALS_CFG}), encoding="utf-8"
    )
    cfg = get_effective_config(project / "config.yml", {})

    class _Src:
        stem = "clip"

    class _Ctx:
        sources = [_Src()]

        def __init__(self):
            self.cfg = cfg

        def stage_dir(self, name):
            return project / name

    stages._intervals_run(_Ctx())
    return (project / "intervals" / "clip_intervals.json").read_text(encoding="utf-8")


def test_the_directors_output_never_reaches_intervals(tmp_path, monkeypatch):
    without = _intervals_output(tmp_path / "a", monkeypatch, with_director=False)
    with_ops = _intervals_output(tmp_path / "b", monkeypatch, with_director=True)
    assert with_ops == without
