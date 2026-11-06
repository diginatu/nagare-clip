"""Human-editable cut-list file: one ``START - END`` range per line."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADER = (
    "# nagare-clip audio-silence cut ranges (seconds, START - END).\n"
    "# Each line marks a silent span that will be CUT from the video.\n"
    "# Edit the times or delete a line to KEEP that span.\n"
    "# Lines starting with '#' and blank lines are ignored.\n"
)

_RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$")

#: The media duration ffmpeg reported, recorded as a comment so every reader of
#: the ranges ignores it.  The one place the pipeline keeps a source's length:
#: WhisperX's JSON has none, and a source with no speech has no word to end on
#: (:func:`nagare_clip.edit_lines.with_media_end`).
_DURATION_PREFIX = "# media duration: "
_DURATION_RE = re.compile(r"^#\s*media duration:\s*(\d+(?:\.\d+)?)\s*s?\s*$")


def write_cuts(
    path: Path, ranges: Sequence[tuple[float, float]], duration: float | None = None
) -> None:
    """Write *ranges* (sorted by start) to *path* with an editing header.

    *duration* (the media's, from ffmpeg) is recorded as a ``#`` comment.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_HEADER]
    if duration is not None and duration > 0:
        lines.append(f"{_DURATION_PREFIX}{duration:.3f}s\n")
    for start, end in sorted(ranges, key=lambda r: r[0]):
        lines.append(f"{start:.3f} - {end:.3f}\n")
    path.write_text("".join(lines), encoding="utf-8")


def read_cuts(path: Path) -> list[tuple[float, float]]:
    """Parse a cut-list file. Missing file ⇒ ``[]``.

    Blank lines and ``#`` comments are skipped silently; malformed lines and
    non-positive-length ranges are skipped with a warning.
    """
    path = Path(path)
    if not path.exists():
        return []

    ranges: list[tuple[float, float]] = []
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


def read_media_duration(path: Path | None) -> float | None:
    """The media duration a cut list recorded, or ``None``.

    ``None`` for a missing file, one written before the duration was recorded,
    or one whose comment a human deleted — the callers then degrade explicitly
    rather than guess.
    """
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _DURATION_RE.match(raw.strip())
        if m:
            value = float(m.group(1))
            return value if value > 0 else None
    return None
