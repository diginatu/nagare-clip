"""``_edits.txt`` integrity checker.

The intervals stage (``sync_text_to_json``) fails fast: it raises a ``ValueError``
on the *first* segment whose edit line cannot be decomposed against the original
WhisperX JSON, and the keep/speed/overlay extractors only emit log warnings
(without line numbers) when the intervals stage actually runs.  Editing
``_edits.txt`` by hand is therefore a fix-one-rerun-find-the-next slog.

This module validates an ``_edits.txt`` against its source JSON and returns
*every* problem at once, each tied to a 1-based line number, so the human can
correct them all in a single pass.  It never raises; it collects.

Checks performed (mirroring the real intervals-stage parsing in ``sync_json.py``
so the verdict matches what the intervals stage would do):

- speech-line count vs. number of JSON segments (speech lines map 1:1 to
  segments);
- silence lines (``guided_edit`` writes one after speech line ``n`` for every
  wait of at least ``director.silence_line_min``): missing, duplicated,
  unexpected or text-carrying ones are named by physical line.  Their bracket
  body is opaque — never scanned for tags or patches — and its seconds and
  description are not compared;
- ``{{old->new}}`` patch syntax (unbalanced braces, empty no-op ``{{->}}``);
- decomposition integrity (text changed outside markers, ``old`` side not
  matching the original, line not covering the whole segment);
- ``<keep>`` / ``<speed>`` / ``<cut>`` tag balance, speed factor > 0,
  ``<overlay .../>`` marker attributes (non-empty text, duration > 0), and
  malformed tags.

Empty ``new`` (``{{old->}}``, a deletion) is valid and is not flagged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

from nagare_clip.edit_lines import (
    DEFAULT_SILENCE_LINE_MIN,
    MARKER_RE,
    expected_silences,
    parse_edit_lines,
    silence_problems,
)
from nagare_clip.intervals.sync_json import (
    _OVERLAY_MARK_RE,
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
    sync_text_to_json,
)
from nagare_clip.text_filter.llm_filter import PATCH_RE, apply_patches_to_lines


class Problem(NamedTuple):
    """A single validation problem.

    ``line`` is the 1-based PHYSICAL line number in ``_edits.txt``, silence
    lines counted (``None`` for file-level problems such as a line-count
    mismatch).
    """

    line: int | None
    message: str


# Any recognised marker tag (valid openers/closers), in the same forms the
# intervals-stage extractors accept.  Used to tokenise a line for balance checking and,
# by subtraction, to spot malformed tags.
_ANY_TAG_RE = MARKER_RE
# A tag-like fragment that survives stripping the valid tags above → malformed.
_TAGLIKE_RE = re.compile(r"</?(?:keep|speed|overlay|cut)\b")

_SPEED_FACTOR_RE = re.compile(r'<speed\s+factor="([0-9.]+)">')


def _strip_tags(line: str) -> str:
    """Remove keep/speed/cut span tags and <overlay/> markers, leaving text + patches.

    `<cut>` tags are stripped leaving their wrapped text in place; the wrapped
    text must still match the original (the intervals stage desugars `<cut>` to a
    `{{wrapped->}}` deletion, whose ``old`` side must decompose against the
    original), so the standard keep-text decomposition check covers it.
    """
    stripped = OVERLAY_TAG_RE.sub("", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line)))
    return CUT_TAG_RE.sub("", stripped)


def _check_patch_syntax(cleaned_line: str) -> list[str]:
    """Return patch-syntax problem messages for a tag-stripped line."""
    messages: list[str] = []
    # Empty no-op patch: both sides empty.
    for m in PATCH_RE.finditer(cleaned_line):
        if m.group(1) == "" and m.group(2) == "":
            messages.append("empty {{->}} patch has no effect; remove it")
    # Strip valid patches; any leftover brace pair signals a malformed marker.
    leftover = PATCH_RE.sub("", cleaned_line)
    if "{{" in leftover or "}}" in leftover:
        messages.append("malformed {{old->new}} patch (unbalanced braces or missing '->')")
    return messages


def _diagnose_decomposition(edit_line: str, original_text: str) -> str | None:
    """Diagnose why ``edit_line`` does not decompose against ``original_text``.

    Mirrors ``sync_json._decompose_edit_line`` but returns a human-readable
    cause (or ``None`` when the line decomposes cleanly).
    """
    markers = list(PATCH_RE.finditer(edit_line))
    if not markers:
        return "segment text changed but no {{old->new}} marker was used"

    edit_pos = 0
    orig_pos = 0
    for m in markers:
        prefix = edit_line[edit_pos : m.start()]
        if prefix:
            orig_end = orig_pos + len(prefix)
            if original_text[orig_pos:orig_end] != prefix:
                return (
                    f"text {prefix!r} outside {{{{old->new}}}} markers does not "
                    f"match the original transcript"
                )
            orig_pos = orig_end
        old = m.group(1)
        orig_end = orig_pos + len(old)
        if original_text[orig_pos:orig_end] != old:
            return (
                f"the 'old' side {old!r} of a {{{{old->new}}}} patch does not "
                f"match the original transcript at this point"
            )
        orig_pos = orig_end
        edit_pos = m.end()

    trailing = edit_line[edit_pos:]
    if trailing:
        orig_end = orig_pos + len(trailing)
        if original_text[orig_pos:orig_end] != trailing:
            return (
                f"text {trailing!r} outside {{{{old->new}}}} markers does not "
                f"match the original transcript"
            )
        orig_pos = orig_end

    if orig_pos != len(original_text):
        return "edit line does not cover the full original segment text"
    return None


def _check_tags(edit_lines: list[tuple[int, str]]) -> list[Problem]:
    """Check keep/speed/cut tag balance, overlay markers, and well-formedness.

    Tracks one open state per *span* tag type (nesting of the same type is not
    allowed, matching the intervals-stage extractors).  ``<overlay .../>`` is a
    self-closing point marker with no balance to track — only its attributes
    are validated.  ``edit_lines`` are ``(physical line, scannable text)`` —
    a silence line contributes only the markers around its body — already
    sliced to the segment count, mirroring the extractors' stop at the first
    speech line past the JSON's segments.
    """
    problems: list[Problem] = []
    # tag name -> opening line number (None == not open)
    open_at: dict[str, int | None] = {
        "keep": None,
        "speed": None,
        "cut": None,
    }

    for lineno, line in edit_lines:
        for tok in _ANY_TAG_RE.finditer(line):
            text = tok.group()
            mark = _OVERLAY_MARK_RE.match(text)
            if mark is not None:
                if mark.group(1) == "":
                    problems.append(
                        Problem(lineno, '<overlay> has empty text=""; nothing to display')
                    )
                if float(mark.group(2)) <= 0:
                    problems.append(Problem(lineno, "<overlay> duration must be greater than 0"))
                continue
            if text.startswith("</"):
                name = text[2:-1]
                if open_at[name] is None:
                    problems.append(Problem(lineno, f"unmatched </{name}>"))
                else:
                    open_at[name] = None
            else:
                name = text[1:].split()[0].rstrip(">")
                if open_at[name] is not None:
                    problems.append(
                        Problem(lineno, f"nested <{name}> opener; close the previous one first")
                    )
                    continue
                open_at[name] = lineno
                if name == "speed":
                    fm = _SPEED_FACTOR_RE.match(text)
                    if fm is not None and float(fm.group(1)) <= 0:
                        problems.append(Problem(lineno, "<speed> factor must be greater than 0"))
        # Malformed tags: tag-like fragments left after removing valid tags.
        if _TAGLIKE_RE.search(_ANY_TAG_RE.sub("", line)):
            problems.append(Problem(lineno, "malformed <keep>/<speed>/<overlay>/<cut> tag"))

    for name, opened in open_at.items():
        if opened is not None:
            problems.append(Problem(opened, f"unclosed <{name}> (opened here, never closed)"))
    return problems


def check_edits(
    edit_lines: list[str],
    json_data: dict[str, Any],
    *,
    silence_line_min: float = DEFAULT_SILENCE_LINE_MIN,
) -> list[Problem]:
    """Validate ``edit_lines`` against ``json_data`` and return all problems.

    *silence_line_min* is ``director.silence_line_min``: the shortest wait
    guided_edit writes a silence line for, which decides where one belongs.
    """
    segments = json_data.get("segments", [])
    problems: list[Problem] = []
    parsed = parse_edit_lines(edit_lines)
    speech = [slot for slot in parsed.slots if not slot.is_silence]

    if len(speech) != len(segments):
        problems.append(
            Problem(
                None,
                f"edits file has {len(speech)} line(s) but JSON has "
                f"{len(segments)} segment(s); lines must map 1:1 to segments",
            )
        )

    problems.extend(
        Problem(line, message)
        for line, message in silence_problems(
            parsed, expected_silences(json_data, silence_line_min), silence_line_min
        )
    )

    # Only the lines up to the len(segments)-th speech line are consumed by
    # the intervals stage.
    in_range = [slot for slot in parsed.slots if slot.speech_line <= len(segments)]
    problems.extend(_check_tags([(slot.file_line, slot.markers) for slot in in_range]))

    # A line must never contain a newline. Read from a file it cannot; but an
    # in-memory list (guided_edit checks its result before writing) can, and
    # every other check here still passes because the list length is right.
    # The newline only becomes an extra physical line at "\n".join() time,
    # shifting every later line off its segment — so catch it before the write.
    for slot in in_range:
        if "\n" in slot.text or "\r" in slot.text:
            problems.append(
                Problem(slot.file_line, "line contains an embedded newline; one line per segment")
            )

    for slot in in_range:
        if slot.is_silence:
            continue
        lineno = slot.file_line
        cleaned = _strip_tags(slot.text)
        syntax = _check_patch_syntax(cleaned)
        if syntax:
            problems.extend(Problem(lineno, msg) for msg in syntax)
            # Don't pile a confusing decomposition error onto a syntax error.
            continue

        original = segments[slot.speech_line - 1].get("text", "").strip()
        corrected = apply_patches_to_lines([cleaned])[0].strip()
        # Only diagnose lines that actually changed (skip untouched segments);
        # a None cause means the line decomposes cleanly — a legitimate edit.
        if corrected != original:
            cause = _diagnose_decomposition(cleaned.strip(), original)
            if cause is not None:
                problems.append(Problem(lineno, cause))

    # Parity guard: even when every itemised check passes, run the real
    # intervals-stage sync — it is the single source of truth (its <cut>
    # expansion is stateful across lines, which the per-line checks above
    # can only approximate). A rejection here becomes a Problem instead of
    # a pipeline crash discovered stages later.
    if not problems:
        try:
            sync_text_to_json(json_data, edit_lines)
        except ValueError as exc:
            m = re.search(r"Segment (\d+)", str(exc))
            lineno = parsed.file_index(int(m.group(1)) + 1, False) if m else None
            problems.append(Problem(lineno, f"intervals-stage sync rejects this file: {exc}"))

    problems.sort(key=lambda p: (p.line is None, p.line or 0))
    return problems


def _format(problem: Problem) -> str:
    where = "file" if problem.line is None else f"line {problem.line}"
    return f"{where}: {problem.message}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check an _edits.txt for syntax/integrity problems against "
            "its WhisperX JSON, reporting every problem at once."
        )
    )
    parser.add_argument(
        "--edits-txt",
        required=True,
        dest="edits_txt",
        help="_edits.txt path (may contain {{old->new}} and keep/speed/overlay/cut markers)",
    )
    parser.add_argument("--json", required=True, dest="json_path", help="WhisperX JSON path")
    parser.add_argument(
        "--silence-line-min",
        type=float,
        default=DEFAULT_SILENCE_LINE_MIN,
        dest="silence_line_min",
        help=(
            "director.silence_line_min: the shortest wait guided_edit writes a "
            f"silence line for (default {DEFAULT_SILENCE_LINE_MIN})"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    edit_lines = Path(args.edits_txt).read_text(encoding="utf-8").splitlines()
    with open(args.json_path, encoding="utf-8") as f:
        json_data = json.load(f)

    problems = check_edits(edit_lines, json_data, silence_line_min=args.silence_line_min)
    for p in problems:
        print(_format(p))
    if problems:
        print(f"{len(problems)} problem(s) found")
        sys.exit(1)
    print("no problems found")
    sys.exit(0)


if __name__ == "__main__":
    main()
