"""Pure timing helpers shared by the plan and director stages.

Extract per-segment times from a WhisperX JSON and render a compact
``[dur, gap]`` bracket.  No I/O, no internal imports — safe to import anywhere.
"""

from __future__ import annotations

from typing import Any


def segment_times(json_data: dict[str, Any]) -> list[tuple[float | None, float | None]]:
    """Return ``(start, end)`` per WhisperX segment (``None`` when missing)."""
    out: list[tuple[float | None, float | None]] = []
    for seg in json_data.get("segments", []):
        if not isinstance(seg, dict):
            out.append((None, None))
            continue
        start = seg.get("start")
        end = seg.get("end")
        start = float(start) if isinstance(start, (int, float)) else None
        end = float(end) if isinstance(end, (int, float)) else None
        out.append((start, end))
    return out


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def span_silence(start: float | None, end: float | None, cuts: list[tuple[float, float]]) -> float:
    """Seconds of ``[start, end]`` covered by the (merged) *cuts* ranges."""
    if start is None or end is None or end <= start:
        return 0.0
    total = 0.0
    for c_start, c_end in _merge_ranges(cuts):
        lo, hi = max(start, c_start), min(end, c_end)
        if hi > lo:
            total += hi - lo
    return total


def segment_silences(
    seg_times: list[tuple[float | None, float | None]],
    cuts: list[tuple[float, float]],
) -> list[float]:
    """Per-segment internal-silence seconds (0.0 for unknown times)."""
    merged = _merge_ranges(cuts)
    out: list[float] = []
    for start, end in seg_times:
        if start is None or end is None or end <= start:
            out.append(0.0)
            continue
        total = 0.0
        for c_start, c_end in merged:
            lo, hi = max(start, c_start), min(end, c_end)
            if hi > lo:
                total += hi - lo
        out.append(total)
    return out


def format_dur_gap(dur: float | None, gap: float | None, silence: float | None = None) -> str:
    """Compact bracket: ``[4.2s, gap 0.8s]`` / ``[13.0s speech, 62.9s silence]``.

    - ``dur is None`` -> ``""`` (no bracket at all).
    - ``gap is None`` -> no gap part.
    - a negligible gap (would render as ``0.0s``, incl. negative) is omitted
      the same way — "gap 0.0s" on every contiguous line/part is pure noise.
    - a significant *silence* (audio_silence-cut seconds inside the span)
      splits the duration into ``Xs speech, Ys silence`` — *dur* is then the
      speech-only figure; a negligible/absent silence renders the plain form.
    """
    if dur is None:
        return ""
    if silence is not None and f"{max(silence, 0.0):.1f}" != "0.0":
        core = f"{dur:.1f}s speech, {silence:.1f}s silence"
    else:
        core = f"{dur:.1f}s"
    if gap is None or f"{max(gap, 0.0):.1f}" == "0.0":
        return f"[{core}]"
    return f"[{core}, gap {gap:.1f}s]"
