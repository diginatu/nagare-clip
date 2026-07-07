"""Tests for the generic stage runner: windowing, skipping, validation."""

import logging

import pytest

from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import (
    PipelineContext,
    Stage,
    resolve_window,
    run_stages,
)


def _stages(names, calls, required=None):
    required = required or {}
    return [
        Stage(
            name=n,
            run=lambda ctx, n=n: calls.append(n),
            required_outputs=required.get(n, lambda ctx: []),
        )
        for n in names
    ]


def _ctx(tmp_path, from_index, to_index):
    return PipelineContext(
        cfg={},
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=[],
        from_index=from_index,
        to_index=to_index,
    )


def test_resolve_window_full_range():
    stages = _stages(["a", "b", "c"], [])
    assert resolve_window(stages, "a", "c") == (0, 2)


def test_resolve_window_unknown_from():
    stages = _stages(["a", "b"], [])
    with pytest.raises(PipelineError, match="Invalid --from-stage value: x"):
        resolve_window(stages, "x", "b")


def test_resolve_window_unknown_to():
    stages = _stages(["a", "b"], [])
    with pytest.raises(PipelineError, match="Invalid --to-stage value: y"):
        resolve_window(stages, "a", "y")


def test_resolve_window_inverted():
    stages = _stages(["a", "b"], [])
    with pytest.raises(PipelineError, match="after --to-stage"):
        resolve_window(stages, "b", "a")


def test_run_stages_runs_only_window(tmp_path, capsys):
    calls = []
    stages = _stages(["a", "b", "c", "d"], calls)
    run_stages(stages, _ctx(tmp_path, 1, 2))
    assert calls == ["b", "c"]
    out = capsys.readouterr().out
    assert "[a] Skipped (--from-stage b)" in out
    assert "[d] Skipped (--to-stage c)" in out


def test_run_stages_validates_skipped_outputs(tmp_path):
    missing = tmp_path / "out" / "a" / "x.json"
    calls = []
    stages = _stages(["a", "b"], calls, required={"a": lambda ctx: [missing]})
    with pytest.raises(PipelineError, match="Missing a output"):
        run_stages(stages, _ctx(tmp_path, 1, 1))
    assert calls == []


def test_run_stages_skipped_outputs_present_ok(tmp_path):
    present = tmp_path / "x.json"
    present.write_text("{}")
    calls = []
    stages = _stages(["a", "b"], calls, required={"a": lambda ctx: [present]})
    run_stages(stages, _ctx(tmp_path, 1, 1))
    assert calls == ["b"]


def test_run_stages_wraps_stage_exception(tmp_path):
    def boom(ctx):
        raise ValueError("kaput")

    stages = [Stage(name="a", run=boom)]
    with pytest.raises(PipelineError, match=r"\[a\] failed: kaput"):
        run_stages(stages, _ctx(tmp_path, 0, 0))


def test_run_stages_logs_traceback_on_stage_failure(tmp_path, caplog):
    def boom(ctx):
        raise ValueError("kaput")

    stages = [Stage(name="a", run=boom)]
    with caplog.at_level(logging.ERROR):
        with pytest.raises(PipelineError, match=r"\[a\] failed: kaput"):
            run_stages(stages, _ctx(tmp_path, 0, 0))

    assert "ValueError: kaput" in caplog.text
    assert caplog.records[0].exc_info is not None


def test_context_paths(tmp_path):
    ctx = _ctx(tmp_path, 0, 0)
    assert ctx.log_file == tmp_path / "out" / "pipeline.log"
    assert ctx.llm_report_dir == tmp_path / "out" / "llm_report"
    assert ctx.stage_dir("intervals") == tmp_path / "out" / "intervals"
