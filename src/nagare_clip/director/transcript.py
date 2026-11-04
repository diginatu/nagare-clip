"""The transcript as the director reads it: clean text, numbered, timed.

Shared by the director's view (:mod:`~nagare_clip.director.display`), its
preview and the ``summary`` stage, which numbers transcripts the same way.
"""

from __future__ import annotations

from collections.abc import Sequence

from nagare_clip.intervals.sync_json import (
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)
from nagare_clip.text_filter.llm_filter import apply_patches_to_lines
from nagare_clip.timing import bracket_seconds, format_dur_gap


def clean_for_display(edit_lines: list[str]) -> list[str]:
    """Render edit lines as plain readable text for the director.

    Applies ``{{old->new}}`` patches and strips any marker tags so the LLM
    sees clean prose, not edit syntax.
    """
    stripped = [
        CUT_TAG_RE.sub(
            "",
            OVERLAY_TAG_RE.sub("", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))),
        )
        for line in edit_lines
    ]
    return apply_patches_to_lines(stripped)


def format_numbered_transcript(clean_lines: list[str], first_line: int = 1) -> str:
    """Format clean lines as ``N: text``, matching the filter LLM.

    *first_line* is where this slice starts in the source's own numbering, so a
    segment beginning at line 31 presents its first line as ``31:``.
    """
    return "\n".join(f"{i + first_line}: {text}" for i, text in enumerate(clean_lines))


def speech_seconds(
    seg_times: Sequence[tuple[float | None, float | None]],
    silences: Sequence[float] | None = None,
) -> list[float | None]:
    """Seconds each line plays with no op at all: its span minus what the
    intervals stage drops inside it (``None`` when the line is untimed).

    *silences* come from :func:`nagare_clip.director.load.source_drops`, the
    intervals stage's own keep chain, so this IS the render's 1x figure.

    The one definition of a line's playing time — the transcript brackets and
    the whole-video "default runtime" both read it, so they cannot drift.
    """
    out: list[float | None] = []
    for i, (start, end) in enumerate(seg_times):
        raw = end - start if start is not None and end is not None else None
        sil = silences[i] if silences is not None and i < len(silences) else None
        out.append(max(raw - sil, 0.0) if raw is not None and sil else raw)
    return out


def line_seconds(
    seg_times: Sequence[tuple[float | None, float | None]],
    silences: Sequence[float] | None = None,
) -> list[float | None]:
    """Each line's duration exactly as its transcript bracket prints it.

    Differs from :func:`speech_seconds` only where a sub-second silence is
    folded back in for display.  Per-line figures quoted back to the director
    (the playback preview) read this, so a line never carries two numbers;
    runtimes keep summing :func:`speech_seconds`, because the intervals stage
    drops that silence regardless of what the bracket shows.
    """
    out: list[float | None] = []
    for i, dur in enumerate(speech_seconds(seg_times, silences)):
        sil = silences[i] if silences is not None and i < len(silences) else None
        out.append(None if dur is None else bracket_seconds(dur, sil))
    return out


def format_numbered_transcript_timed(
    clean_lines: list[str],
    seg_times: list[tuple[float | None, float | None]],
    silences: list[float] | None = None,
    first_line: int = 1,
    silence_after: set[int] | None = None,
) -> str:
    """``N: text  [dur, gap]`` (1-based), gap = time to the next line.

    Per line: ``dur = end - start``; ``gap = next.start - this.end`` (the last
    line has no gap).  When *silences* is given (the seconds of each line's
    span the intervals stage drops, from
    :func:`nagare_clip.director.load.source_drops`),
    a line with significant internal silence renders
    ``[12.9s speech, 62.9s silence]`` — speech-only duration — so the LLM
    never judges pacing from span time that is mostly already-dropped silence.
    A missing ``start``/``end`` degrades that line's bracket via
    :func:`format_dur_gap` (possibly to no bracket at all).

    *silence_after* are the line numbers a silence line follows: that wait is
    stated there, in seconds of its own, so printing it here too would give one
    silence two numbers the director has to reconcile.
    """
    out: list[str] = []
    speech = speech_seconds(seg_times, silences)
    for i, text in enumerate(clean_lines):
        _, end = seg_times[i]
        sil = silences[i] if silences is not None and i < len(silences) else None
        dur = speech[i]
        gap: float | None = None
        if silence_after and i + first_line in silence_after:
            pass
        elif i + 1 < len(clean_lines):
            nxt_start = seg_times[i + 1][0]
            if end is not None and nxt_start is not None:
                gap = nxt_start - end
        bracket = format_dur_gap(dur, gap, sil)
        out.append(f"{i + first_line}: {text}  {bracket}".rstrip())
    return "\n".join(out)
