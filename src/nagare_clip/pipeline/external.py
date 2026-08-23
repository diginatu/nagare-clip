"""External-process command construction and execution.

Only two genuine process boundaries remain in the pipeline: the whisperx
Docker image (WhisperX transcription and ffmpeg silencedetect) and headless
Blender. Command lines are built by pure functions so they are testable
without Docker or Blender installed.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

JA_DEFAULT_ALIGN_MODEL = "vumichien/wav2vec2-large-xlsr-japanese"


def effective_align_model(cfg: dict) -> str:
    """Configured align model, else the Japanese default, else empty."""
    t = cfg["transcription"]
    if t["align_model"]:
        return t["align_model"]
    return JA_DEFAULT_ALIGN_MODEL if t["language"] == "ja" else ""


def _compose_prefix(project_root: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(project_root / "docker-compose.yml"),
        "run",
        "--rm",
        "--user",
        "0:0",
    ]


def build_transcription_cmd(project_root: Path, relatives: list[str], cfg: dict) -> list[str]:
    t = cfg["transcription"]
    cmd = [
        *_compose_prefix(project_root),
        "whisperx",
        "_",
        *relatives,
        "--output_dir",
        "/output/transcription",
        "--output_format",
        "all",
        "--language",
        t["language"],
        "--compute_type",
        t["compute_type"],
        "--batch_size",
        str(t["batch_size"]),
    ]
    align = effective_align_model(cfg)
    if align:
        cmd += ["--align_model", align]
    return cmd


def build_silencedetect_cmd(
    project_root: Path, relative: str, noise: float, min_silence: float
) -> list[str]:
    return [
        *_compose_prefix(project_root),
        "--entrypoint",
        "ffmpeg",
        "whisperx",
        "-hide_banner",
        "-nostats",
        "-i",
        relative,
        "-af",
        f"silencedetect=noise={noise}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]


def build_snapshot_batch_cmd(
    project_root: Path,
    jobs: Sequence[tuple[str, float, str]],
    width: int,
    ssim_jobs: Sequence[tuple[str, str, str]] = (),
) -> list[str]:
    """Every gap-context frame for the whole run, via ONE whisperx container.

    A `docker compose run` pays ~0.8s of container + nvidia-runtime init
    regardless of how little work it does inside; the actual ffmpeg snapshot
    is ~30ms. Running one container per frame (the original design) made
    container startup the dominant cost -- measured 2.47s for 3 frames as 3
    separate containers vs. 0.85s for the same 3 frames batched into one
    (byte-identical JPEGs either way). This mirrors the transcription
    stage's "single Docker container for all source files" precedent.

    *jobs* is `(relative, time_s, out_container_path)` tuples; each becomes
    one `ffmpeg` line in a shell script run via `sh -c` inside the whisperx
    image. Flags match the previous per-frame command exactly (input-side
    `-ss` for fast seek, `-frames:v 1`, `scale={width}:-2`, `-q:v 4`) so
    output is byte-identical; `-nostdin` is added since many ffmpeg
    invocations now share one shell. Each line ends in `|| true` so one bad
    seek can't take down the rest of the batch. Paths are shell-quoted --
    real media filenames contain spaces.

    *ssim_jobs* appends one SSIM comparison per CONSECUTIVE pair of a gap's
    extracted frames (a 3-frame gap yields 2: first-vs-mid, mid-vs-last)
    after all extraction lines -- same container, ~10ms each; each stats
    file is parsed host-side and the minimum of a gap's pair scores is used
    to prefilter pixel-static gaps (comparing only first-vs-last would miss
    a camera pan-away-and-return, since the middle frame -- already
    extracted, already paid for -- is the one that would reveal the
    on-screen action). A failed comparison (`|| true`, no stats file)
    simply drops that pair's score from the min.
    """
    lines = [
        "ffmpeg -hide_banner -nostats -loglevel error -nostdin -y "
        f"-ss {time_s:.3f} -i {shlex.quote(relative)} -frames:v 1 "
        f"-vf scale={width}:-2 -q:v 4 {shlex.quote(out_container_path)} || true"
        for relative, time_s, out_container_path in jobs
    ]
    lines += [
        "ffmpeg -hide_banner -nostats -loglevel error -nostdin "
        f"-i {shlex.quote(first)} -i {shlex.quote(last)} "
        f"-filter_complex {shlex.quote(f'ssim=stats_file={stats_out}')} -f null - || true"
        for first, last, stats_out in ssim_jobs
    ]
    script = "\n".join(lines)
    return [
        *_compose_prefix(project_root),
        "--entrypoint",
        "sh",
        "whisperx",
        "-c",
        script,
    ]


def build_blender_cmd(
    project_root: Path,
    source_paths: list[Path],
    intervals_paths: list[Path],
    output_blend: Path,
    config_path: Path | None,
    log_file: Path,
    manifest: Path | None = None,
) -> list[str]:
    cmd = [
        "blender",
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(project_root / "src/nagare_clip/blender/blender_cli.py"),
        "--",
    ]
    for src in source_paths:
        cmd += ["--source", str(src)]
    for ivp in intervals_paths:
        cmd += ["--intervals", str(ivp)]
    cmd += ["--output", str(output_blend)]
    if manifest is not None:
        cmd += ["--manifest", str(manifest)]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    cmd += ["--log-file", str(log_file)]
    return cmd


def run_command(
    cmd: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    stderr_to: Path | None = None,
) -> None:
    """Run *cmd* with check=True; optionally merge env vars and capture stderr."""
    env = {**os.environ, **env_extra} if env_extra else None
    if stderr_to is not None:
        with stderr_to.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, check=True, env=env, stdout=subprocess.DEVNULL, stderr=f)
    else:
        subprocess.run(cmd, check=True, env=env)


def run_magick(cmd: list[str]) -> str:
    """Run an ImageMagick command and return its stdout.

    ImageMagick is a host binary here, like `blender` -- the whisperx image has
    neither ImageMagick nor CJK fonts, and font slots resolve through host
    fontconfig.  Never `shell=True`: the copy is LLM-written.
    """
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
