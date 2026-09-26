"""Pure timing helpers shared by the summary and director stages.

Extract per-segment times from a WhisperX JSON, measure how much of a span
a set of dropped ranges covers (``span_silence``/``segment_silences`` — the
callers pass :func:`nagare_clip.intervals.keep.dropped_ranges`, what the
render drops), and render a compact ``[dur, gap]`` bracket —
or, when silence overlaps, the ``[Xs speech, Ys silence]`` form
(``format_dur_gap``).  No I/O, no internal imports — safe to import anywhere.
"""

from __future__ import annotations

from typing import Any

#: Internal silence below this many seconds does not split a bracket into
#: ``Xs speech, Ys silence``.  The split exists to flag dead air the editor
#: will drop; a few tenths of a second is breath.  The previous de facto
#: cut-off was ~0.05s (whatever survived rounding to one decimal), which split
#: 77 of a real run's 312 silence figures into noise and printed
#: ``0.0s speech, 0.1s silence`` on 8 lines whose WhisperX alignment had
#: collapsed.  All 235 figures of a second or more are unaffected.
MIN_SILENCE_SPLIT = 1.0


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
    return [span_silence(start, end, cuts) for start, end in seg_times]


def format_dur_gap(dur: float | None, gap: float | None, silence: float | None = None) -> str:
    """Compact bracket: ``[4.2s, gap 0.8s]`` / ``[13.0s speech, 62.9s silence]``.

    - ``dur is None`` -> ``""`` (no bracket at all).
    - ``gap is None`` -> no gap part.
    - a negligible gap (would render as ``0.0s``, incl. negative) is omitted
      the same way — "gap 0.0s" on every contiguous line/part is pure noise.
    - a significant *silence* (seconds of the span the render drops,
      ``>= MIN_SILENCE_SPLIT``) splits the duration into
      ``Xs speech, Ys silence`` — *dur* is then the speech-only figure.
    - a shorter silence is folded back in: callers pass *dur* already net of
      it, so the plain form has to add it back or the bracket under-reports
      the span it claims to be the duration of.  Without this a
      degenerate-alignment line (every word 0.020s, the whole span inside an
      audio_silence cut) renders ``[0.0s]`` — a line the LLM is asked to judge
      the pacing of, claiming to last no time at all.
    """
    if dur is None:
        return ""
    if silence_shown(silence):
        core = f"{dur:.1f}s speech, {silence:.1f}s silence"
    else:
        core = f"{bracket_seconds(dur, silence):.1f}s"
    if not gap_shown(gap):
        return f"[{core}]"
    return f"[{core}, gap {gap:.1f}s]"


def silence_shown(silence: float | None) -> bool:
    """Whether a bracket splits this internal silence out as ``Ys silence``."""
    return silence is not None and silence >= MIN_SILENCE_SPLIT


def bracket_seconds(dur: float, silence: float | None = None) -> float:
    """The one duration figure a line's bracket prints for it.

    *dur* is net speech (span minus what the render drops inside it); a silence
    too short to be split out is folded back in (see :func:`format_dur_gap`).
    Anything that quotes a line's duration back to the director reads it here,
    so it can never see two numbers for one line.
    """
    return dur if silence_shown(silence) else dur + (silence or 0.0)


def gap_shown(gap: float | None) -> bool:
    """Whether a bracket prints this trailing gap (it would not read ``0.0s``)."""
    return gap is not None and f"{max(gap, 0.0):.1f}" != "0.0"
