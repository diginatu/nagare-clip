"""Audio decoding through the host ffmpeg."""

import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


def decode_audio(path: Path) -> np.ndarray:
    """Decode any media file to float32 mono 16 kHz."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {path}: {proc.stderr.decode(errors='replace')[-500:]}"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()
