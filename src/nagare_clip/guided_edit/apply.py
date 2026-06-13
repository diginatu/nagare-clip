"""guided_edit apply orchestration (Pass B2).

Applies each director op with one small-LLM call over just the op's boundary
line(s), splices the result back into the verbatim edit lines, and verifies it
via :mod:`reconcile`.  A failing op is reverted and recorded so a forgotten
closing tag or a silent rephrase never corrupts the file.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Tuple

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.reconcile import verify_op
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.stage2.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]
Unapplied = Tuple[DirectorOp, str]

_LINE_RE = re.compile(r"^\s*(\d+):\s?(.*)$")


def _instruction(op: DirectorOp) -> str:
    if op.type == "cut":
        what = "Cut (delete) the span described below by wrapping it in <cut>...</cut>."
    elif op.type == "speed":
        what = (
            f'Speed up the span by wrapping it in '
            f'<speed factor="{op.factor}">...</speed>.'
        )
    elif op.type == "overlay":
        what = (
            f'Add an on-screen overlay by wrapping the span in '
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


def build_user_prompt(op: DirectorOp, lines: List[str]) -> str:
    """Numbered boundary line(s) for the op, with the middle omitted for wide
    ranges (the tag's open/close on the boundaries spans them automatically)."""
    a, b = op.lines
    parts = [_instruction(op), "", "Lines:"]
    if a == b:
        parts.append(f"{a}: {lines[a - 1]}")
    else:
        parts.append(f"{a}: {lines[a - 1]}")
        if b - a > 1:
            parts.append(
                f"... ({b - a - 1} line(s) in between are included automatically) ..."
            )
        parts.append(f"{b}: {lines[b - 1]}")
    return "\n".join(parts)


def _parse_returned_lines(response: str, allowed: set[int]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for raw in response.splitlines():
        m = _LINE_RE.match(raw)
        if m:
            n = int(m.group(1))
            if n in allowed:
                out[n] = m.group(2)
    return out


def _apply_one_op(
    lines: List[str], op: DirectorOp, cfg: Dict[str, Any], call_llm: CallLLM
) -> List[str]:
    a, b = op.lines
    allowed = {a} if a == b else {a, b}
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": build_user_prompt(op, lines)},
    ]
    response = call_llm(messages, cfg)
    returned = _parse_returned_lines(response, allowed)
    new_lines = list(lines)
    for n, text in returned.items():
        new_lines[n - 1] = text
    return new_lines


def apply_ops(
    edit_lines: List[str],
    ops: List[DirectorOp],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
) -> Tuple[List[str], List[Unapplied]]:
    """Apply ops sequentially; return (new_lines, unapplied).

    Each op is verified after application; if it altered the underlying text or
    was not reflected, the attempt is retried (config ``max_retries``) with a
    nudged temperature.  After all attempts fail the op is reverted and added
    to *unapplied* with the last failure reason.
    """
    lines = list(edit_lines)
    unapplied: List[Unapplied] = []
    attempts = retry_attempts(cfg)
    for op in ops:
        last_reason = f"{op.type} op not applied"
        applied = False
        for attempt in range(attempts):
            try:
                candidate = _apply_one_op(
                    lines, op, cfg_for_attempt(cfg, attempt), call_llm
                )
            except Exception as e:  # noqa: BLE001 - LLM/parse failures are recoverable
                last_reason = f"LLM/apply error: {e}"
                logger.warning(
                    "guided_edit: op %s attempt %d/%d failed: %s",
                    op.type,
                    attempt + 1,
                    attempts,
                    e,
                )
                continue
            reason = verify_op(lines, candidate, op)
            if reason is None:
                lines = candidate
                applied = True
                break
            last_reason = reason
            logger.warning(
                "guided_edit: op %s attempt %d/%d reverted: %s",
                op.type,
                attempt + 1,
                attempts,
                reason,
            )
        if not applied:
            unapplied.append((op, last_reason))
    return lines, unapplied


def format_unapplied(unapplied: List[Unapplied]) -> str:
    """Render the unapplied-ops report (one op per line)."""
    if not unapplied:
        return "# all director ops applied\n"
    out = ["# director ops that could not be applied (op | lines | reason)"]
    for op, reason in unapplied:
        detail = op.note or op.text or (f"factor={op.factor}" if op.factor else "")
        out.append(f"{op.type}\t{op.lines[0]}-{op.lines[1]}\t{reason}\t{detail}")
    return "\n".join(out) + "\n"
