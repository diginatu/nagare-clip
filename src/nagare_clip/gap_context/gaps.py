"""The {stem}_gaps.json contract: described silent gaps, purely time-based.

Consumers (summary, director) anchor these to transcript lines themselves, so
no line numbers are baked into the artifact.  Reading is lenient: a malformed
entry is dropped (logged), never raised, so a hand-edited file cannot break the
pipeline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Gap:
    start: float
    end: float
    frames: list[str] = field(default_factory=list)  # relative to the stage dir
    description: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def gaps_to_dict(gaps: list[Gap]) -> dict[str, Any]:
    return {
        "gaps": [
            {
                "start": g.start,
                "end": g.end,
                "frames": list(g.frames),
                "description": g.description,
            }
            for g in gaps
        ]
    }


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def gaps_from_dict(data: Any) -> list[Gap]:
    if not isinstance(data, dict) or not isinstance(data.get("gaps"), list):
        return []
    out: list[Gap] = []
    for raw in data["gaps"]:
        if not isinstance(raw, dict):
            logger.warning("gap_context: dropping non-object gap entry")
            continue
        start = _coerce_float(raw.get("start"))
        end = _coerce_float(raw.get("end"))
        description = raw.get("description")
        if start is None or end is None or end <= start:
            logger.warning("gap_context: dropping gap with bad start/end: %r", raw)
            continue
        if not isinstance(description, str) or not description.strip():
            logger.warning("gap_context: dropping gap without a description: %r", raw)
            continue
        frames = raw.get("frames")
        frames = [f for f in frames if isinstance(f, str)] if isinstance(frames, list) else []
        out.append(Gap(start=start, end=end, frames=frames, description=description.strip()))
    return out


def load_gaps(path: Path | None) -> list[Gap]:
    """Read a gaps file. Missing/unreadable/invalid ⇒ ``[]`` (logged)."""
    if path is None or not Path(path).is_file():
        return []
    try:
        return gaps_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        logger.warning("gap_context: could not read %s: %s", path, e)
        return []
