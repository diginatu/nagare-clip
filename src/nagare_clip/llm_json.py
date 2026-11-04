"""Reading a model's JSON reply: the ```` ```json ```` fence models wrap it in.

Shared by every stage that parses a model's JSON (``summary``, ``publish``),
so the readers cannot disagree about what a fenced reply is.
"""

from __future__ import annotations

import re

FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_code_fence(response: str) -> str:
    """*response* without the ```` ```json ```` fence models keep wrapping it in."""
    text = response.strip()
    fence = FENCE_RE.match(text)
    return fence.group(1) if fence else text
