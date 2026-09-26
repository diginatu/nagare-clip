"""The ``_edits.txt`` line contract: speech lines, and the silences between them.

``text_filter`` writes one line per WhisperX segment.  ``guided_edit`` adds a
**silence line** after speech line ``n`` wherever the director was shown the
wait after ``n`` as a line of its own, in the very text the director read::

    その状態で
    <keep><speed factor="8.0">[silent 29.9s: a hand enters from the right]</speed></keep>
    この状態で今予備水持ってきたんで

so an op on ``"n~"`` becomes an ordinary marker, and ``_edits.txt`` stays the
one record of every edit.  A silence line's identity is its POSITION — which
speech lines it sits between; the seconds and the description are for humans
and nothing here reads them.

The bracket body is opaque: it runs from ``[silent `` to the first UNescaped
``]`` (:func:`silence_body` escapes ``\\`` and ``]`` inside descriptions), and
markers are recognised only outside it, so a description mentioning
``<keep>`` is description text.

This is the one renderer of that bracket (:func:`silence_body`, which the
director's view prints too) and the one parser of the file
(:func:`parse_edit_lines`).  Every reader of an ``_edits.txt`` goes through it:
a reader that assumed "line i == segment i" would shift every line after the
first silence.  A file with no silence lines — everything ``text_filter``
writes, and every ``guided_edit`` output from before this contract — parses to
exactly that assumption.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from nagare_clip.intervals.speech import line_speech_spans

#: Default for ``director.silence_line_min``: the shortest wait shown as its
#: own line.  Matches ``gap_context.min_gap``, so every described gap has a
#: silence line to land in.
DEFAULT_SILENCE_LINE_MIN = 5.0

#: Joins several descriptions anchored in one silence.
JOIN = " / "

#: Every valid marker tag, in the forms the intervals extractors accept.
MARKER_RE = re.compile(
    r"<keep>|</keep>"
    r'|<speed\s+factor="[0-9.]+">|</speed>'
    r'|<overlay\s+text="[^"]*"\s+duration="[0-9.]+"\s*/>'
    r"|<cut>|</cut>"
)

_BODY_OPEN = "[silent "
_BODY_RE = re.compile(r"\[silent [0-9]+(?:\.[0-9]+)?s(?:: .*)?\]", re.S)
_OPENERS = ("<keep>", "<speed", "<cut>")


def _escape(text: str) -> str:
    """``\\`` and ``]`` escaped, as ``escape_overlay_text`` escapes its own."""
    return text.replace("\\", "\\\\").replace("]", "\\]")


def silence_body(
    seconds: float, descriptions: Sequence[str] = (), after_line: int | None = None
) -> str:
    """``[silent 29.9s: …]`` — the bracket every view of a silence prints.

    The one formatter: the director's transcript lines, its numbered display
    line, the playback preview and ``_edits.txt`` all render a silence through
    it, so its seconds and its descriptions cannot drift between them.
    *after_line* is included only where the silence has no number of its own
    to be addressed by.  Descriptions are escaped so the bracket's end is the
    first unescaped ``]``.
    """
    body = f"silent {seconds:.1f}s"
    if after_line is not None:
        body += f" after line {after_line}"
    if descriptions:
        body += ": " + JOIN.join(_escape(d) for d in descriptions)
    return f"[{body}]"


def silence_line_min(director_cfg: dict) -> float:
    """Read ``director.silence_line_min`` defensively (invalid = the default).

    ``0`` is honoured as "every between-line silence gets a line"; a negative
    or non-numeric value is a broken config, not an instruction.
    """
    raw = director_cfg.get("silence_line_min", DEFAULT_SILENCE_LINE_MIN)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        return DEFAULT_SILENCE_LINE_MIN
    return float(raw)


def gap_spans(whisperx_data: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """``{line: (start, end)}`` for the silence after every line that has one.

    Between :func:`~nagare_clip.intervals.speech.line_speech_spans` — the very
    silence ``run_intervals`` drops — at any length: the threshold is
    :func:`expected_silences`' decision.
    """
    spans = line_speech_spans(whisperx_data)
    out: dict[int, tuple[float, float]] = {}
    for i in range(len(spans) - 1):
        if not spans[i] or not spans[i + 1]:
            continue
        start, end = spans[i][-1][1], spans[i + 1][0][0]
        if end > start:
            out[i + 1] = (start, end)
    return out


def expected_silences(whisperx_data: dict[str, Any], min_seconds: float) -> set[int]:
    """The ``n`` whose silence gets a line: every gap at least *min_seconds* long."""
    return {n for n, (s, e) in gap_spans(whisperx_data).items() if e - s >= min_seconds}


def insert_silence_lines(speech_lines: Sequence[str], bodies: Mapping[int, str]) -> list[str]:
    """*speech_lines* with ``bodies[n]`` written after speech line ``n``.

    A body after the last line is not written: no silence follows the end.
    """
    out: list[str] = []
    last = len(speech_lines)
    for n, line in enumerate(speech_lines, start=1):
        out.append(line)
        if n < last and n in bodies:
            out.append(bodies[n])
    return out


def _skip_markers(line: str, pos: int) -> int:
    """Index of the first character at/after *pos* that is not a marker or space."""
    while True:
        while pos < len(line) and line[pos].isspace():
            pos += 1
        m = MARKER_RE.match(line, pos)
        if m is None:
            return pos
        pos = m.end()


def _body_end(line: str, pos: int) -> int | None:
    """Index just past the first unescaped ``]`` at/after *pos*."""
    while pos < len(line):
        ch = line[pos]
        if ch == "\\":
            pos += 2
            continue
        if ch == "]":
            return pos + 1
        pos += 1
    return None


def split_silence_line(line: str) -> tuple[str, str, str] | None:
    """``(markers before, bracket body, markers after)``, or ``None`` for speech.

    A silence line is nothing but valid marker tags around one
    ``[silent Ns…]`` body.  Any other text outside the body — a word, a
    ``{{old->new}}`` patch — makes it a speech line.
    """
    start = _skip_markers(line, 0)
    if not line.startswith(_BODY_OPEN, start):
        return None
    end = _body_end(line, start + len(_BODY_OPEN))
    if end is None:
        return None
    body = line[start:end]
    if _BODY_RE.fullmatch(body) is None:
        return None
    if _skip_markers(line, end) != len(line):
        return None
    return line[:start].strip(), body, line[end:].strip()


@dataclass(frozen=True)
class Slot:
    """One physical line of ``_edits.txt``.

    *speech_line* is a speech line's own 1-based number (= its WhisperX
    segment) or, for a silence line, the ``n`` it follows (the ``n`` of
    ``"n~"``; ``0`` = before the first speech line, which is invalid).
    """

    file_line: int
    kind: Literal["speech", "silence"]
    speech_line: int
    text: str
    before: str = ""
    body: str = ""
    after: str = ""

    @property
    def is_silence(self) -> bool:
        return self.kind == "silence"

    @property
    def markers(self) -> str:
        """What a marker scanner may read: the whole speech line, or only the
        tags around a silence line's opaque body."""
        return self.before + self.after if self.is_silence else self.text


def _tags(text: str) -> list[str]:
    return MARKER_RE.findall(text)


def _is_opener(tag: str) -> bool:
    return tag.startswith(_OPENERS)


def _tag_name(tag: str) -> str:
    return tag.strip("</>").split()[0].rstrip("/")


@dataclass(frozen=True)
class EditFile:
    """A parsed ``_edits.txt``: every physical line, speech and silence."""

    slots: list[Slot] = field(default_factory=list)

    def speech_lines(self) -> list[str]:
        """The raw speech lines, one per WhisperX segment."""
        return [s.text for s in self.slots if not s.is_silence]

    def silences(self) -> list[Slot]:
        return [s for s in self.slots if s.is_silence]

    def marker_text(self) -> list[str]:
        """Per physical line, the text a marker scanner may read."""
        return [s.markers for s in self.slots]

    def file_index(self, line: int, gap: bool) -> int | None:
        """The 1-based physical line of speech line *line* (or its ``"line~"``)."""
        for s in self.slots:
            if s.speech_line == line and s.is_silence == gap:
                return s.file_line
        return None

    def speech_line_of(self, file_line: int) -> tuple[int, bool] | None:
        """``(n, is_silence)`` for a physical line — :meth:`file_index` inverted."""
        if not 1 <= file_line <= len(self.slots):
            return None
        s = self.slots[file_line - 1]
        return (s.speech_line, s.is_silence)

    def speech_projection(self) -> list[str]:
        """One line per segment, a silence line's markers folded onto its neighbours.

        An opener moves to the start of the next speech line and a closer to the
        end of the previous one; a pair opened and closed on the same silence
        line covers no words and is dropped.  This is the view for WORD
        operations only — text sync and ``<cut>`` deletion — where a cut from
        the silence after 53 must not delete line 53.  Times on a silence line
        are resolved from the slots, never from this projection.
        """
        out: list[str] = []
        pending = ""
        for s in self.slots:
            if not s.is_silence:
                out.append(pending + s.text)
                pending = ""
                continue
            opened: list[str] = []
            for tag in _tags(s.markers):
                if tag.startswith("<overlay"):
                    continue
                if _is_opener(tag):
                    opened.append(tag)
                    continue
                name = _tag_name(tag)
                match = next(
                    (i for i in range(len(opened) - 1, -1, -1) if _tag_name(opened[i]) == name),
                    None,
                )
                if match is not None:
                    del opened[match]
                elif out:
                    out[-1] += tag
            pending += "".join(opened)
        if pending and out:
            out[-1] += pending
        return out


def parse_edit_lines(lines: Sequence[str]) -> EditFile:
    """Split ``_edits.txt`` lines into speech lines and silence positions.

    Never raises: a silence line in the wrong place is still parsed as one, and
    :func:`silence_problems` names it.
    """
    slots: list[Slot] = []
    speech = 0
    for i, line in enumerate(lines, start=1):
        parts = split_silence_line(line)
        if parts is None:
            speech += 1
            slots.append(Slot(i, "speech", speech, line))
        else:
            before, body, after = parts
            slots.append(Slot(i, "silence", speech, line, before, body, after))
    return EditFile(slots)


def silence_problems(
    parsed: EditFile, expected: set[int], min_seconds: float | None = None
) -> list[tuple[int, str]]:
    """``(physical line, message)`` for every silence line out of place.

    Silence lines are generated by guided_edit, never hand-added: only markers
    may change on or around one.  A file with no silence line at all is the
    legacy shape and is not checked.  The seconds and the description are not
    compared — nothing reads them.
    """
    silences = parsed.silences()
    if not silences:
        return []
    threshold = "" if min_seconds is None else f" ≥ {min_seconds:.1f}s"
    problems: list[tuple[int, str]] = []
    seen: set[int] = set()
    for s in silences:
        n = s.speech_line
        if n == 0:
            problems.append((s.file_line, "silence line before the first speech line"))
        elif n in seen:
            problems.append((s.file_line, f"duplicate silence line after speech line {n}"))
        elif n not in expected:
            problems.append(
                (
                    s.file_line,
                    f"unexpected silence line after speech line {n} "
                    f"(no silence{threshold} there; silence lines are generated by guided_edit)",
                )
            )
        seen.add(n)
    for n in sorted(expected - seen):
        speech_at = parsed.file_index(n, False)
        if speech_at is None:
            continue
        where = speech_at + 1
        nxt = parsed.slots[where - 1] if where <= len(parsed.slots) else None
        if nxt is not None and not nxt.is_silence and _BODY_OPEN in nxt.text:
            problems.append(
                (
                    where,
                    f"the silence line after speech line {n} may carry only markers "
                    "(text or a {{old->new}} patch outside its [silent …] bracket)",
                )
            )
            continue
        problems.append(
            (
                where,
                f"missing silence line after speech line {n} (silence lines are "
                "generated by guided_edit; restore it — only markers may change around it)",
            )
        )
    return sorted(problems)
