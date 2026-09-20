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

An op with a silence edge (``"53~"``) is the exception: it is applied as a TIME
range, so it is priced from its resolved times — the silence line's interval
where the transcript shows one — and its line range says only where it hangs.
``["53~","53~"]`` therefore plays 29.9 s of footage in 6.0 s at x5 and makes no
speech unintelligible, which is the whole reason the form exists.

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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nagare_clip.director.director_llm import (
    DirectorOp,
    clean_for_display,
    line_seconds,
    speech_seconds,
)
from nagare_clip.director.display import DisplayView
from nagare_clip.director.silence_lines import INDENT, SilenceLine, silence_body
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.guided_edit.apply import blocking_types, is_time_resolved, resolve_span_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops
from nagare_clip.timing import gap_shown, silence_shown

if TYPE_CHECKING:  # pragma: no cover - typing only; run.py imports this module
    from nagare_clip.director.run import SegmentTranscript

#: Speech in a span sped up this much or more cannot be followed (user's rule).
UNINTELLIGIBLE_FACTOR = 2.0

#: How much of a swallowed line's text is quoted to identify it.
QUOTE_CHARS = 24

HEADER = "Playback of these ops, computed from line timings (approximate):"

WHOLE_VIDEO = "whole video so far"

SegTimes = Sequence[tuple[float | None, float | None]]


@dataclass(frozen=True)
class Numbering:
    """One segment's source line numbers translated to the numbers the model
    reads (:mod:`nagare_clip.director.display`).

    Ops stay in SOURCE coordinates everywhere — that is what ``_director.json``
    holds and what every later stage applies — so the translation happens here,
    at the last moment, on the way out.  Empty (:data:`IDENTITY`) is the
    per-segment view, where the two numberings are the same one.

    A silence has a display number of its own; in source coordinates it has
    none, which is why ``"n~"`` exists, so the two tables are separate.
    """

    speech: dict[int, int] = field(default_factory=dict)
    silence: dict[int, int] = field(default_factory=dict)

    def of(self, line: int, *, silence: bool = False) -> int:
        return (self.silence if silence else self.speech).get(line, line)

    def numbered(self, line: int, silence: bool) -> bool:
        """Does that edge have a line of its own in this numbering?"""
        return not silence or line in self.silence


#: The per-segment view: a source line number is the number shown.
IDENTITY = Numbering()


def numbering_for(view: DisplayView, segment: int) -> Numbering:
    """The display numbers of one segment's lines."""
    speech: dict[int, int] = {}
    silence: dict[int, int] = {}
    for line in view.lines:
        if line.segment == segment:
            (silence if line.is_silence else speech)[line.source_line] = line.number
    return Numbering(speech, silence)


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


def _lines_phrase(lines: list[int], num: Numbering = IDENTITY) -> str:
    """*lines* (source numbers) named as the reader numbers them.

    A contiguous source range is named by its two ends, so the silence lines
    the display numbering interleaves are inside the range rather than holes in
    it — which is what the op does to them too.
    """
    if len(lines) == 1:
        return f"line {num.of(lines[0])}"
    if lines == list(range(lines[0], lines[-1] + 1)):
        return f"lines {num.of(lines[0])}-{num.of(lines[-1])}"
    return "lines " + ", ".join(str(num.of(n)) for n in lines)


def _quote(text: str) -> str:
    text = text.replace("\n", " ").strip()
    return f"「{text[:QUOTE_CHARS]}…」" if len(text) > QUOTE_CHARS else f"「{text}」"


def _op_range(op: DirectorOp, num: Numbering) -> str:
    """An op's range as the reader numbers it: a silence edge is its own line."""
    first = num.of(op.lines[0], silence=op.gap_start)
    last = num.of(op.lines[1], silence=op.gap_end)
    return f"[{first},{last}]"


def _op_title(op: DirectorOp, num: Numbering = IDENTITY) -> str:
    head = f"{op.type} {_op_range(op, num)}"
    if op.type in ("timelapse", "speed") and op.factor is not None:
        head += f" x{op.factor}"
    if op.text and op.type in ("timelapse", "overlay"):
        head += " 「" + op.text.replace("\n", "\\n") + "」"
    return head


def _op_ref(op: DirectorOp, index: int, num: Numbering = IDENTITY) -> str:
    return f"{op.type} {_op_range(op, num)} (op {index + 1})"


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
        # A "n~" op is applied as a TIME range, so what it holds is not its
        # whole line range: with gap_start the first line's speech is outside
        # it, and the silence after that line — which no line map can express
        # — is inside.  Those gaps are tracked separately.
        self.gap_keep: dict[int, tuple[int, int]] = {}
        self.gap_speed: dict[int, tuple[float, tuple[int, int]]] = {}
        for i, per_op in enumerate(placements):
            for j, p in enumerate(per_op):
                if p.lines is None:
                    continue
                if is_time_resolved(p.op):
                    lines, gaps = covered(p.op)
                    if p.kind == "keep":
                        self.keep.update({n: (i, j) for n in lines})
                        self.gap_keep.update({n: (i, j) for n in gaps})
                    elif p.kind == "speed":
                        factor = p.op.factor or 1.0
                        self.speed.update({n: (factor, (i, j)) for n in lines})
                        self.gap_speed.update({n: (factor, (i, j)) for n in gaps})
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
        if n in self.gap_keep:
            return True
        return (
            n in self.keep
            and n + 1 in self.keep
            and self.keep[n] == self.keep[n + 1]
            and n not in self.cut
            and n + 1 not in self.cut
        )

    def gap_factor(self, n: int) -> float:
        if n in self.gap_speed:
            return self.gap_speed[n][0]
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


def covered(op: DirectorOp) -> tuple[list[int], list[int]]:
    """A time-resolved op's (speech lines, gaps-after-line) — see :class:`_Playback`.

    ``["53~","53~"]`` holds no speech at all and one gap; ``[53,"53~"]`` holds
    line 53 and the gap after it; ``["53~",55]`` holds lines 54-55 and the gaps
    after 53 and 54.
    """
    first, last = op.lines
    lines = list(range(first + 1 if op.gap_start else first, last + 1))
    gaps = [n for n in range(first, last)]
    if op.gap_start and first not in gaps:
        gaps.append(first)
    if op.gap_end:
        gaps.append(last)
    return lines, sorted(set(gaps))


def _fate(pb: _Playback, n: int, ops: list[DirectorOp], num: Numbering = IDENTITY) -> str:
    """What happens to the gap after line *n*: dropped, or which op keeps it."""
    if pb.gap_plays(n):
        i, _ = pb.gap_keep[n] if n in pb.gap_keep else pb.keep[n]
        return f"covered by {_op_ref(ops[i], i, num)}"
    return "dropped"


def _silence_render(silence: SilenceLine, num: Numbering) -> str:
    """One silence as the transcript the model reads shows it.

    With a display numbering the wait is a numbered line of its own, so it is
    quoted with that number; a wait too short to have earned a line (or the
    per-segment view, where no silence has a number) still names the line it
    follows.
    """
    number = num.silence.get(silence.after_line)
    if number is None:
        return silence.render(num.of(silence.after_line))
    return f"{INDENT}{number}: {silence_body(silence.duration, silence.descriptions)}"


def _silence_after(
    pb: _Playback,
    n: int,
    silence_lines: Sequence[SilenceLine],
    anchored: list[tuple[int, Gap]] | None,
) -> SilenceLine:
    """The silence after line *n*, as the transcript shows it.

    The transcript's own line when there is one — so the preview quotes the
    same seconds and the same description, and the director can tie the two
    together (and address it as ``"n~"``).  A wait under
    ``director.silence_line_min`` has no line of its own; it is still outside
    somebody's op, so one is built from the segment times, with any gap
    description that falls inside it.
    """
    for line in silence_lines:
        if line.after_line == n:
            return line
    start, end = pb.end(n), pb.start(n + 1)
    descriptions = tuple(
        gap.description
        for _anchor, gap in anchored or []
        if start <= (gap.start + gap.end) / 2 <= end
    )
    return SilenceLine(n, start, end, descriptions)


def _clip_note(
    pb_placements: list[list[Placement]],
    ops: list[DirectorOp],
    own: int,
    p: Placement,
    label: str,
    num: Numbering = IDENTITY,
) -> list[str]:
    """Why *p* landed on fewer lines than sent (empty when it did not clip)."""
    sent = p.op.lines
    if p.lines is None:
        return [f"  {label}not applied: {p.reason}"]
    if p.kind == "overlay":
        if p.lines[0] == sent[0]:
            return []
        lost = [sent[0]]
        verb = f"moved to line {num.of(p.lines[0])}"
    else:
        if p.lines == tuple(sent):
            return []
        lost = [n for n in range(sent[0], sent[1] + 1) if not p.lines[0] <= n <= p.lines[1]]
        verb = f"clipped to [{num.of(p.lines[0])},{num.of(p.lines[1])}]"
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
        reasons.append(f"{_lines_phrase(lines, num)} {verb_be} under {_op_ref(ops[k], k, num)}")
    if unexplained:
        verb_be = "is" if len(unexplained) == 1 else "are"
        reasons.append(f"{_lines_phrase(unexplained, num)} {verb_be} not free in the transcript")
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
    silence_lines: Sequence[SilenceLine] | None = None,
    first_line: int = 1,
    drops: Sequence[str] = (),
    label: str = "segment",
    elsewhere_seconds: float | None = None,
    numbering: Numbering = IDENTITY,
) -> SegmentPreview:
    """The playback facts for one segment's *ops*, as the director holds them.

    Takes what :func:`~.director_llm.generate_director_ops` holds: the raw
    segment *edit_lines*, *seg_times*/*silences* for those lines,
    *anchored_gaps* with segment-relative anchors, the *silence_lines* the
    transcript shows, the segment's *first_line*, and the parsed *ops* plus the
    parser's *drops*.  *elsewhere_seconds* (the rest of the finished video's
    runtime) adds a whole-video estimate.

    *numbering* (:func:`numbering_for`) renames every line number printed here
    into the whole video's display numbering — the numbers the model's ops
    arrive in.  The ops themselves stay in source coordinates; only what is
    SAID about them changes, so nothing downstream sees the display numbers.
    """
    num = numbering
    silence_lines = list(silence_lines or [])
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
                        f"  on screen together with the caption of {_op_ref(ops[k2], k2, num)} "
                        f"for {_s(overlap)}"
                    )
        return out

    def silence_note(n: int) -> list[str]:
        """The silence after line *n*, named and priced as the transcript does."""
        silence = _silence_after(pb, n, silence_lines, anchored_gaps)
        number = num.silence.get(n)
        # Under the display numbering the silence is a line the model can name;
        # without one (a wait under director.silence_line_min, or the
        # per-segment view) it is still only "the silence after line n".
        head = f"line {number}" if number is not None else f"the silence after line {num.of(n)}"
        return [
            f"  {head} is outside this op — {_fate(pb, n, ops, num)}",
            _silence_render(silence, num),
        ]

    def boundary_lines(keep: Placement | None) -> list[str]:
        """The silences just outside a span op — the reason this module exists.

        Only reached for an op WITHOUT a silence edge: one that has an edge is
        reported by :func:`resolved_body`, where the neighbouring silence is
        inside the op rather than outside it.
        """
        rng = _range(keep)
        if not rng:
            return []
        a, b = rng[0], rng[-1]
        out = []
        if a > first_line and gap_shown(pb.gap_after(a - 1)):
            out.extend(silence_note(a - 1))
        if b < pb.last and gap_shown(pb.gap_after(b)):
            out.extend(silence_note(b))
        return out

    def unintelligible_lines(speed: Placement | None) -> list[str]:
        if speed is None or speed.lines is None:
            return []
        return unintelligible_over(_range(speed), speed.op.factor or 1.0)

    def unintelligible_over(span: list[int], factor: float) -> list[str]:
        if factor < UNINTELLIGIBLE_FACTOR:
            return []
        lines = [n for n in span if n not in pb.cut]
        if not lines:
            return []
        total = sum(pb.figure(n) for n in lines)
        if len(lines) == 1:
            n = lines[0]
            return [
                f"  speech at x{factor} — unintelligible: line {num.of(n)} {_s(pb.figure(n))} "
                f"{_quote(text_of(n))} (total {_s(total)})"
            ]
        out = [
            f"  speech at x{factor} — unintelligible (total {_s(total)} over {len(lines)} lines):"
        ]
        out.extend(f"    {num.of(n)} {_s(pb.figure(n))} {_quote(text_of(n))}" for n in lines)
        return out

    def plays_line(lines: list[int], caption: float | None) -> list[str]:
        footage, onscreen, gaps = pb.played(lines, "figure")
        default = sum(pb.figure(n) for n in lines)
        line = (
            f"  plays {_s(footage)} of footage in {_s(onscreen)} "
            f"(default for {_lines_phrase(lines, num)}: {_s(default)})"
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

    def resolved_bounds(op: DirectorOp) -> tuple[float, float] | None:
        """A ``"n~"`` op's real time range: its silence edges are times, not lines."""
        first, last = op.lines
        if not (first_line <= first <= pb.last and first_line <= last <= pb.last):
            return None
        if (op.gap_start and first >= pb.last) or (op.gap_end and last >= pb.last):
            return None  # no following line, so no silence to end at
        start = (
            _silence_after(pb, first, silence_lines, anchored_gaps).start
            if op.gap_start
            else pb.start(first)
        )
        end = (
            _silence_after(pb, last, silence_lines, anchored_gaps).end
            if op.gap_end
            else pb.end(last)
        )
        return (start, end) if end > start else None

    def covers_phrase(op: DirectorOp) -> str:
        first, last = op.lines
        if num.numbered(first, op.gap_start) and num.numbered(last, op.gap_end):
            # Every edge has a line of its own here, so the range names itself.
            a = num.of(first, silence=op.gap_start)
            b = num.of(last, silence=op.gap_end)
            return _lines_phrase(list(range(a, b + 1)))
        if op.gap_start and op.gap_end and first == last:
            return f"the silence after line {num.of(first)}"
        head = (
            f"the silence after line {num.of(first)}"
            if op.gap_start
            else _lines_phrase(
                list(range(first, last + 1)) if not op.gap_end or first != last else [first],
                num,
            )
        )
        if not op.gap_end:
            return f"{head} through line {num.of(last)}" if op.gap_start else head
        tail = "the silence after it" if first == last else f"the silence after line {num.of(last)}"
        return f"{head} and {tail}"

    def resolved_body(op: DirectorOp) -> list[str]:
        """A ``"n~"`` op plays a TIME range; its lines are only where it hangs."""
        bounds = resolved_bounds(op)
        if bounds is None:
            return ["  this op's silence is outside the segment — it cannot be applied"]
        start, end = bounds
        footage = end - start
        factor = op.factor or 1.0 if op.type in ("speed", "timelapse") else 1.0
        speech_lines, gaps = covered(op)
        onscreen = footage / factor
        default = sum(pb.figure(n) for n in speech_lines)
        cost = (
            f"default for {_lines_phrase(speech_lines, num)}: {_s(default)}"
            if speech_lines
            else "dropped by default"
        )
        out = [
            f"  covers {covers_phrase(op)} ({start:.1f}-{end:.1f} s)",
            f"  plays {_s(footage)} of footage in {_s(onscreen)} ({cost})"
            + (f"; caption on screen {_s(onscreen)}" if op.text else ""),
        ]
        out.extend(unintelligible_over(speech_lines, factor))
        for n in gaps:
            # A wait of no length is not a wait: every line of a talking
            # stretch has a gap after it, and printing "[silent 0.0s]" for
            # each is noise the director reads past on every turn.
            if not gap_shown(pb.gap_after(n)):
                continue
            if n in pb.gap_keep or n in pb.gap_speed:
                out.append(
                    _silence_render(_silence_after(pb, n, silence_lines, anchored_gaps), num)
                )
        return out

    blocks: list[str] = []
    for i, op in enumerate(ops):
        body: list[str] = []
        per = {p.kind: p for p in placements[i]}
        if is_time_resolved(op):
            body.extend(resolved_body(op))
        elif op.type == "edit":
            body.append("  no runtime change (a text edit within the line)")
        elif op.type == "overlay":
            p = per["overlay"]
            body.extend(_clip_note(placements, ops, i, p, "", num))
            if p.lines is not None:
                body.append(
                    f"  no runtime change; caption on screen {_s(op.duration or 0.0)} "
                    f"from line {num.of(p.lines[0])}"
                )
                body.extend(caption_lines(i))
        elif op.type == "cut":
            p = per["cut"]
            body.extend(_clip_note(placements, ops, i, p, "", num))
            lines = _range(p)
            if lines:
                removed = sum(pb.figure(n) for n in lines)
                body.append(f"  removes {_lines_phrase(lines, num)}: {_s(removed)}")
        elif op.type in ("keep", "speed"):
            p = per[op.type]
            body.extend(_clip_note(placements, ops, i, p, "", num))
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
                body.extend(_clip_note(placements, ops, i, keep, "", num))
            else:
                if keep:
                    body.extend(_clip_note(placements, ops, i, keep, "its keep: ", num))
                if speed:
                    body.extend(_clip_note(placements, ops, i, speed, "its speed-up: ", num))
            if cap:
                body.extend(_clip_note(placements, ops, i, cap, "its caption: ", num))
            lines = sorted(set(_range(keep)) | set(_range(speed)))
            if lines:
                caption = cap.op.duration if cap and cap.lines else None
                body.extend(plays_line(lines, caption))
                body.extend(unintelligible_lines(speed))
                body.extend(boundary_lines(keep))
                if caption is not None:
                    body.extend(caption_lines(i))
        blocks.append("\n".join([_op_title(op, num)] + body))

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


def segment_preview(
    view: DisplayView,
    transcripts: Sequence[SegmentTranscript],
    index: int,
    ops: Sequence[DirectorOp],
) -> SegmentPreview:
    """One segment of the finished video, priced under the display numbering."""
    t = transcripts[index - 1]
    return preview_segment(
        t.edit_lines,
        list(ops),
        seg_times=t.seg_times,
        silences=t.silences,
        anchored_gaps=t.gaps,
        silence_lines=t.silence_lines,
        first_line=t.first_line,
        label=f"segment [{index}] {view.segments[index - 1].label}",
        numbering=numbering_for(view, index),
    )


def preview_turn(
    view: DisplayView,
    transcripts: Sequence[SegmentTranscript],
    ops: Mapping[int, Sequence[DirectorOp]],
    *,
    segments: Sequence[int] = (),
    drops: Sequence[str] = (),
) -> str:
    """The answer to one turn of the conversation.

    *ops* are every op accepted so far, keyed by the segment's 1-based index in
    the playback order; *segments* are the ones this turn touched, and only
    those are printed — a segment is priced with its WHOLE op list, because a
    new cut is clipped by a keep an earlier turn placed, but the turns that
    reviewed other footage do not get replayed on every message.

    The footer is the whole video, not this stretch of it: every segment with
    accepted ops at its edited runtime and every other at its default, so the
    running total answers "how long is this video now" rather than "how long is
    the part I just looked at".
    """
    previews = {
        index: segment_preview(view, transcripts, index, ops.get(index, ()))
        for index in sorted(set(ops) | set(segments))
    }
    blocks = [previews[index].text for index in sorted(set(segments))]
    default = _total(t.default_runtime() for t in transcripts)
    runtime = _total(
        previews[i].runtime_seconds if i in previews else t.default_runtime()
        for i, t in enumerate(transcripts, start=1)
    )
    footer = [f"dropped by the parser (no effect): {d}" for d in drops]
    if default is None or runtime is None:
        footer.append(f"{WHOLE_VIDEO}: not computable (a segment has no line timings)")
    else:
        footer.append(
            f"{WHOLE_VIDEO}: default {_s(default)} ({_m(default)}) → with the ops "
            f"accepted so far {_s(runtime)} ({_m(runtime)})"
        )
    return "\n\n".join([*blocks, "\n".join(footer)])


def _total(values: Iterable[float | None]) -> float | None:
    """The sum, or ``None`` as soon as one part is unknown."""
    out = 0.0
    for value in values:
        if value is None:
            return None
        out += value
    return out


def _m(sec: float) -> str:
    return f"{sec / 60:.1f} min"
