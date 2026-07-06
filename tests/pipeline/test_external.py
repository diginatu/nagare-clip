"""Tests for external (docker/blender) command construction."""

from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline.external import (
    build_blender_cmd,
    build_silencedetect_cmd,
    build_transcription_cmd,
    effective_align_model,
    run_command,
)


def _cfg(overrides=None):
    return get_effective_config(None, overrides or {})


def test_effective_align_model_ja_default():
    assert effective_align_model(_cfg()) == "vumichien/wav2vec2-large-xlsr-japanese"


def test_effective_align_model_explicit_wins():
    cfg = _cfg({"transcription": {"align_model": "my/model"}})
    assert effective_align_model(cfg) == "my/model"


def test_effective_align_model_non_ja_empty():
    assert effective_align_model(_cfg({"transcription": {"language": "en"}})) == ""


def test_build_transcription_cmd():
    root = Path("/proj")
    cmd = build_transcription_cmd(root, ["a.mp4", "b.mp4"], _cfg())
    assert cmd == [
        "docker",
        "compose",
        "-f",
        "/proj/docker-compose.yml",
        "run",
        "--rm",
        "--user",
        "0:0",
        "whisperx",
        "_",
        "a.mp4",
        "b.mp4",
        "--output_dir",
        "/output/transcription",
        "--output_format",
        "all",
        "--language",
        "ja",
        "--compute_type",
        "float16",
        "--batch_size",
        "16",
        "--align_model",
        "vumichien/wav2vec2-large-xlsr-japanese",
    ]


def test_build_transcription_cmd_omits_empty_align_model():
    cmd = build_transcription_cmd(
        Path("/proj"), ["a.mp4"], _cfg({"transcription": {"language": "en"}})
    )
    assert "--align_model" not in cmd


def test_build_silencedetect_cmd():
    cmd = build_silencedetect_cmd(Path("/proj"), "a.mp4", -30.0, 0.8)
    assert cmd == [
        "docker",
        "compose",
        "-f",
        "/proj/docker-compose.yml",
        "run",
        "--rm",
        "--user",
        "0:0",
        "--entrypoint",
        "ffmpeg",
        "whisperx",
        "-hide_banner",
        "-nostats",
        "-i",
        "a.mp4",
        "-af",
        "silencedetect=noise=-30.0dB:d=0.8",
        "-f",
        "null",
        "-",
    ]


def test_build_blender_cmd(tmp_path):
    cmd = build_blender_cmd(
        Path("/proj"),
        [Path("/vids/a.mp4")],
        [Path("/out/intervals/a_intervals.json")],
        Path("/out/blender/a_edited.blend"),
        Path("/cfg.yml"),
        Path("/out/pipeline.log"),
    )
    assert cmd == [
        "blender",
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        "/proj/src/nagare_clip/blender/blender_cli.py",
        "--",
        "--source",
        "/vids/a.mp4",
        "--intervals",
        "/out/intervals/a_intervals.json",
        "--output",
        "/out/blender/a_edited.blend",
        "--config",
        "/cfg.yml",
        "--log-file",
        "/out/pipeline.log",
    ]


def test_build_blender_cmd_without_config(tmp_path):
    cmd = build_blender_cmd(
        Path("/proj"),
        [Path("/v/a.mp4")],
        [Path("/i/a.json")],
        Path("/b/a.blend"),
        None,
        Path("/l.log"),
    )
    assert "--config" not in cmd


def test_run_command_stderr_capture(tmp_path):
    log = tmp_path / "err.log"
    run_command(
        ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        stderr_to=log,
    )
    assert log.read_text() == "err\n"


def test_run_command_env_extra(tmp_path):
    marker = tmp_path / "env.txt"
    run_command(
        [
            "python3",
            "-c",
            "import os, sys; open(sys.argv[1], 'w').write(os.environ['PIPE_TEST_VAR'])",
            str(marker),
        ],
        env_extra={"PIPE_TEST_VAR": "hello"},
    )
    assert marker.read_text() == "hello"
