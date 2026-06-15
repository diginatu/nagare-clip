"""Pure timing helpers shared by the plan and director stages.

Extract per-segment times from a WhisperX JSON and render a compact
``[dur, gap]`` bracket.  No I/O, no internal imports — safe to import anywhere.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def segment_times(
    json_data: Dict[str, Any]
) -> List[Tuple[Optional[float], Optional[float]]]:
    """Return ``(start, end)`` per WhisperX segment (``None`` when missing)."""
    out: List[Tuple[Optional[float], Optional[float]]] = []
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


def format_dur_gap(dur: Optional[float], gap: Optional[float]) -> str:
    """Compact bracket: ``[4.2s, gap 0.8s]``.

    - ``dur is None`` -> ``""`` (no bracket at all).
    - ``gap is None`` -> ``"[4.2s]"``.
    - negative ``gap`` is clamped to ``0.0``.
    """
    if dur is None:
        return ""
    if gap is None:
        return f"[{dur:.1f}s]"
    if gap < 0:
        gap = 0.0
    return f"[{dur:.1f}s, gap {gap:.1f}s]"
