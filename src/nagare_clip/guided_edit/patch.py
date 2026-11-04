"""The deterministic ``edit`` op: one exact ``{{old->new}}`` patch on one line.

The director (an agent reading ``view``) states the change exactly — the text
as it reads it and its replacement — so applying it needs no model: find
``old`` in the line's text *as the director sees it* (patches applied, marker
tags stripped, :func:`~nagare_clip.director.transcript.clean_for_display`) and
wrap the raw characters it came from as ``{{old->new}}``.

The match must come from plain text only: a span that would cut through a
marker tag or an existing patch is refused rather than guessed at, because
the patch would then either break the tag or patch text that is not in the
file.  Touching one at the edge is fine.  :func:`edit_problem` is what the
``write`` tool reports; :func:`apply_edit` is what ``guided_edit`` splices.

Pure: no I/O.
"""

from __future__ import annotations

from nagare_clip.intervals.sync_json import (
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)
from nagare_clip.text_filter.llm_filter import PATCH_RE, apply_patches_to_lines

#: What ``old``/``new`` may not contain: the patch syntax itself, a marker's
#: angle brackets, or a line break (one ``_edits.txt`` line is one segment).
FORBIDDEN = ("{{", "}}", "->", "<", ">", "\n", "\r")

_TAG_RES = (KEEP_TAG_RE, SPEED_TAG_RE, OVERLAY_TAG_RE, CUT_TAG_RE)


def forbidden_problem(field: str, value: str) -> str | None:
    """Why *value* cannot go into a patch, or ``None``."""
    bad = [token for token in FORBIDDEN if token in value]
    if not bad:
        return None
    shown = ", ".join(repr(token) for token in bad)
    return f"edit op {field!r} contains {shown}; patch text may not hold patch/marker syntax or a line break"


def _clean_map(line: str) -> tuple[str, list[int | None]] | None:
    """The line as the director reads it, and each character's raw index.

    Text a patch contributes (its ``new`` side) maps to ``None``: it is not in
    the file, so no new patch may cover it.  ``None`` overall when the markup
    cannot be mapped (overlapping matches, or a result that differs from
    ``clean_for_display``'s) — the caller refuses rather than guesses.
    """
    spans: list[tuple[int, int, str]] = []
    for regex in _TAG_RES:
        spans.extend((m.start(), m.end(), "") for m in regex.finditer(line))
    spans.extend((m.start(), m.end(), m.group(2)) for m in PATCH_RE.finditer(line))
    spans.sort()
    clean: list[str] = []
    index: list[int | None] = []
    pos = 0
    for start, end, replacement in spans:
        if start < pos:
            return None
        for i in range(pos, start):
            clean.append(line[i])
            index.append(i)
        clean.extend(replacement)
        index.extend([None] * len(replacement))
        pos = end
    for i in range(pos, len(line)):
        clean.append(line[i])
        index.append(i)
    text = "".join(clean)
    stripped = line
    for regex in _TAG_RES:
        stripped = regex.sub("", stripped)
    if text != apply_patches_to_lines([stripped])[0]:
        return None
    return text, index


def _occurrences(text: str, old: str) -> list[int]:
    out, at = [], text.find(old)
    while at >= 0:
        out.append(at)
        at = text.find(old, at + 1)
    return out


def locate_edit(line: str, old: str) -> tuple[int, int] | str:
    """The raw ``[start, end)`` that *old* reads as, or why there is none."""
    if not old:
        return "edit op 'old' is empty; state the exact text to change"
    mapped = _clean_map(line)
    if mapped is None:
        return "the line's existing markers/patches could not be mapped; edit the file by hand"
    text, index = mapped
    found = _occurrences(text, old)
    if not found:
        return f"'old' {old!r} does not occur in the line as view shows it: {text!r}"
    if len(found) > 1:
        return (
            f"'old' {old!r} occurs {len(found)} times in the line; "
            "include more of the surrounding text so it occurs exactly once"
        )
    raw = index[found[0] : found[0] + len(old)]
    if any(i is None for i in raw) or any(b != a + 1 for a, b in zip(raw, raw[1:])):
        return (
            f"'old' {old!r} cuts through an existing marker or patch on the line; "
            "choose text outside it"
        )
    return raw[0], raw[-1] + 1


def edit_problem(line: str, old: str, new: str) -> str | None:
    """Why ``{{old->new}}`` cannot be applied to *line*, or ``None``."""
    for field, value in (("old", old), ("new", new)):
        reason = forbidden_problem(field, value)
        if reason:
            return reason
    located = locate_edit(line, old)
    return located if isinstance(located, str) else None


def apply_edit(line: str, old: str, new: str) -> str:
    """*line* with *old* wrapped as ``{{old->new}}``; ``ValueError`` if it cannot be."""
    reason = edit_problem(line, old, new)
    if reason:
        raise ValueError(reason)
    start, end = locate_edit(line, old)  # type: ignore[misc]
    return f"{line[:start]}{{{{{line[start:end]}->{new}}}}}{line[end:]}"


__all__ = ["FORBIDDEN", "apply_edit", "edit_problem", "forbidden_problem", "locate_edit"]
