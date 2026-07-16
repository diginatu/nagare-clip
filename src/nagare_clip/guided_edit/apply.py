"""guided_edit apply orchestration (Pass B2).

Span ops (``cut``/``speed``/``overlay``/``keep``) are a pure whole-line-range
wrap — the director already fixed the boundaries, so they are applied
deterministically with no LLM call (see :func:`apply_span_op`).  Only ``edit``
ops, which need a within-line ``{{old->new}}`` the director only described in
prose, go through a small-LLM call over the op's boundary line(s); the result
is spliced back into the verbatim edit lines.  Every op is verified via
:mod:`reconcile` and a failing one is reverted and recorded, so a forgotten
closing tag or a silent rephrase never corrupts the file.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.reconcile import verify_op
from nagare_clip.intervals.sync_json import (
    _OVERLAY_OPEN_RE,
    _SPEED_OPEN_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    VERIFY_FAIL,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]
Unapplied = tuple[DirectorOp, str]

_LINE_RE = re.compile(r"^\s*(\d+):\s?(.*)$")


def _open_close_counts(line: str, op_type: str) -> tuple[int, int]:
    """(#open tags, #close tags) of *op_type* on a single line."""
    if op_type == "speed":
        total = len(SPEED_TAG_RE.findall(line))
        opens = len(_SPEED_OPEN_RE.findall(line))
        return opens, total - opens
    if op_type == "overlay":
        total = len(OVERLAY_TAG_RE.findall(line))
        opens = len(_OVERLAY_OPEN_RE.findall(line))
        return opens, total - opens
    if op_type == "keep":
        return line.count("<keep>"), line.count("</keep>")
    if op_type == "cut":
        return line.count("<cut>"), line.count("</cut>")
    return 0, 0  # pragma: no cover


def occupied_lines(lines: list[str], op_type: str) -> set[int]:
    """1-based line numbers already touched by a same-type span.

    A line is occupied if it lies inside an open same-type span or carries any
    same-type tag itself — so wrapping it again would nest/interleave with the
    existing span (which the downstream extractors reject).
    """
    occ: set[int] = set()
    depth = 0
    for i, line in enumerate(lines, start=1):
        opens, closes = _open_close_counts(line, op_type)
        if depth > 0 or opens or closes:
            occ.add(i)
        depth = max(0, depth + opens - closes)
    return occ


def blocked_lines(lines: list[str], op_type: str) -> set[int]:
    """1-based line numbers an op of *op_type* may not touch.

    Same-type overlap would nest/interleave tags (rejected downstream).
    ``<cut>`` is destructive on top of that: the intervals stage deletes
    whatever it wraps, silently swallowing any other tag caught inside — so a
    cut op is blocked by lines under ANY existing tag, and every op is blocked
    by lines under an existing ``<cut>`` span.
    """
    if op_type == "cut":
        types = ("cut", "keep", "speed", "overlay")
    else:
        types = (op_type, "cut")
    blocked: set[int] = set()
    for t in types:
        blocked |= occupied_lines(lines, t)
    return blocked


def clip_range(a: int, b: int, occupied: set[int]) -> tuple[int, int] | None:
    """Largest contiguous run of free lines within ``[a, b]`` (ties → earliest),
    or ``None`` if every line is occupied."""
    best: tuple[int, int] | None = None
    start: int | None = None
    for n in range(a, b + 2):  # +2 so a trailing run is flushed on the last pass
        if n <= b and n not in occupied:
            if start is None:
                start = n
        elif start is not None:
            run = (start, n - 1)
            if best is None or (run[1] - run[0]) > (best[1] - best[0]):
                best = run
            start = None
    return best


def _span_tags(op: DirectorOp) -> tuple[str, str]:
    """(open, close) marker pair for a span op type."""
    if op.type == "cut":
        return "<cut>", "</cut>"
    if op.type == "keep":
        return "<keep>", "</keep>"
    if op.type == "speed":
        return f'<speed factor="{op.factor}">', "</speed>"
    if op.type == "overlay":
        return f'<overlay text="{op.text}">', "</overlay>"
    raise ValueError(f"not a span op: {op.type}")  # pragma: no cover


def apply_span_op(lines: list[str], op: DirectorOp) -> list[str]:
    """Wrap the op's line range in its marker pair, deterministically.

    The director already fixed the boundaries and the op granularity is
    whole-line, so this is a pure splice — the open tag prepends the first
    boundary line, the close tag appends the last (both on one line when the
    range is a single line).  Existing markers/patches on the line are left
    intact (nested inside), so the underlying transcript text is unchanged.
    """
    a, b = op.lines
    open_t, close_t = _span_tags(op)
    new = list(lines)
    if a == b:
        new[a - 1] = f"{open_t}{new[a - 1]}{close_t}"
    else:
        new[a - 1] = f"{open_t}{new[a - 1]}"
        new[b - 1] = f"{new[b - 1]}{close_t}"
    return new


def _instruction(op: DirectorOp) -> str:
    if op.type == "cut":
        what = "Cut (delete) the span described below by wrapping it in <cut>...</cut>."
    elif op.type == "speed":
        what = f'Speed up the span by wrapping it in <speed factor="{op.factor}">...</speed>.'
    elif op.type == "overlay":
        what = (
            f"Add an on-screen overlay by wrapping the span in "
            f'<overlay text="{op.text}">...</overlay>.'
        )
    elif op.type == "keep":
        what = "Protect the span from being cut by wrapping it in <keep>...</keep>."
    elif op.type == "edit":
        what = "Make the within-line text edit described below using {{old->new}}."
    else:  # pragma: no cover - parse layer rejects unknown types
        what = "Edit the span."
    note = f" Where: {op.note}" if op.note else ""
    return what + note


def build_user_prompt(op: DirectorOp, lines: list[str]) -> str:
    """Numbered boundary line(s) for the op, with the middle omitted for wide
    ranges (the tag's open/close on the boundaries spans them automatically)."""
    a, b = op.lines
    parts = [_instruction(op), "", "Lines:"]
    if a == b:
        parts.append(f"{a}: {lines[a - 1]}")
    else:
        parts.append(f"{a}: {lines[a - 1]}")
        if b - a > 1:
            parts.append(f"... ({b - a - 1} line(s) in between are included automatically) ...")
        parts.append(f"{b}: {lines[b - 1]}")
    return "\n".join(parts)


def _parse_returned_lines(response: str, allowed: set[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw in response.splitlines():
        m = _LINE_RE.match(raw)
        if m:
            n = int(m.group(1))
            if n in allowed:
                out[n] = m.group(2)
    return out


def _build_messages(op: DirectorOp, lines: list[str], cfg: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": build_user_prompt(op, lines)},
    ]


def _apply_one_op(
    lines: list[str],
    op: DirectorOp,
    cfg: dict[str, Any],
    call_llm: CallLLM,
    messages: list[dict[str, str]],
) -> tuple[list[str], str]:
    """Returns (new_lines, raw_response)."""
    a, b = op.lines
    allowed = {a} if a == b else {a, b}
    response = call_llm(messages, cfg)
    returned = _parse_returned_lines(response, allowed)
    new_lines = list(lines)
    for n, text in returned.items():
        new_lines[n - 1] = text
    return new_lines, response


def apply_ops(
    edit_lines: list[str],
    ops: list[DirectorOp],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "guided_edit",
) -> tuple[list[str], list[Unapplied]]:
    """Apply ops sequentially; return (new_lines, unapplied).

    Span ops are wrapped deterministically (no LLM, no retry) and verified;
    ``edit`` ops call the LLM and, if the result altered the underlying text or
    was not reflected, retry (config ``max_retries``) with a nudged temperature.
    Any op that still fails verification is reverted and added to *unapplied*
    with the last failure reason.
    """
    lines = list(edit_lines)
    unapplied: list[Unapplied] = []
    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    # Apply cut ops after everything else: <cut> deletes whatever it wraps, so
    # protective/annotation tags (keep/speed/overlay) must land first and the
    # cut then clips around them (the director may emit overlapping ops).
    ordered = sorted(enumerate(ops), key=lambda t: (t[1].type == "cut", t[0]))
    for i, op in ordered:
        section = f"op {i}: {op.type} [{op.lines[0]}-{op.lines[1]}]"
        if op.type != "edit":
            # Span ops are a pure line-range wrap — no LLM judgement needed.
            # Clip the range to lines it may touch (see blocked_lines) so the
            # tags stay disjoint where the downstream extractors require it.
            clipped = clip_range(op.lines[0], op.lines[1], blocked_lines(lines, op.type))
            if clipped is None:
                reason = f"{op.type} op fully overlaps existing span(s)"
                recorder.attempt(
                    unit=unit,
                    attempt=0,
                    total=1,
                    messages=[],
                    outcome=VERIFY_FAIL,
                    reason=reason,
                    cfg=cfg,
                    section=section,
                )
                logger.warning("guided_edit: op %s dropped: %s", op.type, reason)
                unapplied.append((op, reason))
                continue
            eff_op = op if clipped == tuple(op.lines) else replace(op, lines=clipped)
            if eff_op is not op:
                logger.warning(
                    "guided_edit: op %s clipped from %s to %s (span overlap)",
                    op.type,
                    tuple(op.lines),
                    clipped,
                )
            candidate = apply_span_op(lines, eff_op)
            reason = verify_op(lines, candidate, eff_op)
            recorder.attempt(
                unit=unit,
                attempt=0,
                total=1,
                messages=[],
                response="\n".join(candidate[eff_op.lines[0] - 1 : eff_op.lines[1]]),
                outcome=OK if reason is None else VERIFY_FAIL,
                reason="" if reason is None else reason,
                cfg=cfg,
                section=section,
            )
            if reason is None:
                lines = candidate
            else:
                unapplied.append((op, reason))
            continue
        last_reason = f"{op.type} op not applied"
        applied = False
        for attempt in range(attempts):
            attempt_cfg = cfg_for_attempt(cfg, attempt)
            messages = _build_messages(op, lines, attempt_cfg)
            try:
                candidate, response = _apply_one_op(lines, op, attempt_cfg, call_llm, messages)
            except Exception as e:  # noqa: BLE001 - LLM/parse failures are recoverable
                last_reason = f"LLM/apply error: {e}"
                logger.warning(
                    "guided_edit: op %s attempt %d/%d failed: %s",
                    op.type,
                    attempt + 1,
                    attempts,
                    e,
                )
                recorder.attempt(
                    unit=unit,
                    attempt=attempt,
                    total=attempts,
                    messages=messages,
                    error=str(e),
                    outcome=LLM_ERROR,
                    reason=last_reason,
                    cfg=attempt_cfg,
                    section=section,
                )
                continue
            reason = verify_op(lines, candidate, op)
            if reason is None:
                recorder.attempt(
                    unit=unit,
                    attempt=attempt,
                    total=attempts,
                    messages=messages,
                    response=response,
                    outcome=OK,
                    cfg=attempt_cfg,
                    section=section,
                )
                lines = candidate
                applied = True
                break
            last_reason = reason
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=VERIFY_FAIL,
                reason=reason,
                cfg=attempt_cfg,
                section=section,
            )
            logger.warning(
                "guided_edit: op %s attempt %d/%d reverted: %s",
                op.type,
                attempt + 1,
                attempts,
                reason,
            )
        if not applied:
            unapplied.append((op, last_reason))
    if unapplied:
        outcome = DROPPED_ITEMS
        reason = f"{len(unapplied)}/{len(ops)} op(s) unapplied"
    else:
        outcome = OK
        reason = f"{len(ops)} op(s) applied"
    recorder.flush_unit(unit, outcome=outcome, reason=reason)
    return lines, unapplied
