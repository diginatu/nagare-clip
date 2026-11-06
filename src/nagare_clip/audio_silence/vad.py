"""Silero VAD results: read the worker's ``{stem}_vad.json`` and derive silences."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path


def read_vad(path: Path) -> tuple[float, list[tuple[float, float]]]:
    """``(duration, speech)`` from a ``vad`` worker result (seconds)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    speech = [(float(a), float(b)) for a, b in data["speech"]]
    return float(data["duration"]), speech


def silences_from_speech(
    speech: Sequence[tuple[float, float]], duration: float, min_silence: float
) -> list[tuple[float, float]]:
    """The complement of *speech* within ``[0, duration]``.

    Gaps shorter than *min_silence* are dropped.
    """
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(speech):
        if start - cursor >= min_silence:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= min_silence:
        out.append((cursor, duration))
    return out
