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


def test_build_snapshot_batch_cmd_one_container_for_whole_batch():
    """The whole stage's frame extraction is ONE docker command, regardless
    of how many jobs it carries (container-startup overhead dominates over
    per-frame ffmpeg work -- see docs/stages/gap_context.md)."""
    from nagare_clip.pipeline.external import build_snapshot_batch_cmd

    jobs = [
        ("talk1.mp4", 10.2, "/output/gap_context/frames/talk1/10.200.jpg"),
        ("talk1.mp4", 15.0, "/output/gap_context/frames/talk1/15.000.jpg"),
        ("talk2.mp4", 3.5, "/output/gap_context/frames/talk2/3.500.jpg"),
    ]
    cmd = build_snapshot_batch_cmd(Path("/proj"), jobs, 960)
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert "--entrypoint" in cmd and cmd[cmd.index("--entrypoint") + 1] == "sh"
    assert cmd[-2] == "-c"
    script = cmd[-1]
    # One ffmpeg invocation per job, all in the single script.
    assert script.count("ffmpeg ") == len(jobs)


def test_build_snapshot_batch_cmd_preserves_ffmpeg_flags_per_job():
    from nagare_clip.pipeline.external import build_snapshot_batch_cmd

    jobs = [("talk1.mp4", 12.6, "/output/gap_context/frames/talk1/12.600.jpg")]
    cmd = build_snapshot_batch_cmd(Path("/proj"), jobs, 960)
    script = cmd[-1]
    line = script.strip()
    assert "-hide_banner" in line
    assert "-nostats" in line
    assert "-loglevel error" in line
    assert "-nostdin" in line
    # input-side -ss (fast seek), before -i
    assert line.index("-ss") < line.index("-i")
    assert "-ss 12.600" in line
    assert "-i talk1.mp4" in line
    assert "-frames:v 1" in line
    assert "-vf scale=960:-2" in line
    assert "-q:v 4" in line
    assert "/output/gap_context/frames/talk1/12.600.jpg" in line


def test_build_snapshot_batch_cmd_each_job_tolerates_failure():
    """`|| true` per line: one bad seek must not kill the rest of the batch."""
    from nagare_clip.pipeline.external import build_snapshot_batch_cmd

    jobs = [
        ("a.mp4", 1.0, "/output/gap_context/frames/a/1.000.jpg"),
        ("b.mp4", 2.0, "/output/gap_context/frames/b/2.000.jpg"),
    ]
    cmd = build_snapshot_batch_cmd(Path("/proj"), jobs, 960)
    script = cmd[-1]
    lines = [line for line in script.splitlines() if line.strip()]
    assert len(lines) == len(jobs)
    for line in lines:
        assert line.rstrip().endswith("|| true")


def test_build_snapshot_batch_cmd_quotes_paths_with_spaces():
    """Real media filenames have spaces (e.g. "2022-05-28 23.00.21.mp4") --
    the relative input path and the output path must both be shell-quoted."""
    import shlex

    from nagare_clip.pipeline.external import build_snapshot_batch_cmd

    relative = "2022-05-28 23.00.21.mp4"
    out_path = "/output/gap_context/frames/2022-05-28 23.00.21/1.000.jpg"
    jobs = [(relative, 1.0, out_path)]
    cmd = build_snapshot_batch_cmd(Path("/proj"), jobs, 960)
    script = cmd[-1].strip()
    # shlex.split must recover the exact tokens -- proves proper quoting,
    # not naive string interpolation that would split on the space.
    tokens = shlex.split(script)
    assert relative in tokens
    assert out_path in tokens
    # Un-shell-quoted, the raw string would be split by whitespace and the
    # command would break; confirm the quoting is actually present in the
    # source script text (not just recoverable by luck).
    assert shlex.quote(relative) in script
    assert shlex.quote(out_path) in script
