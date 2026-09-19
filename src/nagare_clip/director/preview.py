"""What the director's ops will play, stated as facts (no I/O, no LLM).

The director writes ops over line ranges and never sees them played back.  Two
failures recur: a keep/timelapse ends at the last word of its last line, so the
gap after it is outside the op and dropped (``timelapse [53,53]`` "compressing
the 29.9 s wait" compresses 0.9 s of speech); and a timelapse over talking
renders that talking unintelligible without anything saying what it cost.  This
module computes, per op, what plays, for how long, what it swallows and what it
shows — the costs *and* what a span buys, because showing only costs pushes the
model off timelapses.

Facts only.  The one judgement is the user's rule that speech in a span at
:data:`UNINTELLIGIBLE_FACTOR` or faster cannot be followed.

Line granularity, from segment times: a line with no op plays its bracket
figure; a kept line plays its whole span; a gap between two lines plays only
when one kept range holds both; a sped line/gap plays over its factor.

Overlap resolution is not modelled here: :func:`resolve_placements` runs
guided_edit's own :func:`~nagare_clip.guided_edit.apply.resolve_span_ops` (after
the real timelapse expansion), so an op is reported where it will actually land.

Two figures, each the one the director already sees for its scope: a *line*'s
seconds are its transcript bracket (:func:`~.director_llm.line_seconds`, which
folds a sub-second silence back in), and a *segment*'s runtime sums
:func:`~.director_llm.speech_seconds`, as the whole-video header's "default
runtime" does, because the audio_silence cut removes that silence regardless.
Markers already in the edit lines steer the clipping but are not played back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nagare_clip.director.director_llm import (
    DirectorOp,
    clean_for_display,
    line_seconds,
    speech_seconds,
)
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.guided_edit.apply import blocking_types, resolve_span_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops
from nagare_clip.timing import gap_shown, silence_shown

#: Speech in a span sped up this much or more cannot be followed (user's rule).
UNINTELLIGIBLE_FACTOR = 2.0

#: How much of a swallowed line's text is quoted to identify it.
QUOTE_CHARS = 24

HEADER = "Playback of these ops, computed from line timings (approximate):"

SegTimes = Sequence[tuple[float | None, float | None]]


@dataclass(frozen=True)
class Placement:
    """One marker an op turns into, where it lands (``lines=None``: nowhere)."""

    kind: str
    op: DirectorOp
    lines: tuple[int, int] | None
    reason: str | None = None


def resolve_placements(
    edit_lines: list[str], ops: list[DirectorOp], seg_times: SegTimes
) -> list[list[Placement]]:
    """Each op's markers as guided_edit will place them, per op in *ops*.

    *edit_lines* and *seg_times* are indexed by absolute line number (index 0 =
    line 1).  A timelapse becomes up to three placements (caption overlay,
    speed, keep) via the real :func:`expand_timelapse_ops`; an ``edit`` none.
    """
    expanded: list[DirectorOp] = []
    owner: list[int] = []
    for i, op in enumerate(ops):
        for sub in expand_timelapse_ops([op], list(seg_times)):
            expanded.append(sub)
            owner.append(i)
    placed = resolve_span_ops(list(edit_lines), expanded)
    out: list[list[Placement]] = [[] for _ in ops]
    for j, sub in enumerate(expanded):
        p = placed.get(j)
        if p is None:
            continue
        out[owner[j]].append(Placement(sub.type, sub, p.lines if p.applied else None, p.reason))
    return out


@dataclass(frozen=True)
class SegmentPreview:
    text: str
    default_seconds: float | None
    runtime_seconds: float | None


def _s(sec: float) -> str:
    return f"{sec:.1f} s"


def _lines_phrase(lines: list[int]) -> str:
    if len(lines) == 1:
        return f"line {lines[0]}"
    if lines == list(range(lines[0], lines[-1] + 1)):
        return f"lines {lines[0]}-{lines[-1]}"
    return "lines " + ", ".join(str(n) for n in lines)


def _quote(text: str) -> str:
    text = text.replace("\n", " ").strip()
    return f"「{text[:QUOTE_CHARS]}…」" if len(text) > QUOTE_CHARS else f"「{text}」"


def _op_title(op: DirectorOp) -> str:
    head = f"{op.type} [{op.lines[0]},{op.lines[1]}]"
    if op.type in ("timelapse", "speed") and op.factor is not None:
        head += f" x{op.factor}"
    if op.text and op.type in ("timelapse", "overlay"):
        head += " 「" + op.text.replace("\n", "\\n") + "」"
    return head


def _op_ref(op: DirectorOp, index: int) -> str:
    return f"{op.type} [{op.lines[0]},{op.lines[1]}] (op {index + 1})"


class _Playback:
    """Per-line state of one segment once every placement has landed."""

    def __init__(
        self,
        first: int,
        seg_times: SegTimes,
        silences: Sequence[float] | None,
        placements: list[list[Placement]],
    ):
        self.first = first
        self.last = first + len(seg_times) - 1
        self.times = list(seg_times)
        sil = list(silences) if silences is not None else [0.0] * len(seg_times)
        self.sil = sil
        self.fig = line_seconds(seg_times, silences)
        self.speech = speech_seconds(seg_times, silences)
        self.cut: set[int] = set()
        self.keep: dict[int, tuple[int, int]] = {}
        self.speed: dict[int, tuple[float, tuple[int, int]]] = {}
        for i, per_op in enumerate(placements):
            for j, p in enumerate(per_op):
                if p.lines is None:
                    continue
                span = range(p.lines[0], p.lines[1] + 1)
                if p.kind == "cut":
                    self.cut.update(span)
                elif p.kind == "keep":
                    self.keep.update({n: (i, j) for n in span})
                elif p.kind == "speed":
                    self.speed.update({n: (p.op.factor or 1.0, (i, j)) for n in span})

    def _i(self, n: int) -> int:
        return n - self.first

    def start(self, n: int) -> float:
        return self.times[self._i(n)][0]

    def end(self, n: int) -> float:
        return self.times[self._i(n)][1]

    def span(self, n: int) -> float:
        return self.end(n) - self.start(n)

    def figure(self, n: int) -> float:
        return self.fig[self._i(n)]

    def shown_silence(self, n: int) -> float:
        sil = self.sil[self._i(n)]
        return sil if silence_shown(sil) else 0.0

    def gap_after(self, n: int) -> float | None:
        if n >= self.last:
            return None
        return self.start(n + 1) - self.end(n)

    def factor(self, n: int) -> float:
        return self.speed[n][0] if n in self.speed else 1.0

    def gap_plays(self, n: int) -> bool:
        return (
            n in self.keep
            and n + 1 in self.keep
            and self.keep[n] == self.keep[n + 1]
            and n not in self.cut
            and n + 1 not in self.cut
        )

    def gap_factor(self, n: int) -> float:
        if n in self.speed and n + 1 in self.speed and self.speed[n][1] == self.speed[n + 1][1]:
            return self.speed[n][0]
        return 1.0

    def line_footage(self, n: int, base: str) -> float:
        """Seconds of footage line *n* plays at 1x (0 when cut)."""
        if n in self.cut:
            return 0.0
        if n in self.keep:
            return self.span(n)
        value = self.figure(n) if base == "figure" else self.speech[self._i(n)]
        return value

    def played(self, lines: list[int], base: str) -> tuple[float, float, float]:
        """(footage at 1x, on-screen seconds, gap seconds) over *lines*."""
        footage = onscreen = gaps = 0.0
        members = set(lines)
        for n in lines:
            f = self.line_footage(n, base)
            footage += f
            onscreen += f / self.factor(n)
            if n + 1 in members and self.gap_plays(n):
                g = max(self.gap_after(n) or 0.0, 0.0)
                footage += g
                gaps += g
                onscreen += g / self.gap_factor(n)
        return footage, onscreen, gaps

    def all_lines(self) -> list[int]:
        return list(range(self.first, self.last + 1))

    def timeline_start(self, n: int) -> float:
        """Where line *n* starts on the edited timeline of this segment."""
        before = list(range(self.first, n))
        _, onscreen, _ = self.played(before, "speech")
        if before and self.gap_plays(n - 1):
            onscreen += max(self.gap_after(n - 1) or 0.0, 0.0) / self.gap_factor(n - 1)
        return onscreen


def _fate(pb: _Playback, n: int, ops: list[DirectorOp]) -> str:
    """What happens to the gap after line *n*: dropped, or which op keeps it."""
    if pb.gap_plays(n):
        i, _ = pb.keep[n]
        return f"covered by {_op_ref(ops[i], i)}"
    return "dropped"


def _gap_descriptions(
    pb: _Playback, n: int, anchored: list[tuple[int, Gap]] | None, first: int
) -> list[str]:
    """``[silent gap: …]`` lines the transcript shows for the gap after line *n*."""
    out = []
    for anchor, gap in anchored or []:
        if anchor + first - 1 != n:
            continue
        if (gap.start + gap.end) / 2 >= pb.end(n):
            out.append(f"    [silent gap: {gap.description}]")
    return out


def _clip_note(
    pb_placements: list[list[Placement]],
    ops: list[DirectorOp],
    own: int,
    p: Placement,
    label: str,
) -> list[str]:
    """Why *p* landed on fewer lines than sent (empty when it did not clip)."""
    sent = p.op.lines
    if p.lines is None:
        return [f"  {label}not applied: {p.reason}"]
    if p.kind == "overlay":
        if p.lines[0] == sent[0]:
            return []
        lost = [sent[0]]
        verb = f"moved to line {p.lines[0]}"
    else:
        if p.lines == tuple(sent):
            return []
        lost = [n for n in range(sent[0], sent[1] + 1) if not p.lines[0] <= n <= p.lines[1]]
        verb = f"clipped to [{p.lines[0]},{p.lines[1]}]"
    by: dict[int, list[int]] = {}
    unexplained: list[int] = []
    types = blocking_types(p.kind)
    for n in lost:
        hit = None
        for k, per_op in enumerate(pb_placements):
            if k == own:
                continue
            for q in per_op:
                if q.lines and q.kind in types and q.lines[0] <= n <= q.lines[1]:
                    hit = k
        if hit is None:
            unexplained.append(n)
        else:
            by.setdefault(hit, []).append(n)
    reasons = []
    for k, lines in by.items():
        verb_be = "is" if len(lines) == 1 else "are"
        reasons.append(f"{_lines_phrase(lines)} {verb_be} under {_op_ref(ops[k], k)}")
    if unexplained:
        verb_be = "is" if len(unexplained) == 1 else "are"
        reasons.append(f"{_lines_phrase(unexplained)} {verb_be} not free in the transcript")
    return [f"  {label}{verb}: " + "; ".join(reasons)]


def _range(p: Placement | None) -> list[int]:
    if p is None or p.lines is None:
        return []
    return list(range(p.lines[0], p.lines[1] + 1))


def preview_segment(
    edit_lines: list[str],
    ops: list[DirectorOp],
    *,
    seg_times: SegTimes | None,
    silences: Sequence[float] | None = None,
    anchored_gaps: list[tuple[int, Gap]] | None = None,
    first_line: int = 1,
    drops: Sequence[str] = (),
    label: str = "segment",
    elsewhere_seconds: float | None = None,
) -> SegmentPreview:
    """The playback facts for one segment's *ops*, as the director holds them.

    Takes what :func:`~.director_llm.generate_director_ops` holds: the raw
    segment *edit_lines*, *seg_times*/*silences* for those lines,
    *anchored_gaps* with segment-relative anchors, the segment's *first_line*,
    and the parsed *ops* plus the parser's *drops*.  *elsewhere_seconds* (the
    rest of the finished video's runtime) adds a whole-video estimate.
    """
    footer_drops = [f"dropped by the parser (no effect): {d}" for d in drops]
    timed = (
        seg_times is not None
        and len(seg_times) == len(edit_lines)
        and all(s is not None and e is not None for s, e in seg_times)
    )
    if not timed:
        text = "\n".join(
            ["Playback of these ops cannot be computed: this segment has no line timings."]
            + footer_drops
        )
        return SegmentPreview(text, None, None)

    clean = clean_for_display(edit_lines)
    pad = first_line - 1
    placements = resolve_placements(
        [""] * pad + list(edit_lines), ops, [(None, None)] * pad + list(seg_times)
    )
    pb = _Playback(first_line, seg_times, silences, placements)

    def text_of(n: int) -> str:
        return clean[n - first_line]

    # Captions on the edited timeline: (op index, line, seconds).
    captions: list[tuple[int, int, float]] = []
    for i, per_op in enumerate(placements):
        for p in per_op:
            if p.kind == "overlay" and p.lines is not None and p.op.duration:
                captions.append((i, p.lines[0], p.op.duration))

    def caption_lines(i: int) -> list[str]:
        out = []
        for k, line, dur in captions:
            if k != i:
                continue
            start = pb.timeline_start(line)
            for k2, line2, dur2 in captions:
                if k2 == i:
                    continue
                start2 = pb.timeline_start(line2)
                overlap = min(start + dur, start2 + dur2) - max(start, start2)
                if overlap > 0 and f"{overlap:.1f}" != "0.0":
                    out.append(
                        f"  on screen together with the caption of {_op_ref(ops[k2], k2)} "
                        f"for {_s(overlap)}"
                    )
        return out

    def boundary_lines(keep: Placement | None) -> list[str]:
        rng = _range(keep)
        if not rng:
            return []
        a, b = rng[0], rng[-1]
        out = []
        if a > first_line and gap_shown(pb.gap_after(a - 1)):
            fate = _fate(pb, a - 1, ops)
            out.append(
                f"  before line {a}: the {_s(pb.gap_after(a - 1))} gap after line {a - 1} "
                f"is outside this op — {fate}"
            )
            out.extend(_gap_descriptions(pb, a - 1, anchored_gaps, first_line))
        if b < pb.last and gap_shown(pb.gap_after(b)):
            fate = _fate(pb, b, ops)
            out.append(
                f"  after line {b}: the {_s(pb.gap_after(b))} gap before line {b + 1} "
                f"is outside this op — {fate}"
            )
            out.extend(_gap_descriptions(pb, b, anchored_gaps, first_line))
        return out

    def unintelligible_lines(speed: Placement | None) -> list[str]:
        if speed is None or speed.lines is None:
            return []
        factor = speed.op.factor or 1.0
        if factor < UNINTELLIGIBLE_FACTOR:
            return []
        lines = [n for n in _range(speed) if n not in pb.cut]
        total = sum(pb.figure(n) for n in lines)
        if len(lines) == 1:
            n = lines[0]
            return [
                f"  speech at x{factor} — unintelligible: line {n} {_s(pb.figure(n))} "
                f"{_quote(text_of(n))} (total {_s(total)})"
            ]
        out = [
            f"  speech at x{factor} — unintelligible (total {_s(total)} over {len(lines)} lines):"
        ]
        out.extend(f"    {n} {_s(pb.figure(n))} {_quote(text_of(n))}" for n in lines)
        return out

    def plays_line(lines: list[int], caption: float | None) -> list[str]:
        footage, onscreen, gaps = pb.played(lines, "figure")
        default = sum(pb.figure(n) for n in lines)
        line = (
            f"  plays {_s(footage)} of footage in {_s(onscreen)} "
            f"(default for {_lines_phrase(lines)}: {_s(default)})"
        )
        if caption is not None:
            line += f"; caption on screen {_s(caption)}"
        out = [line]
        speech = sum(pb.figure(n) for n in lines)
        silence = sum(pb.shown_silence(n) for n in lines if n in pb.keep)
        if f"{gaps:.1f}" != "0.0" or f"{silence:.1f}" != "0.0":
            out.append(
                f"  footage inside: {_s(speech)} speech, {_s(gaps)} gaps between lines, "
                f"{_s(silence)} silence within lines"
            )
        return out

    blocks: list[str] = []
    for i, op in enumerate(ops):
        body: list[str] = []
        per = {p.kind: p for p in placements[i]}
        if op.type == "edit":
            body.append("  no runtime change (a text edit within the line)")
        elif op.type == "overlay":
            p = per["overlay"]
            body.extend(_clip_note(placements, ops, i, p, ""))
            if p.lines is not None:
                body.append(
                    f"  no runtime change; caption on screen {_s(op.duration or 0.0)} "
                    f"from line {p.lines[0]}"
                )
                body.extend(caption_lines(i))
        elif op.type == "cut":
            p = per["cut"]
            body.extend(_clip_note(placements, ops, i, p, ""))
            lines = _range(p)
            if lines:
                removed = sum(pb.figure(n) for n in lines)
                body.append(f"  removes {_lines_phrase(lines)}: {_s(removed)}")
        elif op.type in ("keep", "speed"):
            p = per[op.type]
            body.extend(_clip_note(placements, ops, i, p, ""))
            lines = _range(p)
            if lines:
                body.extend(plays_line(lines, None))
                if op.type == "speed":
                    body.extend(unintelligible_lines(p))
                else:
                    body.extend(boundary_lines(p))
        elif op.type == "timelapse":
            keep, speed, cap = per.get("keep"), per.get("speed"), per.get("overlay")
            if keep and speed and keep.lines and keep.lines == speed.lines:
                body.extend(_clip_note(placements, ops, i, keep, ""))
            else:
                if keep:
                    body.extend(_clip_note(placements, ops, i, keep, "its keep: "))
                if speed:
                    body.extend(_clip_note(placements, ops, i, speed, "its speed-up: "))
            if cap:
                body.extend(_clip_note(placements, ops, i, cap, "its caption: "))
            lines = sorted(set(_range(keep)) | set(_range(speed)))
            if lines:
                caption = cap.op.duration if cap and cap.lines else None
                body.extend(plays_line(lines, caption))
                body.extend(unintelligible_lines(speed))
                body.extend(boundary_lines(keep))
                if caption is not None:
                    body.extend(caption_lines(i))
        blocks.append("\n".join([_op_title(op)] + body))

    default = sum(pb.speech)
    runtime = pb.played(pb.all_lines(), "speech")[1]
    footer = footer_drops + [f"{label}: default {_s(default)} → with these ops {_s(runtime)}"]
    if elsewhere_seconds is not None:
        footer.append(
            f"whole video (estimate): {_s(elsewhere_seconds)} elsewhere + {_s(runtime)} here "
            f"= {_s(elsewhere_seconds + runtime)}"
        )
    parts = [HEADER] + (blocks or ["(no ops: every line plays its default)"]) + ["\n".join(footer)]
    return SegmentPreview("\n\n".join(parts), default, runtime)
