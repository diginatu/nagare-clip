"""Tests for the pipeline CLI: flags, overrides, env, wiring."""

import os

import yaml

from nagare_clip.pipeline import cli


def test_build_cli_overrides_mapping():
    args = cli.parse_args(
        [
            "--language",
            "en",
            "--align-model",
            "m/x",
            "--input-videos-dir",
            "vids",
            "--output-dir",
            "out2",
            "--keep-pre-margin",
            "0.5",
            "--keep-post-margin",
            "0.25",
            "--from-stage",
            "intervals",
            "--to-stage",
            "blender",
        ]
    )
    assert cli.build_cli_overrides(args) == {
        "transcription": {"language": "en", "align_model": "m/x"},
        "pipeline": {
            "input_videos_dir": "vids",
            "output_dir": "out2",
            "from_stage": "intervals",
            "to_stage": "blender",
        },
        "intervals": {"keep_pre_margin": 0.5, "keep_post_margin": 0.25},
    }


def test_build_cli_overrides_empty_when_no_flags():
    assert cli.build_cli_overrides(cli.parse_args([])) == {}


def test_missing_config_file_errors(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "nope.yml")])
    assert rc == 1
    assert "Config file not found" in capsys.readouterr().err


def test_invalid_stage_name_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    rc = cli.main(["--from-stage", "bogus"])
    assert rc == 1
    assert "Invalid --from-stage value: bogus" in capsys.readouterr().err


def test_pipeline_wires_context_and_prints_done(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    seen = {}

    def fake_run_stages(stages, ctx):
        seen["ctx"] = ctx

    monkeypatch.setattr(cli, "run_stages", fake_run_stages)
    rc = cli.main([])
    assert rc == 0
    ctx = seen["ctx"]
    assert ctx.stems == ["a"]
    assert ctx.from_index == 0 and ctx.to_index == len(cli.STAGE_NAMES) - 1
    assert ctx.output_dir == (tmp_path / "output").resolve()
    # stage output dirs created upfront
    assert (tmp_path / "output" / "intervals").is_dir()
    assert (tmp_path / "cache").is_dir()
    assert "Done: " in capsys.readouterr().out


def test_full_run_points_at_the_blend_the_copy_and_the_thumbnails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "a_edited.blend" in out
    assert "publish.md" in out
    assert "render.md" in out


def test_stopping_at_publish_does_not_point_at_thumbnails(tmp_path, monkeypatch, capsys):
    """The contact sheet does not exist until render has run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--to-stage", "publish"]) == 0
    out = capsys.readouterr().out
    assert "publish.md" in out
    assert "render.md" not in out


def test_stopping_at_blender_points_at_the_blend_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--to-stage", "blender"]) == 0
    out = capsys.readouterr().out
    assert "a_edited.blend" in out
    assert "publish.md" not in out


def test_to_stage_prints_stopped(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    rc = cli.main(["--to-stage", "intervals"])
    assert rc == 0
    assert "Done (stopped at --to-stage intervals)" in capsys.readouterr().out


def test_langfuse_false_sets_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NAGARE_LANGFUSE", raising=False)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    cfg = tmp_path / "c.yml"
    cfg.write_text(yaml.safe_dump({"general": {"langfuse": False}}))
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--config", str(cfg)]) == 0
    assert os.environ["NAGARE_LANGFUSE"] == "0"


def test_run_id_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NAGARE_RUN_ID", raising=False)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main([]) == 0
    assert os.environ.get("NAGARE_RUN_ID")


def test_cleanup_of_copied_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    outside = tmp_path / "clip.mp4"
    outside.write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--source", str(outside)]) == 0
    assert not (tmp_path / "src_video" / "clip.mp4").exists()
    assert outside.exists()


# --- the page at the top of the output directory ------------------------------


def _one_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")


def test_every_invocation_writes_the_index(tmp_path, monkeypatch):
    _one_source(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main([]) == 0
    assert (tmp_path / "output" / "index.md").is_file()


def test_a_single_stage_run_writes_the_index_too(tmp_path, monkeypatch):
    """It is a finally, not the last stage: --to-stage cuts the stage list."""
    _one_source(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--from-stage", "render", "--to-stage", "render"]) == 0
    assert (tmp_path / "output" / "index.md").is_file()


def test_a_failed_run_still_writes_the_index_and_still_exits_non_zero(tmp_path, monkeypatch):
    """A run that died in director is when you most want to know what is on disk."""
    _one_source(tmp_path, monkeypatch)

    def boom(stages, ctx):
        raise cli.PipelineError("[director] failed: nope")

    monkeypatch.setattr(cli, "run_stages", boom)
    assert cli.main([]) == 1
    assert (tmp_path / "output" / "index.md").is_file()


def test_an_exploding_index_writer_does_not_change_the_outcome(tmp_path, monkeypatch, capsys):
    """A finally that raises replaces the real error with its own."""
    _one_source(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    monkeypatch.setattr(cli, "write_index", _explode)
    assert cli.main([]) == 0
    err = capsys.readouterr().err
    assert "could not write the output index" in err
    assert "the index writer blew up" in err


def test_an_exploding_index_writer_does_not_mask_a_failure(tmp_path, monkeypatch, capsys):
    _one_source(tmp_path, monkeypatch)

    def boom(stages, ctx):
        raise cli.PipelineError("[director] failed: nope")

    monkeypatch.setattr(cli, "run_stages", boom)
    monkeypatch.setattr(cli, "write_index", _explode)
    assert cli.main([]) == 1
    assert "[director] failed: nope" in capsys.readouterr().err


def _explode(*args, **kwargs):
    raise RuntimeError("the index writer blew up")


def test_the_index_is_written_after_the_llm_report(tmp_path, monkeypatch):
    """It counts the report's rows, so the report has to exist first."""
    _one_source(tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: order.append("stages"))
    real = cli.write_index
    monkeypatch.setattr(
        cli, "write_index", lambda *a, **k: (order.append("index"), real(*a, **k))[1]
    )
    assert cli.main([]) == 0
    assert order == ["stages", "index"]


def test_the_page_needed_no_new_stage_machinery():
    """It has one consumer and a finally regenerates it every invocation, so
    there is nothing to name with --from-stage."""
    from dataclasses import fields

    from nagare_clip.pipeline.runner import Stage

    assert [f.name for f in fields(Stage)] == ["name", "run", "required_outputs"]
    assert [s.name for s in cli.STAGES] == list(cli.STAGE_NAMES)
