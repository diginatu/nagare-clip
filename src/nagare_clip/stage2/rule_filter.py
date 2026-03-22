"""Stage 2 rule-based filters applied before the AI filter."""

from __future__ import annotations

_CLOSING_PHRASE = "ご視聴ありがとうございました"


def remove_midstream_closing(lines: list[str]) -> list[str]:
    """Mark closing phrases with ``{{phrase->}}`` unless at the last non-empty line.

    WhisperX sometimes hallucinates "ご視聴ありがとうございました" in the middle
    of a transcript. This filter marks those occurrences for removal using
    the ``{{old->new}}`` patch format while keeping a genuine closing at the end.
    """
    # Find the last non-empty line index
    last_nonempty = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_nonempty = i
            break

    if last_nonempty < 0:
        return lines

    return [
        line.replace(_CLOSING_PHRASE, "{{" + _CLOSING_PHRASE + "->}}")
        if i < last_nonempty and _CLOSING_PHRASE in line
        else line
        for i, line in enumerate(lines)
    ]
