"""Human-editable cut-list file: one ``START - END`` range per line."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Sequence, Tuple

logger = logging.getLogger(__name__)

_HEADER = (
    "# nagare-clip audio-silence cut ranges (seconds, START - END).\n"
    "# Each line marks a silent span that will be CUT from the video.\n"
    "# Edit the times or delete a line to KEEP that span.\n"
    "# Lines starting with '#' and blank lines are ignored.\n"
)

_RANGE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$"
)


def write_cuts(path: Path, ranges: Sequence[Tuple[float, float]]) -> None:
    """Write *ranges* (sorted by start) to *path* with an editing header."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_HEADER]
    for start, end in sorted(ranges, key=lambda r: r[0]):
        lines.append(f"{start:.3f} - {end:.3f}\n")
    path.write_text("".join(lines), encoding="utf-8")


def read_cuts(path: Path) -> List[Tuple[float, float]]:
    """Parse a cut-list file. Missing file ⇒ ``[]``.

    Blank lines and ``#`` comments are skipped silently; malformed lines and
    non-positive-length ranges are skipped with a warning.
    """
    path = Path(path)
    if not path.exists():
        return []

    ranges: List[Tuple[float, float]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _RANGE_RE.match(line)
        if not m:
            logger.warning("Ignoring malformed cut line: %r", raw)
            continue
        start, end = float(m.group(1)), float(m.group(2))
        if start >= end:
            logger.warning("Ignoring non-positive cut range: %r", raw)
            continue
        ranges.append((start, end))
    return ranges
