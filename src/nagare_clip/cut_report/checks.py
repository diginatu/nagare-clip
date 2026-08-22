"""Where the finished cut breaches a number the pipeline already states.

Each finding carries the threshold it breached, so the number is arguable
rather than hidden -- the rule ``plan/divergence.py`` follows.  A check that
flags everything is the same as no check, so nothing here fires on a shape
that is merely unusual: 148 readable captions rode inside one 8x range on the
run this was built from, and only the seven already too dense to read before
compression are a defect.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nagare_clip.cut_report.metrics import (
    DEFAULT_FRAGMENT_THRESHOLD,
    DEFAULT_GAP_THRESHOLD,
    CutMetrics,
    Sources,
    covering_range,
)

DEFAULT_CAPTION_CPS = 18.0
DEFAULT_TIMELAPSE_MIN_SCREEN = 30.0
DEFAULT_TIMELAPSE_MAX_SCREEN = 180.0

# Most-severe first: a caption nobody can read is a defect, a timelapse that is
# over too soon is a judgement call, and Blender's own notices come last.
KIND_ORDER = (
    "caption-compressed",
    "caption-fast",
    "timelapse-short",
    "timelapse-long",
    "keep-gap",
    "keep-fragment",
    "blender-warning",
)

# Enough of a caption to recognise it in the report without pasting the line.
TEXT_PREVIEW = 40


@dataclass(frozen=True)
class Finding:
    kind: str
    stem: str
    detail: str
    text: str = ""

    @property
    def preview(self) -> str:
        if len(self.text) <= TEXT_PREVIEW:
            return self.text
        return self.text[:TEXT_PREVIEW] + "…"


def _caption_findings(sources: Sources, threshold: float) -> list[Finding]:
    out: list[Finding] = []
    for stem, data in sources:
        ranges = data.get("speed_ranges", []) or []
        for cap in data.get("captions", []) or []:
            start, end = float(cap["start"]), float(cap["end"])
            text = str(cap.get("text", ""))
            span = end - start
            if span <= 0 or not text:
                continue
            cps = len(text) / span
            if cps < threshold:
                continue
            sr = covering_range(ranges, start)
            if sr is None:
                out.append(
                    Finding(
                        kind="caption-fast",
                        stem=stem,
                        detail=(
                            f"[{start:.1f}s] {cps:.1f} chars/s over {span:.2f}s "
                            f"(threshold {threshold:.1f} chars/s)"
                        ),
                        text=text,
                    )
                )
                continue
            factor = float(sr["factor"]) or 1.0
            out.append(
                Finding(
                    kind="caption-compressed",
                    stem=stem,
                    detail=(
                        f"[{start:.1f}s] {cps:.1f} chars/s over {span:.2f}s "
                        f"(threshold {threshold:.1f} chars/s), inside the "
                        f"{float(sr['start']):.1f}-{float(sr['end']):.1f}s speed range "
                        f"at factor {factor:.1f} -> {span / factor:.2f}s on screen, "
                        f"{cps * factor:.1f} chars/s"
                    ),
                    text=text,
                )
            )
    return out


def _timelapse_findings(metrics: CutMetrics, minimum: float, maximum: float) -> list[Finding]:
    out: list[Finding] = []
    for span in metrics.speed_spans:
        if span.screen < minimum:
            kind, bound = "timelapse-short", f"under the {minimum:.1f}s floor"
        elif span.screen > maximum:
            kind, bound = "timelapse-long", f"over the {maximum:.1f}s ceiling"
        else:
            continue
        out.append(
            Finding(
                kind=kind,
                stem=span.stem,
                detail=(
                    f"[{span.start:.1f}-{span.end:.1f}s] factor {span.factor:.1f} over "
                    f"{span.kept:.1f}s of kept footage = {span.screen:.1f}s on screen, "
                    f"{bound} (the director prompt asks for about a minute)"
                ),
            )
        )
    return out


def _keep_findings(metrics: CutMetrics) -> list[Finding]:
    out = [
        Finding(
            kind="keep-gap",
            stem=s.stem,
            detail=(
                f"[{s.start:.1f}-{s.end:.1f}s] {s.length:.2f}s cut "
                f"(intervals.min_cut {metrics.gaps.threshold:.2f}s)"
            ),
        )
        for s in metrics.gaps.under
    ]
    out += [
        Finding(
            kind="keep-fragment",
            stem=s.stem,
            detail=(
                f"[{s.start:.1f}-{s.end:.1f}s] {s.length:.2f}s kept "
                f"(threshold {metrics.fragments.threshold:.2f}s)"
            ),
        )
        for s in metrics.fragments.under
    ]
    return out


def find_issues(
    sources: Sources,
    metrics: CutMetrics,
    *,
    caption_cps: float = DEFAULT_CAPTION_CPS,
    timelapse_min: float = DEFAULT_TIMELAPSE_MIN_SCREEN,
    timelapse_max: float = DEFAULT_TIMELAPSE_MAX_SCREEN,
    gap_threshold: float = DEFAULT_GAP_THRESHOLD,  # noqa: ARG001 - metrics owns it
    fragment_threshold: float = DEFAULT_FRAGMENT_THRESHOLD,  # noqa: ARG001
    blender_warnings: Sequence[str] = (),
) -> list[Finding]:
    """Every breached threshold, most severe first.

    ``gap_threshold``/``fragment_threshold`` are accepted so one call site can
    pass the whole threshold bundle, but the spans were already classified
    against them when *metrics* was measured.
    """
    found = _caption_findings(sources, caption_cps)
    found += _timelapse_findings(metrics, timelapse_min, timelapse_max)
    found += _keep_findings(metrics)
    found += [Finding(kind="blender-warning", stem="", detail=w) for w in blender_warnings]
    found.sort(key=lambda f: KIND_ORDER.index(f.kind) if f.kind in KIND_ORDER else len(KIND_ORDER))
    return found


def read_blender_warnings(path: Any) -> list[str]:
    """Blender's own WARNING lines, captured by the blender stage.

    They are the only notice that a requested interval did not fit, and they
    print into the same stream as Blender's unrelated ``bl_pkg``/``cattrs``
    startup tracebacks that the operator is told to ignore -- so the blender
    stage writes them to a file and they are read back here.  Best-effort:
    a missing or unreadable file simply means no warnings to surface.
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logging.warning("cut_report: could not read %s: %s", p, e)
        return []
    warnings = data.get("warnings") if isinstance(data, dict) else None
    return [str(w) for w in warnings] if isinstance(warnings, list) else []
