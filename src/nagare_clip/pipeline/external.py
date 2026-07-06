"""External-process command construction and execution.

Only two genuine process boundaries remain in the pipeline: the whisperx
Docker image (WhisperX transcription and ffmpeg silencedetect) and headless
Blender. Command lines are built by pure functions so they are testable
without Docker or Blender installed.
"""

from __future__ import annotations

import os
import subprocess
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
        "docker", "compose", "-f", str(project_root / "docker-compose.yml"),
        "run", "--rm", "--user", "0:0",
    ]


def build_transcription_cmd(
    project_root: Path, relatives: list[str], cfg: dict
) -> list[str]:
    t = cfg["transcription"]
    cmd = [
        *_compose_prefix(project_root),
        "whisperx",
        "_",
        *relatives,
        "--output_dir", "/output/transcription",
        "--output_format", "all",
        "--language", t["language"],
        "--compute_type", t["compute_type"],
        "--batch_size", str(t["batch_size"]),
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
        "--entrypoint", "ffmpeg", "whisperx",
        "-hide_banner", "-nostats",
        "-i", relative,
        "-af", f"silencedetect=noise={noise}dB:d={min_silence}",
        "-f", "null", "-",
    ]


def build_blender_cmd(
    project_root: Path,
    source_paths: list[Path],
    intervals_paths: list[Path],
    output_blend: Path,
    config_path: Path | None,
    log_file: Path,
) -> list[str]:
    cmd = [
        "blender", "--background", "--factory-startup",
        "--python-exit-code", "1",
        "--python", str(project_root / "src/nagare_clip/blender/blender_cli.py"),
        "--",
    ]
    for src in source_paths:
        cmd += ["--source", str(src)]
    for ivp in intervals_paths:
        cmd += ["--intervals", str(ivp)]
    cmd += ["--output", str(output_blend)]
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
            subprocess.run(
                cmd, check=True, env=env, stdout=subprocess.DEVNULL, stderr=f
            )
    else:
        subprocess.run(cmd, check=True, env=env)
