"""The director checkpoint always stops; resuming at guided_edit checks the files."""

from __future__ import annotations

import json

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.director.tools.cli import main
from nagare_clip.director.tools.project import load_project
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineError, PipelineStop
from nagare_clip.pipeline.runner import run_stages

from ..director.test_tools import PLAN, _cfg, _director, _edit_file, project  # noqa: F401


def _ctx(root):
    return load_project(get_effective_config(_cfg(root))).ctx


def _write(root, capsys, edit):
    assert main(["write", "--config", _cfg(root), "--edit", _edit_file(root, edit)]) == 0
    capsys.readouterr()


def _shrink_b(root):
    """text_filter re-ran and b lost a line: an op on b's line 2 maps nowhere."""
    (root / "out" / "text_filter" / "b_edits.txt").write_text("bの1行目\n", encoding="utf-8")
    seg_path = root / "out" / "sentence_split" / "b.json"
    seg = json.loads(seg_path.read_text(encoding="utf-8"))
    seg["segments"] = seg["segments"][:1]
    seg_path.write_text(json.dumps(seg), encoding="utf-8")


def _earlier_outputs(root):
    """What the stages before the director leave on disk (skip-validation)."""
    out = root / "out"
    for stem in ("a", "b"):
        for sub in ("transcription", "audio_silence"):
            (out / sub).mkdir(exist_ok=True)
        (out / "transcription" / f"{stem}.json").write_text(
            (out / "sentence_split" / f"{stem}.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        text = (out / "text_filter" / f"{stem}_edits.txt").read_text(encoding="utf-8")
        (out / "transcription" / f"{stem}.txt").write_text(text, encoding="utf-8")
        (out / "sentence_split" / f"{stem}.txt").write_text(text, encoding="utf-8")
        (out / "audio_silence" / f"{stem}_cuts.txt").write_text("", encoding="utf-8")
        gaps = out / "gap_context" / f"{stem}_gaps.json"
        if not gaps.exists():
            gaps.write_text('{"gaps": []}', encoding="utf-8")


def _guided_edit(ctx):
    _earlier_outputs(ctx.output_dir.parent)
    names = list(st.STAGE_NAMES)
    ctx.from_index = ctx.to_index = names.index("guided_edit")
    run_stages(st.STAGES, ctx)


def test_no_director_files_stops_cleanly_and_names_the_skill(project):  # noqa: F811
    with pytest.raises(PipelineStop) as e:
        st._director_run(_ctx(project))
    msg = str(e.value)
    assert "No director files yet" in msg and "nagare-director" in msg
    assert "--from-stage guided_edit" in msg
    assert not _director(project).exists()  # nothing is written for the person


def test_valid_director_files_still_stop_and_say_to_revise(project, capsys):  # noqa: F811
    """Reaching the director means earlier stages just re-ran: the files on
    disk were made for the previous transcripts, so the run never continues."""
    _write(project, capsys, {"plan": PLAN, "ops": [{"type": "cut", "lines": [1, 1]}]})
    with pytest.raises(PipelineStop) as e:
        st._director_run(_ctx(project))
    msg = str(e.value)
    assert "revise" in msg and "dump, then write" in msg
    assert "--from-stage guided_edit" in msg


def test_resuming_requires_order_json_and_every_director_json(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "ops": []})
    (_director(project) / "order.json").unlink()
    with pytest.raises(PipelineError, match="order.json"):
        _guided_edit(_ctx(project))
    _write(project, capsys, {"plan": PLAN, "ops": []})
    (_director(project) / "b_director.json").unlink()
    with pytest.raises(PipelineError, match="b_director.json"):
        _guided_edit(_ctx(project))


def test_plan_md_is_not_required(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "ops": []})
    (_director(project) / "plan.md").unlink()
    _guided_edit(_ctx(project))


def test_resuming_with_valid_files_says_what_it_continues_with(project, capsys):  # noqa: F811
    edit = {
        "plan": PLAN,
        "ops": [
            {"type": "cut", "lines": [1, 1]},
            {"type": "keep", "lines": [3, 4]},
            {"type": "cut", "lines": [6, 6]},
        ],
    }
    _write(project, capsys, edit)
    _guided_edit(_ctx(project))
    out = capsys.readouterr().out
    assert "Continuing with 3 op(s) (a 2, b 1); shooting order" in out
    assert (project / "out" / "guided_edit" / "a_edits.txt").is_file()


def test_an_empty_edit_is_visible(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "order": [[5, 6], [1, 4]], "ops": []})
    _guided_edit(_ctx(project))
    out = capsys.readouterr().out
    assert "Continuing with 0 op(s) (a 0, b 0); a reordered playback" in out


def test_resuming_with_files_that_no_longer_map_fails(project, capsys):  # noqa: F811
    _write(project, capsys, {"plan": PLAN, "ops": [{"type": "cut", "lines": [6, 6]}]})
    _shrink_b(project)
    with pytest.raises(PipelineError) as e:
        _guided_edit(_ctx(project))
    msg = str(e.value)
    assert "b_director.json: cut [2, 2]" in msg
    assert "dump, then write" in msg
    assert not (project / "out" / "guided_edit" / "a_edits.txt").exists()  # nothing processed


def test_a_run_from_scratch_stops_at_the_director(project):  # noqa: F811
    """Nothing after the director runs, and nothing writes director files."""
    _earlier_outputs(project)
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
    _shrink_b(project)
    assert main(["view", "--config", _cfg(project)]) == 0
    err = capsys.readouterr().err
    assert "1 item(s) in the director files on disk do not map" in err
    assert "b_director.json: cut [2, 2]" in err
