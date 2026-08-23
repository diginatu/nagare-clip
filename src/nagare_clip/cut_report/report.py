"""The finished-cut section of ``llm_report/index.md``.

Two kinds of content, and the distinction matters.  The **measurements** print
whether or not anything is wrong: they are the per-run regression table the
operator prompt asks a human to maintain by hand, and prompt changes regress as
often as they improve, so a report that only speaks up on failure cannot serve
that purpose.  The **findings** print only when a stated threshold is breached,
and each one carries that threshold.

Deliberately absent: any comparison against ``project.target_duration`` or the
brief's caption-density sentence.  Those are free text by design; the finished
duration and the overlay density are stated plainly and the human compares.
A wrong parse would be worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from nagare_clip.cut_report.checks import Finding, find_issues, read_blender_warnings
from nagare_clip.cut_report.metrics import CutMetrics, Sources, SpanStats, measure

HEADING = "## finished cut"


def _minutes(seconds: float) -> str:
    return f"{seconds / 60.0:.1f} min"


def _row(label: str, value: str, note: str = "") -> str:
    return f"{label:<18}{value:>10}" + (f"   {note}" if note else "")


def _span_row(label: str, stats: SpanStats, threshold_note: str) -> str:
    if not stats.count:
        return _row(label, "0", "")
    return _row(
        label,
        str(stats.count),
        f"min {stats.minimum:.2f}s, median {stats.median:.2f}s, "
        f"below {threshold_note}: {stats.below}",
    )


def format_measurements(m: CutMetrics) -> list[str]:
    """The numbers, always -- this is the regression table."""
    rows = [
        _row("source", _minutes(m.source_duration)),
        _row("finished", _minutes(m.finished_duration), f"({m.finished_share:.0%} of source)"),
        _row("  at 1x", _minutes(m.plain_duration), f"({m.plain_share:.0%})"),
        _row("  under timelapse", _minutes(m.sped_duration), f"({m.sped_share:.0%})"),
        _row("keep intervals", str(m.keep_intervals)),
        _row(
            "blender strips",
            str(m.strips),
            f"across {m.segments} segment(s) of {m.sources} source(s)",
        ),
        _row(
            "captions",
            str(m.captions),
            f"({m.captions_in_speed} start inside a speed range)",
        ),
        _row("overlays", str(m.overlays), f"({m.overlay_density:.2f} per finished minute)"),
        _span_row(
            "keep gaps",
            m.gaps,
            f"intervals.min_cut ({m.gaps.threshold:.2f}s)",
        ),
        _span_row("keep fragments", m.fragments, f"{m.fragments.threshold:.2f}s"),
    ]
    return ["```", *rows, "```"]


def format_timelapses(m: CutMetrics) -> list[str]:
    if not m.speed_spans:
        return []
    lines = [
        "",
        "| source | span | factor | on screen |",
        "|---|---|---|---|",
    ]
    for s in m.speed_spans:
        lines.append(f"| {s.stem} | {s.span:.1f}s | {s.factor:.1f} | {s.screen:.1f}s |")
    return lines


def format_findings(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["", "Every threshold checked is within threshold; nothing to report."]
    lines = ["", f"{len(findings)} finding(s):", ""]
    for f in findings:
        where = f" {f.stem}" if f.stem else ""
        lines.append(f"- **{f.kind}**{where} — {f.detail}")
        if f.text:
            lines.append(f"  - 「{f.preview}」")
    return lines


def format_cut_report(m: CutMetrics, findings: Sequence[Finding]) -> str:
    """The markdown block inlined into the LLM report's index."""
    lines = [
        HEADING,
        "",
        "Measured off the intervals JSONs the pipeline already wrote (plus Blender's "
        "own warnings). Deterministic — no LLM call. The measurements print every run; "
        "a finding names the threshold it breached so the number is arguable.",
        "",
        *format_measurements(m),
        *format_timelapses(m),
        *format_findings(findings),
    ]
    return "\n".join(lines) + "\n"


def build_cut_report(
    sources: Sources,
    cfg: dict[str, Any],
    *,
    blender_warnings: Sequence[str] = (),
    blender_warnings_path: Any = None,
) -> str:
    """Measure, check and render in one call. Empty string when disabled."""
    conf = cfg.get("cut_report", {})
    if not conf.get("enabled", True):
        return ""
    if blender_warnings_path is not None and not blender_warnings:
        blender_warnings = read_blender_warnings(blender_warnings_path)
    thresholds = {
        "caption_cps": conf.get("caption_chars_per_sec", 18.0),
        "timelapse_max": conf.get("timelapse_max_screen", 180.0),
        "gap_threshold": cfg.get("intervals", {}).get("min_cut", 0.4),
        "fragment_threshold": conf.get("min_keep_fragment", 1.0),
    }
    m = measure(
        sources,
        gap_threshold=thresholds["gap_threshold"],
        fragment_threshold=thresholds["fragment_threshold"],
    )
    return format_cut_report(
        m, find_issues(sources, m, blender_warnings=blender_warnings, **thresholds)
    )
