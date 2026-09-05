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
