"""The segment order: what the finished video plays, and in what order.

A **segment** is a stem plus an optional line range; ``lines=None`` means the
whole source.  Shooting order — one whole-source segment per source, in name
order — is the identity value, so a project with no order behaves exactly as the
pipeline did before the order existed, and the fallback needs no input beyond the
list of stems.

Two spellings of the same thing must not be distinguishable downstream:
``Segment(stem, (1, N))`` and ``Segment(stem, None)`` mean the whole source, and
:func:`normalise` collapses the former into the latter wherever the line count is
known.  Without that, "identity behaves like today" would hold only when the
``order`` key is absent, not when it is present and identity.

**Coverage is the contract, sequence is free:** the segments must cover every
line of every source exactly once (:func:`validate_segments`).  Deleting footage
is the director's job; an order that could drop lines by omitting them would make
a missing scene indistinguishable from an editorial decision.

The module is deliberately dependency-free (stdlib only, no stage imports), so
it can be imported from the pipeline process and from Blender's own Python
alike.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The ordered manifest the intervals stage writes into its own output dir.
MANIFEST_NAME = "timeline.json"


@dataclass(frozen=True)
class Segment:
    """One stretch of one source, in line space.  ``lines=None`` = the whole source.

    *gap_end* says the segment ends on the silence AFTER ``lines[1]`` rather
    than on that line — spelled ``"57~"`` on disk, as ``_director.json`` spells
    a silence edge.  Without it the silence after a segment's last line belongs
    to whatever plays line ``lines[1] + 1``; with it, to this segment.  A
    segment never needs a flag at its START: the silence before its first line
    is already its own unless the segment before it claimed it.
    """

    stem: str
    lines: tuple[int, int] | None = None
    gap_end: bool = False


@dataclass(frozen=True)
class TimelineSegment:
    """One manifest entry: the same stretch, resolved to source seconds."""

    stem: str
    start: float
    end: float
    lines: tuple[int, int] | None = None
    #: Carried from :attr:`Segment.gap_end` so the manifest spells the range
    #: the way ``order.json`` does (``"5~"``); the seconds already account for it.
    gap_end: bool = False


def segment_label(segment: Segment) -> str:
    """How a segment is named to a human or an LLM.

    A whole-source segment is its stem alone, so an identity order reads exactly
    as it did before segments existed; a partial one carries its range.
    """
    if segment.lines is None:
        return segment.stem
    return f"{segment.stem} [{segment.lines[0]}-{_end(segment)}]"


def segment_unit(segment: Segment) -> str:
    """The ``llm_report`` unit name for one segment's call.

    Same rule as :func:`segment_label` in a filename-safe form, so identity runs
    keep the report filenames they have always had.
    """
    if segment.lines is None:
        return segment.stem
    return f"{segment.stem}_{segment.lines[0]}-{_end(segment)}"


def _end(segment: Segment | TimelineSegment) -> str:
    assert segment.lines is not None
    return f"{segment.lines[1]}~" if segment.gap_end else str(segment.lines[1])


def identity_segments(stems: Sequence[str]) -> list[Segment]:
    """Shooting order, expressed as segments: one whole source each."""
    return [Segment(stem, None) for stem in stems]


def normalise(segments: Iterable[Segment], line_counts: Mapping[str, int]) -> list[Segment]:
    """Collapse a full-range segment into the whole-source form.

    A ``gap_end`` on a source's last line means nothing — no line follows it
    to take the silence from — so it is dropped, which is also what lets a
    full range ending ``"N~"`` collapse.  A stem with no known line count is
    left exactly as written.
    """
    out: list[Segment] = []
    for seg in segments:
        count = line_counts.get(seg.stem)
        if seg.lines is None:
            out.append(Segment(seg.stem, None))
        elif count is None:
            out.append(seg)
        elif seg.lines == (1, count):
            out.append(Segment(seg.stem, None))
        elif seg.gap_end and seg.lines[1] >= count:
            out.append(Segment(seg.stem, seg.lines))
        else:
            out.append(seg)
    return out


def validate_segments(segments: Sequence[Segment], line_counts: Mapping[str, int]) -> list[str]:
    """Every problem with an order, as readable strings.  Never raises.

    An order is valid when its segments partition every known source's lines
    exactly: no gap, no overlap, nothing outside ``1..N``, and no source left
    without a segment.  The sequence of the segments is not constrained — that
    is the whole point.
    """
    if not segments:
        return ["the order is empty"]

    problems: list[str] = []
    by_stem: dict[str, list[Segment]] = {}
    for seg in segments:
        if seg.stem not in line_counts:
            problems.append(f"unknown source {seg.stem!r} in the order")
            continue
        by_stem.setdefault(seg.stem, []).append(seg)

    for stem in line_counts:
        if stem not in by_stem:
            problems.append(f"no segment covers {stem!r}")

    for stem, segs in by_stem.items():
        count = line_counts[stem]
        if count < 1:
            problems.append(f"{stem}: has no lines to cover")
            continue
        ranges: list[tuple[int, int]] = []
        for seg in segs:
            start, end = seg.lines if seg.lines is not None else (1, count)
            if start > end:
                problems.append(f"{stem}: reversed line range {start}-{end}")
                continue
            if start < 1 or end > count:
                problems.append(f"{stem}: line range {start}-{end} is outside 1-{count}")
                continue
            ranges.append((start, end))
        cursor = 1
        for start, end in sorted(ranges):
            if start > cursor:
                problems.append(f"{stem}: lines {cursor}-{start - 1} are covered by no segment")
            elif start < cursor:
                problems.append(
                    f"{stem}: lines {start}-{min(end, cursor - 1)} are covered more than once"
                )
            cursor = max(cursor, end + 1)
        if cursor <= count:
            problems.append(f"{stem}: lines {cursor}-{count} are covered by no segment")
    return problems


def _pair(value: Any) -> tuple[int, int] | None:
    """A 2-integer pair, or ``None``.  ``bool`` is an ``int`` subclass — reject it."""
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        return None
    start, end = value
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return (start, end)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def segments_to_dict(segments: Iterable[Segment]) -> list[dict[str, Any]]:
    """The ``order`` array of ``plan.json``; a whole-source segment omits ``lines``."""
    out: list[dict[str, Any]] = []
    for seg in segments:
        entry: dict[str, Any] = {"stem": seg.stem}
        if seg.lines is not None:
            entry["lines"] = [seg.lines[0], _end(seg) if seg.gap_end else seg.lines[1]]
        out.append(entry)
    return out


def _segment_lines(value: Any) -> tuple[tuple[int, int], bool] | None:
    """``[a, b]`` or ``[a, "b~"]`` -> ``((a, b), gap_end)``; anything else ``None``."""
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        return None
    start, end = value
    gap_end = False
    if isinstance(end, str) and end.endswith("~") and end[:-1].isdigit():
        end, gap_end = int(end[:-1]), True
    pair = _pair((start, end))
    return None if pair is None else (pair, gap_end)


def segments_from_dict(data: Any) -> list[Segment]:
    """Read an ``order`` array leniently; a malformed entry is dropped.

    Dropping rather than raising is safe because the result still has to pass
    :func:`validate_segments`, and a dropped entry leaves a coverage hole that
    rejects the whole order.
    """
    if not isinstance(data, list):
        return []
    out: list[Segment] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        stem = raw.get("stem")
        if not isinstance(stem, str) or not stem:
            continue
        if raw.get("lines") is None:
            out.append(Segment(stem, None))
            continue
        parsed = _segment_lines(raw.get("lines"))
        if parsed is None:
            continue
        out.append(Segment(stem, parsed[0], parsed[1]))
    return out


def manifest_to_dict(entries: Iterable[TimelineSegment]) -> dict[str, Any]:
    """The ``timeline.json`` shape: playback order, in source seconds."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        item: dict[str, Any] = {
            "stem": entry.stem,
            "start": round(entry.start, 3),
            "end": round(entry.end, 3),
        }
        if entry.lines is not None:
            item["lines"] = [entry.lines[0], _end(entry) if entry.gap_end else entry.lines[1]]
        out.append(item)
    return {"segments": out}


def manifest_from_dict(data: Any) -> list[TimelineSegment]:
    """Read a manifest leniently; a malformed entry is dropped."""
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        return []
    out: list[TimelineSegment] = []
    for raw in data["segments"]:
        if not isinstance(raw, dict):
            continue
        stem = raw.get("stem")
        start = _number(raw.get("start"))
        end = _number(raw.get("end"))
        if not isinstance(stem, str) or not stem or start is None or end is None:
            continue
        parsed = _segment_lines(raw.get("lines")) if raw.get("lines") is not None else None
        lines, gap_end = parsed if parsed is not None else (None, False)
        out.append(TimelineSegment(stem=stem, start=start, end=end, lines=lines, gap_end=gap_end))
    return out


def read_manifest(path: Path | str | None) -> list[TimelineSegment]:
    """The manifest at *path*, or ``[]`` when it is missing or unreadable.

    A project built before this feature has no ``timeline.json``, and the
    consumers degrade to their per-source behaviour rather than failing.
    """
    if path is None:
        return []
    path = Path(path)
    if not path.is_file():
        # Absent is the normal degrade, not a fault worth a warning: it is what
        # every project built before the order existed looks like.
        logger.debug("order: no timeline manifest at %s", path)
        return []
    try:
        return manifest_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        logger.warning("order: could not read the timeline manifest %s", path)
        return []


def write_manifest(path: Path | str, entries: Iterable[TimelineSegment]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest_to_dict(entries), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
