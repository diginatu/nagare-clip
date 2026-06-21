"""Parse ffmpeg ``silencedetect`` output into cut ranges.

The ffmpeg invocation itself is driven by ``scripts/run_pipeline.sh`` inside
the whisperx Docker image (mirroring transcription). This module only builds the
ffmpeg argument list and parses the captured stderr, so it stays pure and
needs no ffmpeg/Docker in the test path.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)"
)
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def _parse_duration(stderr: str) -> float | None:
    m = _DURATION_RE.search(stderr)
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_silencedetect_output(stderr: str) -> List[Tuple[float, float]]:
    """Parse ffmpeg stderr into a list of ``(start, end)`` silence ranges.

    A trailing ``silence_start`` with no matching ``silence_end`` (silence
    that runs to EOF) is closed with the media duration parsed from ffmpeg's
    own ``Duration:`` line. Unparseable lines and orphan ``silence_end``
    lines are ignored.
    """
    duration = _parse_duration(stderr)
    ranges: List[Tuple[float, float]] = []
    pending_start: float | None = None

    for line in stderr.splitlines():
        start_m = _SILENCE_START_RE.search(line)
        if start_m:
            pending_start = float(start_m.group(1))
            continue
        end_m = _SILENCE_END_RE.search(line)
        if end_m and pending_start is not None:
            end = float(end_m.group(1))
            if end > pending_start:
                ranges.append((pending_start, end))
            pending_start = None

    if pending_start is not None and duration is not None and duration > pending_start:
        ranges.append((pending_start, duration))

    return ranges


def build_ffmpeg_args(
    rel_source: str, noise: float, min_silence: float
) -> List[str]:
    """ffmpeg argument list run inside the whisperx container.

    ``rel_source`` is the path relative to the mounted input-videos dir
    (container ``working_dir`` is ``/app``) — the same value transcription passes.
    """
    return [
        "-hide_banner",
        "-nostats",
        "-i",
        rel_source,
        "-af",
        f"silencedetect=noise={noise}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
