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

from nagare_clip.stage2.llm_filter import _call_llm, apply_patches_to_lines
from nagare_clip.stage3.sync_json import (
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)

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


def _parse_op(raw: Any, num_lines: int) -> Optional[DirectorOp]:
    if not isinstance(raw, dict):
        return None
    op_type = raw.get("type")
    if op_type not in VALID_TYPES:
        logger.warning("Director op dropped: unknown type %r", op_type)
        return None
    lines = _coerce_lines(raw.get("lines"), num_lines)
    if lines is None:
        logger.warning("Director op dropped: bad lines %r", raw.get("lines"))
        return None

    note = str(raw.get("note", "") or "")

    factor: Optional[float] = None
    if op_type == "speed":
        raw_factor = raw.get("factor")
        if not isinstance(raw_factor, (int, float)) or isinstance(raw_factor, bool):
            logger.warning("Director speed op dropped: missing factor")
            return None
        factor = float(raw_factor)
        if factor <= 0:
            logger.warning("Director speed op dropped: factor %r <= 0", factor)
            return None

    text: Optional[str] = None
    if op_type == "overlay":
        raw_text = raw.get("text")
        if not isinstance(raw_text, str) or raw_text == "":
            logger.warning("Director overlay op dropped: empty/missing text")
            return None
        text = raw_text

    return DirectorOp(type=op_type, lines=lines, note=note, factor=factor, text=text)


def parse_director_response(response: str, num_lines: int) -> List[DirectorOp]:
    """Parse the director LLM response into validated ops.

    Returns ``[]`` on any JSON/shape failure; individual malformed ops are
    skipped (logged) rather than aborting the whole list.
    """
    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("Director response is not valid JSON; ignoring")
        return []
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        logger.warning("Director response has no 'ops' array; ignoring")
        return []

    ops: List[DirectorOp] = []
    for raw in data["ops"]:
        op = _parse_op(raw, num_lines)
        if op is not None:
            ops.append(op)
    return ops


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
) -> List[DirectorOp]:
    """Run the director LLM over the transcript and return validated ops.

    Returns ``[]`` on any LLM/parse failure (graceful no-op), so the pipeline
    proceeds with the unedited transcript.
    """
    clean_lines = clean_for_display(edit_lines)
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": format_numbered_transcript(clean_lines)},
    ]
    try:
        response = call_llm(messages, cfg)
    except Exception:
        logger.warning("Director LLM call failed; proceeding with no ops", exc_info=True)
        return []
    return parse_director_response(response, num_lines=len(clean_lines))
