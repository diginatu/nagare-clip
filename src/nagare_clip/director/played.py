"""The edit as the viewer HEARS it: the transcript with every op applied.

The view numbers what was shot; the preview prices what each op plays.
Neither one lets you read the finished video top to bottom, and that is where
a cut goes wrong: one that is fine on its own may leave a line that answers a
question nobody asks any more, or a "so" with nothing before it.  A real edit
shipped like that and the director did not notice, because nothing showed
the lines that remain next to each other.

:func:`played_transcript` lists them, in playback order: every line that
plays heard at speed, with its ``edit`` ops applied and its captions under
it; a cut as one ``✂`` marker where its lines were, so a line that no longer
follows names the cut to look at; a timelapse (or any speed at
:data:`~.preview.UNINTELLIGIBLE_FACTOR` or faster) as one ``⏩`` marker,
since its speech cannot be followed; a silence line only where an op keeps
it.

Nothing is re-modelled: the stretches are :func:`~.preview.timeline_runs`'
(through :func:`~.preview.segment_preview`), the same resolved placements
guided_edit uses, so this and ``preview`` cannot disagree about what plays.
Markers already in the edit lines are not played back, as in the preview.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING

from nagare_clip.director.display import INDENT, DisplayView
from nagare_clip.director.ops import DirectorOp
from nagare_clip.director.preview import (
    SPED,
    UNINTELLIGIBLE_FACTOR,
    Run,
    _lines_phrase,
    _order_breaks,
    _playback_position,
    _s,
    numbering_for,
    segment_preview,
)
from nagare_clip.director.transcript import clean_for_display

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nagare_clip.director.load import SegmentTranscript

HEADER = (
    "THE TRANSCRIPT AS IT PLAYS: every op applied, in playback order. Read it "
    "top to bottom as the viewer hears it — does each line still follow from "
    "the one before? Cut and timelapsed speech is not heard, so it is not "
    "listed; a ✂/⏩ marker stands where it was, so a line that no longer "
    "follows names the op to look at."
)

UNTIMED = "(no line timings: the lines below are shown without this source's ops)"


def _texts(
    view: DisplayView,
    transcripts: Sequence[SegmentTranscript],
    ops: Mapping[int, Sequence[DirectorOp]],
) -> tuple[dict[int, str], set[int]]:
    """``(display line -> text heard, display lines an edit op changed)``."""
    out: dict[int, str] = {}
    edited: set[int] = set()
    for seg, t in zip(view.segments, transcripts):
        clean = clean_for_display(t.edit_lines)
        num = numbering_for(view, seg.index)
        for n in range(seg.first, seg.last + 1):
            line = view.lines[n - 1]
            if line.is_silence:
                out[n] = line.text
            else:
                out[n] = clean[line.source_line - t.first_line]
        for op in ops.get(seg.index, ()):
            if op.type != "edit" or op.old is None or op.new is None:
                continue
            n = num.of(op.lines[0])
            text = out.get(n)
            # The write check guarantees exactly once; a stale op on disk that
            # no longer matches is left unapplied, as guided_edit leaves it.
            if text is not None and text.count(op.old) == 1:
                out[n] = text.replace(op.old, op.new)
                edited.add(n)
    return out, edited


def _collapsed(run: Run) -> bool:
    """Does *run* play its speech too fast to follow (or not at all)?"""
    return run.kind in SPED and (run.factor or 1.0) >= UNINTELLIGIBLE_FACTOR


Stamp = Callable[[int], str | None]


def _dropped_row(lines: list[int]) -> str:
    """One row for consecutive lines *when* says are not in the finished video."""
    return f"{INDENT}✂ dropped {_lines_phrase(lines)} — not in the finished video"


def _line_rows(
    numbers: Sequence[int],
    row: Callable[[int], list[str]],
    when: Stamp | None,
) -> list[str]:
    """``row(n)`` per line, prefixed with ``when(n)``; a run of lines *when*
    maps to ``None`` becomes one :func:`_dropped_row` instead of a stamp."""
    rows: list[str] = []
    dropped: list[int] = []
    for n in numbers:
        stamp = None if when is None else when(n)
        if when is not None and stamp is None:
            dropped.append(n)
            continue
        if dropped:
            rows.append(_dropped_row(dropped))
            dropped = []
        first, *rest = row(n)
        rows.append(("" if stamp is None else stamp + "  ") + first)
        rows.extend(rest)
    if dropped:
        rows.append(_dropped_row(dropped))
    return rows


def _run_rows(
    run: Run,
    view: DisplayView,
    texts: Mapping[int, str],
    edited: AbstractSet[int],
    captions: Mapping[int, list[tuple[str, float]]],
    when: Stamp | None = None,
) -> list[str]:
    def stamp(n: int | None) -> str:
        text = None if when is None or n is None else when(n)
        return "" if text is None else text + "  "

    where = _lines_phrase(run.lines) if run.lines else ""
    if run.kind == "cut":
        return [f"{INDENT}✂ cut {where}"]
    if _collapsed(run):
        row = f"{stamp(run.lines[0] if run.lines else None)}{INDENT}⏩ {run.kind} {where} x{run.factor}"
        if run.text:
            row += " 「" + run.text.replace("\n", "\\n") + "」"
        rows = [row + f", {_s(run.seconds)} on screen — its speech is not followed"]
        for n in run.lines:
            rows.extend(
                f"{INDENT}  +「{text}」 {seconds:.1f} s"
                for text, seconds in captions.get(n, [])
                if text != run.text
            )
        return rows
    if not run.lines:
        # A wait with no line of its own that an op plays (a "n~" edge).
        place = "before line 1" if run.after == 0 else f"after line {run.after}"
        return [f"{INDENT}(the silence {place} plays, {_s(run.seconds)})"]

    def row(n: int) -> list[str]:
        head = f"{n}: {texts[n]}"
        if n in edited:
            head += "  (edited)"
        if run.kind in SPED:
            head += f"  (x{run.factor})"
        return [
            head,
            *(f"{INDENT}+「{text}」 {seconds:.1f} s" for text, seconds in captions.get(n, [])),
        ]

    heard = [n for n in run.lines if not view.lines[n - 1].is_silence or run.kind == "keep"]
    return _line_rows(heard, row, when)


def played_transcript(
    view: DisplayView,
    transcripts: Sequence[SegmentTranscript],
    ops: Mapping[int, Sequence[DirectorOp]],
    *,
    order: Sequence[tuple[int, int]] = (),
    only: AbstractSet[int] | None = None,
    when: Stamp | None = None,
) -> str:
    """The finished video as text, in playback order (see the module doc).

    *ops* are keyed by the segment's 1-based index, in source coordinates, as
    :func:`~.preview.edit_state` takes them; *order* is the order in force in
    display ranges (empty: shooting order); *only* narrows to those segments.
    *when* (display line -> text), when given, prefixes every played line row
    and every ``⏩`` row with ``when(its first display line) + "  "`` (the
    publish view's finished times); a line it maps to ``None`` is not in the
    finished video, and consecutive such lines become one ``✂ dropped`` row.
    In a source the preview cannot time, *when* also decides which silence
    lines are listed.  ``None`` leaves the output unchanged.
    """
    breaks = _order_breaks(order, len(view.lines))
    texts, edited = _texts(view, transcripts, ops)
    entries: list[tuple[int, Run | None]] = []
    captions: dict[int, list[tuple[str, float]]] = {}
    for seg in view.segments[: len(transcripts)]:
        if only is not None and seg.index not in only:
            continue
        preview = segment_preview(view, transcripts, seg.index, ops.get(seg.index, ()), breaks)
        for line, text, seconds in preview.captions:
            captions.setdefault(line, []).append((text, seconds))
        if not preview.runs:
            entries.append((seg.index, None))
        entries.extend((seg.index, run) for run in preview.runs)

    def first_line(entry: tuple[int, Run | None]) -> int:
        index, run = entry
        if run is None:
            return view.segments[index - 1].first
        return run.lines[0] if run.lines else (run.after or 0) + 1

    if breaks:
        position = _playback_position(order)
        entries.sort(key=lambda e: position(first_line(e)))

    out = [HEADER]
    current: int | None = None
    previous: int | None = None
    for index, run in entries:
        start = first_line((index, run))
        if index != current or (previous is not None and start != previous + 1):
            seg = view.segments[index - 1]
            out.append("")
            out.append(f"▶ [{index}] {seg.label}")
            current = index
        if run is None:
            seg = view.segments[index - 1]
            out.append(f"{INDENT}{UNTIMED}")
            # Without timings the source's ops are not modelled, so a silence
            # line is listed only when *when* says the finished video plays it.
            listed = [
                n
                for n in range(seg.first, seg.last + 1)
                if not view.lines[n - 1].is_silence or (when is not None and when(n) is not None)
            ]
            out.extend(_line_rows(listed, lambda n: [f"{n}: {texts[n]}"], when))
            previous = seg.last
            continue
        out.extend(_run_rows(run, view, texts, edited, captions, when))
        if run.lines:
            previous = run.lines[-1]
    return "\n".join(out)


__all__ = ["HEADER", "played_transcript"]
