"""Every publish tool command runs with the LLM transport booby-trapped.

The static guard (``tests/render/test_run.py``) proves nothing under
``publish/`` or ``render/`` *names* ``call_llm``; this one proves no command
*reaches* a model at run time, through any import path: ``call_llm`` is
replaced everywhere it is bound (the transport module and every module that
imported the name) and ``litellm.completion`` raises too.  Docker and
ImageMagick are faked at the runner, so the real extraction, build and render
code all run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nagare_clip import llm_client
from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools import cli
from nagare_clip.publish.tools import frames as frames_mod
from tests.publish.tools.conftest import make_project

PLACEMENTS = [
    Placement("a", 1.0, 2.0, 0.0, 1.0, 1.0),
    Placement("a", 5.0, 6.0, 1.0, 2.0, 1.0),
]


class LLMCalled(AssertionError):
    pass


def _boom(*args, **kwargs):
    raise LLMCalled("a publish tool called an LLM")


@pytest.fixture
def no_llm(monkeypatch):
    original = llm_client.call_llm
    for module in list(sys.modules.values()):
        if getattr(module, "call_llm", None) is original:
            monkeypatch.setattr(module, "call_llm", _boom)
    try:
        import litellm
    except ImportError:  # pragma: no cover - litellm is a dependency
        pass
    else:
        monkeypatch.setattr(litellm, "completion", _boom)


@pytest.fixture
def project(tmp_path, monkeypatch, no_llm):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.mp4").write_bytes(b"")
    project = make_project(tmp_path)
    monkeypatch.setattr(cli, "load_project", lambda cfg: project)
    monkeypatch.setattr(cli, "finished_placements", lambda p: PLACEMENTS)
    monkeypatch.setattr(cli, "read_ops", lambda ctx, stems: {})

    def extract(proj, stills, **kw):
        out = proj.ctx.stage_dir("publish")

        def docker(argv, **kwargs):  # the one container: write every still
            for s in stills:
                (out / s.path).write_bytes(b"jpeg")

        return frames_mod.extract_stills(proj, stills, run=docker)

    monkeypatch.setattr(cli, "extract_stills", extract)

    def magick(cmd):
        if cmd[:2] == ["magick", "montage"]:
            Path(cmd[-1]).write_bytes(b"sheet")
            return ""
        if cmd[-1] == "info:":
            return "".join("300 90\n" for a in cmd if a.startswith("label:"))
        return ""

    monkeypatch.setattr(cli, "run_magick", magick)
    return project


def test_every_command_runs_without_an_llm(project, tmp_path, capsys):
    assert cli.main(["view"]) == 0
    assert cli.main(["view", "--compact"]) == 0
    assert cli.main(["frames", "--line", "1"]) == 0
    assert cli.main(["frames", "--at", "0:01", "--sheet"]) == 0
    background = json.loads((project.ctx.stage_dir("publish") / "stills.json").read_text())[0][
        "path"
    ]
    assert cli.main(["chapters", "1", "3"]) in (0, 1)  # too short for YouTube: exit 1 is fine
    edit = tmp_path / "edit.json"
    edit.write_text(
        json.dumps(
            {
                "titles": ["Title"],
                "lead": "Lead",
                "chapters": [],
                "thumbnails": [{"background": background, "lines": [{"text": "Hi"}]}],
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["write", "--edit", str(edit)]) == 0
    assert cli.main(["render"]) == 0
    rendered = json.loads((project.ctx.stage_dir("render") / "render.json").read_text())
    assert [r["set"] for r in rendered["renders"]] == [1]
