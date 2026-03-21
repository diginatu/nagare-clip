"""LLM-based transcription text filter using {{old->new}} patch syntax."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_PATCH_RE = re.compile(r"\{\{([^}]*?)->(.*?)\}\}")
_LINE_RE = re.compile(r"^(\d+):\s?(.*)")


def apply_patches_to_lines(lines: List[str]) -> List[str]:
    """Apply {{old->new}} patches in each line, returning clean text.

    Lines without patches are returned as-is.  For each line that contains
    markers, the ``old`` part is replaced with ``new`` and the marker syntax
    is stripped.  If validation fails for a line, the original text (with
    markers removed by keeping ``old``) is returned instead.
    """
    clean: List[str] = []
    for line in lines:
        original = _PATCH_RE.sub(r"\1", line)
        result = _apply_patches(line, original)
        clean.append(result if result is not None else original)
    return clean


def filter_transcript(lines: List[str], cfg: Dict[str, Any]) -> List[str]:
    """Send transcript lines to LLM in batches, return lines with {{old->new}} markers.

    The returned lines preserve the ``{{old->new}}`` patch syntax so that
    a human can review and further edit the markers before they are applied
    by Stage 3.  Falls back to original lines on any API or parse failure.
    """
    if not lines:
        return []

    batch_size = cfg.get("batch_size", 10)
    batches = _batch_lines(lines, batch_size)
    result = list(lines)  # copy

    for batch in batches:
        try:
            prompt_text = _format_batch(batch)
            messages = [
                {"role": "system", "content": cfg.get("prompt", "")},
                {"role": "user", "content": prompt_text},
            ]
            response = _call_llm(messages, cfg)
            patches = _parse_response(response, batch)
            for idx, corrected in patches.items():
                result[idx] = corrected
        except Exception:
            logger.warning(
                "LLM filter failed for batch starting at line %d, keeping originals",
                batch[0][0] + 1,
                exc_info=True,
            )

    return result


def _batch_lines(
    lines: List[str], batch_size: int
) -> List[List[Tuple[int, str]]]:
    """Group (index, line) into batches of batch_size."""
    indexed = list(enumerate(lines))
    return [indexed[i : i + batch_size] for i in range(0, len(indexed), batch_size)]


def _format_batch(batch: List[Tuple[int, str]]) -> str:
    """Format batch as numbered lines (1-indexed for LLM readability)."""
    return "\n".join(f"{idx + 1}: {text}" for idx, text in batch)


def _call_llm(messages: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
    """Call OpenAI-compatible chat completions API via urllib."""
    api_base = cfg.get("api_base", "http://localhost:11434/v1").rstrip("/")
    url = f"{api_base}/chat/completions"

    body = {
        "model": cfg.get("model", "gemma3:4b"),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.1),
    }

    headers = {"Content-Type": "application/json"}
    api_key = cfg.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    timeout = cfg.get("timeout", 60)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise ConnectionError(f"LLM API request failed: {e}") from e

    return result["choices"][0]["message"]["content"]


def _parse_response(
    response: str, original_batch: List[Tuple[int, str]]
) -> Dict[int, str]:
    """Parse LLM response lines, validate {{old->new}} markers.

    Returns a mapping from original index to response text with markers
    preserved.  Lines whose markers fail validation are omitted (caller
    keeps the original).
    """
    original_map = {idx: text for idx, text in original_batch}
    result: Dict[int, str] = {}

    # Parse response lines with line-number prefix
    response_lines: Dict[int, str] = {}
    for raw_line in response.splitlines():
        m = _LINE_RE.match(raw_line)
        if m:
            line_num = int(m.group(1)) - 1  # convert to 0-indexed
            response_lines[line_num] = m.group(2)

    if not response_lines:
        logger.warning("LLM response has no parseable numbered lines, keeping originals")
        return {}

    for idx, original_text in original_map.items():
        if idx not in response_lines:
            logger.warning("LLM did not return line %d, keeping original", idx + 1)
            continue

        response_text = response_lines[idx]
        if _validate_patches(response_text, original_text):
            result[idx] = response_text

    return result


def _validate_patches(response_text: str, original_text: str) -> bool:
    """Check that all {{old->new}} markers in response_text are valid.

    Returns True if the response can be accepted (markers are valid or absent).
    """
    markers = list(_PATCH_RE.finditer(response_text))
    if not markers:
        return True
    for m in markers:
        old = m.group(1)
        if old and old not in original_text:
            logger.warning(
                "Patch old text %r not found in original %r, keeping original",
                old,
                original_text,
            )
            return False
    return True


def _apply_patches(response_text: str, original_text: str) -> str | None:
    """Apply {{old->new}} patches from response_text, validating against original.

    Returns the corrected text, or None if validation fails.
    """
    markers = list(_PATCH_RE.finditer(response_text))

    if not markers:
        # No patches — LLM returned text as-is (possibly unchanged)
        return response_text

    if not _validate_patches(response_text, original_text):
        return None

    # Build the corrected text by replacing markers with 'new' part
    # and stripping the marker syntax
    corrected = response_text
    for m in reversed(markers):  # reverse to preserve positions
        corrected = corrected[: m.start()] + m.group(2) + corrected[m.end() :]

    return corrected
