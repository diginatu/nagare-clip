"""The director checkpoint: the pipeline stops until the skill wrote director/."""

from __future__ import annotations

import json

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.director.tools.cli import main
from nagare_clip.director.tools.project import load_project
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineStop
from nagare_clip.pipeline.runner import run_stages

from ..director.test_tools import PLAN, _cfg, _director, _edit_file, project  # noqa: F401


def _ctx(root):
    return load_project(get_effective_config(_cfg(root))).ctx


def _write(root, capsys, edit):
    assert main(["write", "--config", _cfg(root), "--edit", _edit_file(root, edit)]) == 0
    capsys.readouterr()


def test_no_director_files_stops_cleanly_and_names_the_skill(project):  # noqa: F811
    with pytest.raises(PipelineStop) as e:
        st._director_run(_ctx(project))
    msg = str(e.value)
    assert "nagare-director" in msg
    assert "--from-stage guided_edit" in msg
    for name in ("order.json", "a_director.json", "b_director.json"):
        assert name in msg
    assert not _director(project).exists()  # nothing is written for the person


def test_partial_director_files_name_only_what_is_missing(project):  # noqa: F811
    d = _director(project)
    d.mkdir(parents=True)
    (d / "order.json").write_text('{"order": [{"stem": "a"}, {"stem": "b"}]}', encoding="utf-8")
    (d / "a_director.json").write_text('{"ops": []}', encoding="utf-8")
    with pytest.raises(PipelineStop) as e:
        st._director_run(_ctx(project))
    msg = str(e.value)
    assert "b_director.json" in msg
    assert "a_director.json" not in msg and "order.json" not in msg


def test_plan_md_is_not_required(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "ops": []})
    (_director(project) / "plan.md").unlink()
    st._director_run(_ctx(project))


def test_existing_files_continue_and_say_what_with(project, capsys):  # noqa: F811
    edit = {
        "plan": PLAN,
        "ops": [
            {"type": "cut", "lines": [1, 1]},
            {"type": "keep", "lines": [3, 4]},
            {"type": "cut", "lines": [6, 6]},
        ],
    }
    _write(project, capsys, edit)
    st._director_run(_ctx(project))
    out = capsys.readouterr().out
    assert "Continuing with 3 op(s) (a 2, b 1); shooting order" in out


def test_an_empty_edit_is_visible(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "order": [[5, 6], [1, 4]], "ops": []})
    st._director_run(_ctx(project))
    out = capsys.readouterr().out
    assert "Continuing with 0 op(s) (a 0, b 0); a reordered playback" in out


def test_files_that_no_longer_map_stop_the_run(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "ops": [{"type": "cut", "lines": [6, 6]}]})
    # text_filter re-ran and b lost a line: the cut on b's line 2 maps nowhere
    edits = project / "out" / "text_filter" / "b_edits.txt"
    edits.write_text("bの1行目\n", encoding="utf-8")
    seg = json.loads((project / "out" / "sentence_split" / "b.json").read_text(encoding="utf-8"))
    seg["segments"] = seg["segments"][:1]
    (project / "out" / "sentence_split" / "b.json").write_text(json.dumps(seg), encoding="utf-8")
    with pytest.raises(PipelineStop) as e:
        st._director_run(_ctx(project))
    msg = str(e.value)
    assert "b_director.json: cut [2, 2]" in msg
    assert "dump, then write" in msg


def test_a_run_from_scratch_stops_at_the_director(project):  # noqa: F811
    """With no director files, the pipeline stops at the director: nothing
    after it runs, and nothing writes empty director files to pass it."""
    out = project / "out"
    for stem in ("a", "b"):
        for sub in ("transcription", "audio_silence"):
            (out / sub).mkdir(exist_ok=True)
        (out / "transcription" / f"{stem}.json").write_text(
            (out / "sentence_split" / f"{stem}.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (out / "transcription" / f"{stem}.txt").write_text(
            (out / "text_filter" / f"{stem}_edits.txt").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (out / "audio_silence" / f"{stem}_cuts.txt").write_text("", encoding="utf-8")
    ctx = _ctx(project)
    names = list(st.STAGE_NAMES)
    ctx.from_index = names.index("sentence_split")
    ctx.to_index = names.index("render")
    with pytest.raises(PipelineStop):
        run_stages(st.STAGES, ctx)
    assert not (project / "out" / "guided_edit").exists()
    assert not _director(project).exists()


def test_view_warns_when_the_files_on_disk_no_longer_map(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "ops": [{"type": "cut", "lines": [6, 6]}]})
    assert main(["view", "--config", _cfg(project)]) == 0
    assert "do not map" not in capsys.readouterr().err
    (project / "out" / "text_filter" / "b_edits.txt").write_text("bの1行目\n", encoding="utf-8")
    seg_path = project / "out" / "sentence_split" / "b.json"
    seg = json.loads(seg_path.read_text(encoding="utf-8"))
    seg["segments"] = seg["segments"][:1]
    seg_path.write_text(json.dumps(seg), encoding="utf-8")
    assert main(["view", "--config", _cfg(project)]) == 0
    err = capsys.readouterr().err
    assert "1 item(s) in the director files on disk do not map" in err
    assert "b_director.json: cut [2, 2]" in err
