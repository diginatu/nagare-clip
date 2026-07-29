"""Project-wide editorial brief injected into the LLM stages' system prompts.

The ``project:`` config section carries what the transcript cannot say: who the
video is for, how long it should be, what tone it wants, and what happened in a
previous episode.  ``format_brief`` renders it as a plain-text block and
``apply_brief`` appends that block to a stage config's prompt field(s).

Every field is empty by default and an empty brief renders as ``""``, so an
unset ``project:`` section leaves every prompt byte-identical to what it was
before this feature existed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRIEF_HEADER = (
    "Editorial brief (applies to the whole project; follow it when deciding "
    "what to keep, cut, tighten and emphasise):"
)

# (config key, label) in render order.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("audience", "Audience"),
    ("purpose", "Purpose"),
    ("target_duration", "Target duration"),
    ("tone", "Tone"),
    ("story_so_far", "Story so far"),
)


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def load_previous_summary(path: Path | str) -> str:
    """Return the ``summary`` string of a previous project's ``summary.json``.

    Returns ``""`` when the file is missing, unreadable, or has no summary —
    a stale path degrades the brief, it never fails the run.
    """
    p = Path(path)
    if not p.is_file():
        logger.warning("project brief: previous_summary not found: %s", p)
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("project brief: could not read previous_summary %s", p)
        return ""
    summary = data.get("summary") if isinstance(data, dict) else None
    return _clean(summary)


def format_brief(project_cfg: dict[str, Any] | None) -> str:
    """Render the ``project:`` section as a prompt block; ``""`` when unset."""
    project_cfg = project_cfg or {}
    lines: list[str] = []
    for key, label in _FIELDS:
        value = _clean(project_cfg.get(key))
        if value:
            lines.append(f"- {label}: {value}")
    previous = _clean(project_cfg.get("previous_summary"))
    if previous:
        summary = load_previous_summary(previous)
        if summary:
            lines.append(f"- Previous video (this project continues it): {summary}")
    if not lines:
        return ""
    return "\n".join([BRIEF_HEADER, *lines])


def apply_brief(
    stage_cfg: dict[str, Any],
    cfg: dict[str, Any],
    *,
    keys: tuple[str, ...] = ("prompt",),
) -> dict[str, Any]:
    """Return *stage_cfg* with the brief appended to each of *keys*' prompts.

    ``stage_cfg`` is never mutated.  When the brief is empty the very same dict
    is returned, so prompts stay byte-identical to an unbriefed run.
    """
    brief = format_brief(cfg.get("project"))
    if not brief:
        return stage_cfg
    out = dict(stage_cfg)
    for key in keys:
        prompt = out.get(key, "")
        if not isinstance(prompt, str):
            continue
        out[key] = f"{prompt}\n\n{brief}" if prompt else brief
    return out
