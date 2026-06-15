"""Director stage (Pass A): parse/validate the big LLM's edit-operation JSON.

The director never re-outputs the transcript; it returns a JSON object
``{"ops": [...]}`` where each op references segment lines by 1-based number.
This module turns that response into validated :class:`DirectorOp` objects,
dropping any malformed/out-of-range op (with a warning) so a single bad op
never derails the rest.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.stage2.llm_filter import _call_llm, apply_patches_to_lines
from nagare_clip.stage3.sync_json import (
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)
from nagare_clip.timing import format_dur_gap

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]

VALID_TYPES = {"cut", "speed", "overlay", "keep", "edit"}

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass
class DirectorOp:
    type: str
    lines: Tuple[int, int]  # 1-based inclusive (start, end)
    note: str = ""
    factor: Optional[float] = None
    text: Optional[str] = None
    extra: dict = field(default_factory=dict)


def _coerce_lines(value: Any, num_lines: int) -> Optional[Tuple[int, int]]:
    """Validate a ``lines`` value into a 1-based inclusive (start, end) range."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(value, int):
        start = end = value
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
        if isinstance(start, bool) or isinstance(end, bool):
            return None
        if not isinstance(start, int) or not isinstance(end, int):
            return None
    else:
        return None
    if not (1 <= start <= end <= num_lines):
        return None
    return (start, end)


def _parse_op(
    raw: Any, num_lines: int, drops: Optional[List[str]] = None
) -> Optional[DirectorOp]:
    def _drop(msg: str) -> None:
        logger.warning("Director op dropped: %s", msg)
        if drops is not None:
            drops.append(msg)

    if not isinstance(raw, dict):
        return None
    op_type = raw.get("type")
    if op_type not in VALID_TYPES:
        _drop(f"unknown type {op_type!r}")
        return None
    lines = _coerce_lines(raw.get("lines"), num_lines)
    if lines is None:
        _drop(f"bad lines {raw.get('lines')!r}")
        return None

    note = str(raw.get("note", "") or "")

    factor: Optional[float] = None
    if op_type == "speed":
        raw_factor = raw.get("factor")
        if not isinstance(raw_factor, (int, float)) or isinstance(raw_factor, bool):
            _drop("speed op missing factor")
            return None
        factor = float(raw_factor)
        if factor <= 0:
            _drop(f"speed factor {factor!r} <= 0")
            return None

    text: Optional[str] = None
    if op_type == "overlay":
        raw_text = raw.get("text")
        if not isinstance(raw_text, str) or raw_text == "":
            _drop("overlay op empty/missing text")
            return None
        text = raw_text

    return DirectorOp(type=op_type, lines=lines, note=note, factor=factor, text=text)


def try_parse_director_response(
    response: str, num_lines: int, drops: Optional[List[str]] = None
) -> Optional[List[DirectorOp]]:
    """Parse the director LLM response, distinguishing failure from empty.

    Returns ``None`` on a *hard* parse failure (invalid JSON / no ``ops``
    array) so the caller can retry; returns the (possibly empty) validated op
    list otherwise.  A valid ``{"ops": []}`` is a legitimate "no edits" result
    and yields ``[]`` (not ``None``).  Individual malformed ops are skipped
    (logged) rather than aborting the whole list.
    """
    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("Director response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        logger.warning("Director response has no 'ops' array; ignoring")
        return None

    ops: List[DirectorOp] = []
    for raw in data["ops"]:
        op = _parse_op(raw, num_lines, drops)
        if op is not None:
            ops.append(op)
    return ops


def parse_director_response(response: str, num_lines: int) -> List[DirectorOp]:
    """Parse the director LLM response into validated ops.

    Thin wrapper over :func:`try_parse_director_response` that collapses a hard
    parse failure to ``[]`` (backward-compatible).
    """
    return try_parse_director_response(response, num_lines) or []


def ops_from_dict(data: Any, num_lines: int) -> List[DirectorOp]:
    """Load validated ops from a parsed ``_director.json`` dict.

    Same validation as :func:`parse_director_response`; invalid/out-of-range
    ops are skipped.
    """
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return []
    ops: List[DirectorOp] = []
    for raw in data["ops"]:
        op = _parse_op(raw, num_lines)
        if op is not None:
            ops.append(op)
    return ops


def clean_for_display(edit_lines: List[str]) -> List[str]:
    """Render edit lines as plain readable text for the director.

    Applies ``{{old->new}}`` patches and strips any marker tags so the LLM
    sees clean prose, not edit syntax.
    """
    stripped = [
        CUT_TAG_RE.sub(
            "",
            OVERLAY_TAG_RE.sub(
                "", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))
            ),
        )
        for line in edit_lines
    ]
    return apply_patches_to_lines(stripped)


def format_numbered_transcript(clean_lines: List[str]) -> str:
    """Format clean lines as ``N: text`` (1-based), matching the filter LLM."""
    return "\n".join(f"{i + 1}: {text}" for i, text in enumerate(clean_lines))


def format_numbered_transcript_timed(
    clean_lines: List[str],
    seg_times: List[Tuple[Optional[float], Optional[float]]],
) -> str:
    """``N: text  [dur, gap]`` (1-based), gap = time to the next line.

    Per line: ``dur = end - start``; ``gap = next.start - this.end`` (the last
    line has no gap).  A missing ``start``/``end`` degrades that line's bracket
    via :func:`format_dur_gap` (possibly to no bracket at all).
    """
    out: List[str] = []
    for i, text in enumerate(clean_lines):
        start, end = seg_times[i]
        dur = end - start if start is not None and end is not None else None
        gap: Optional[float] = None
        if i + 1 < len(clean_lines):
            nxt_start = seg_times[i + 1][0]
            if end is not None and nxt_start is not None:
                gap = nxt_start - end
        bracket = format_dur_gap(dur, gap)
        out.append(f"{i + 1}: {text}  {bracket}".rstrip())
    return "\n".join(out)


def ops_to_dict(ops: List[DirectorOp]) -> Dict[str, Any]:
    """Serialise ops to the ``{stem}_director.json`` shape."""
    out: List[Dict[str, Any]] = []
    for op in ops:
        entry: Dict[str, Any] = {
            "type": op.type,
            "lines": [op.lines[0], op.lines[1]],
        }
        if op.factor is not None:
            entry["factor"] = op.factor
        if op.text is not None:
            entry["text"] = op.text
        if op.note:
            entry["note"] = op.note
        out.append(entry)
    return {"ops": out}


def generate_director_ops(
    edit_lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    overview_context: str = "",
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
    seg_times: Optional[List[Tuple[Optional[float], Optional[float]]]] = None,
) -> List[DirectorOp]:
    """Run the director LLM over the transcript and return validated ops.

    Retries (config ``max_retries``) on an LLM exception or a hard parse
    failure, nudging temperature up each attempt.  A valid empty op list is
    accepted without retry.  Returns ``[]`` after all attempts fail (graceful
    no-op), so the pipeline proceeds with the unedited transcript.

    ``overview_context`` (from the summary/plan stages) is appended to the system
    prompt when non-empty; an empty string leaves the prompt unchanged.
    """
    clean_lines = clean_for_display(edit_lines)
    system_prompt = cfg.get("prompt", "")
    if overview_context:
        system_prompt = f"{system_prompt}\n\n{overview_context}"
    if seg_times is not None and len(seg_times) == len(clean_lines):
        user_content = format_numbered_transcript_timed(clean_lines, seg_times)
    else:
        user_content = format_numbered_transcript(clean_lines)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "Director LLM call failed (attempt %d/%d)",
                attempt + 1,
                attempts,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: List[str] = []
        ops = try_parse_director_response(
            response, num_lines=len(clean_lines), drops=drops
        )
        if ops is None:
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid JSON / no 'ops' array", cfg=attempt_cfg,
            )
            logger.warning(
                "Director response unparseable (attempt %d/%d)", attempt + 1, attempts
            )
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} op(s) dropped: " + "; ".join(drops)
        elif not ops:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=unit, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, reason=reason, cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=outcome, reason=reason)
        return ops
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("Director: all %d attempt(s) failed; proceeding with no ops", attempts)
    return []
