"""LLM-based transcription text filter using {{old->new}} patch syntax."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from nagare_clip.llm_report import DROPPED_ITEMS, LLM_ERROR, NULL_RECORDER, OK, Recorder

logger = logging.getLogger(__name__)

PATCH_RE = re.compile(r"\{\{([^}]*?)->(.*?)\}\}")
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
        original = PATCH_RE.sub(r"\1", line)
        result = _apply_patches(line, original)
        clean.append(result if result is not None else original)
    return clean


def filter_transcript(
    lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm=None,
    recorder: Recorder = NULL_RECORDER,
) -> List[str]:
    """Send transcript lines to LLM in batches, return lines with {{old->new}} markers.

    The returned lines preserve the ``{{old->new}}`` patch syntax so that
    a human can review and further edit the markers before they are applied
    by Stage 3.  Falls back to original lines on any API or parse failure.
    """
    if call_llm is None:
        call_llm = _call_llm
    if not lines:
        return []

    batch_size = cfg.get("batch_size", 10)
    batches = _batch_lines(lines, batch_size)
    result = list(lines)  # copy

    stats: Dict[int, Dict[str, int]] = defaultdict(lambda: {"total": 0, "succeeded": 0})
    for batch in batches:
        _process_batch(batch, result, cfg, batch_size, stats, call_llm=call_llm, recorder=recorder)

    _log_stats(stats, batch_size)
    return result


def _process_batch(
    batch: List[Tuple[int, str]],
    result: List[str],
    cfg: Dict[str, Any],
    current_size: int,
    stats: Optional[Dict[int, Dict[str, int]]] = None,
    *,
    call_llm=None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    """Run one LLM call for ``batch``; on any line missing from the parse
    result, recursively retry the failed lines with a halved batch size
    (down to ``retry_min_batch_size``)."""
    if call_llm is None:
        call_llm = _call_llm
    a = batch[0][0] + 1
    b = batch[-1][0] + 1
    unit = f"lines {a}-{b} (size {current_size})"
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": _format_batch(batch)},
    ]
    try:
        response = call_llm(messages, cfg)
        patches = _parse_response(response, batch)
    except Exception as e:  # noqa: BLE001 - recoverable
        if stats is not None:
            stats[current_size]["total"] += len(batch)
        logger.warning(
            "LLM filter failed for batch starting at line %d, keeping originals",
            batch[0][0] + 1,
            exc_info=True,
        )
        recorder.attempt(
            unit=unit, attempt=0, total=1, messages=messages, error=str(e),
            outcome=LLM_ERROR, reason="LLM call failed", cfg=cfg,
        )
        recorder.flush_unit(unit, outcome=LLM_ERROR, reason="LLM call failed")
        return

    if stats is not None:
        stats[current_size]["total"] += len(batch)
        stats[current_size]["succeeded"] += len(patches)

    for idx, corrected in patches.items():
        result[idx] = corrected

    missing = len(batch) - len(patches)
    if missing:
        outcome = DROPPED_ITEMS
        reason = f"{missing}/{len(batch)} line(s) kept original"
    else:
        outcome = OK
        reason = ""
    recorder.attempt(
        unit=unit, attempt=0, total=1, messages=messages, response=response,
        outcome=outcome, reason=reason, cfg=cfg,
    )
    recorder.flush_unit(unit, outcome=outcome, reason=reason)

    if not cfg.get("retry_on_invalid", True):
        return
    min_size = max(1, int(cfg.get("retry_min_batch_size", 1)))
    if current_size <= min_size:
        return
    failed = [(idx, text) for idx, text in batch if idx not in patches]
    if not failed:
        return
    new_size = max(min_size, current_size // 2)
    if new_size >= current_size:
        return
    logger.info(
        "Retrying %d failed line(s) with batch_size=%d (was %d)",
        len(failed),
        new_size,
        current_size,
    )
    for i in range(0, len(failed), new_size):
        _process_batch(
            failed[i : i + new_size], result, cfg, new_size, stats,
            call_llm=call_llm, recorder=recorder,
        )


def _log_stats(stats: Dict[int, Dict[str, int]], initial_size: int) -> None:
    if not stats:
        return
    for size in sorted(stats.keys(), reverse=True):
        s = stats[size]
        label = f"batch_size={size}" if size == initial_size else f"batch_size={size} (retry)"
        pct = 100.0 * s["succeeded"] / s["total"] if s["total"] else 0.0
        logger.info("LLM filter %s: %d/%d succeeded (%.0f%%)", label, s["succeeded"], s["total"], pct)

    retry_saved = sum(s["succeeded"] for sz, s in stats.items() if sz < initial_size)
    retry_total = sum(s["total"] for sz, s in stats.items() if sz < initial_size)
    all_total = sum(s["total"] for s in stats.values())
    all_succeeded = sum(s["succeeded"] for s in stats.values())
    if retry_total > 0:
        logger.info(
            "LLM filter total: %d/%d line-attempts succeeded; retries saved %d/%d lines (%.0f%%)",
            all_succeeded, all_total, retry_saved, retry_total, 100.0 * retry_saved / retry_total,
        )


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
    """Call Ollama native chat API via urllib."""
    api_base = cfg.get("api_base", "http://localhost:11434").rstrip("/")
    url = f"{api_base}/api/chat"

    body: Dict[str, Any] = {
        "model": cfg.get("model", "qwen3.5:4b"),
        "messages": messages,
        "stream": False,
        "think": cfg.get("thinking", False),
        "options": {
            "temperature": cfg.get("temperature", 0.1),
        },
    }

    response_format = cfg.get("response_format")
    if response_format:
        body["format"] = response_format

    headers = {"Content-Type": "application/json"}
    api_key = cfg.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    logger.debug("LLM request: %s", json.dumps(body, ensure_ascii=False))

    timeout = cfg.get("timeout", 300)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise ConnectionError(f"LLM API request failed: {e}") from e

    logger.debug("LLM response: %s", json.dumps(result, ensure_ascii=False))

    return result["message"]["content"]


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
            result[idx] = _strip_noop_markers(response_text)

    return result


def _strip_noop_markers(text: str) -> str:
    """Remove {{X->X}} markers where old and new are identical."""

    def _replace(m: re.Match) -> str:
        if m.group(1) == m.group(2):
            return m.group(1)
        return m.group(0)

    return PATCH_RE.sub(_replace, text)


def _validate_patches(response_text: str, original_text: str) -> bool:
    """Check that all {{old->new}} markers in response_text are valid.

    Returns True if the response can be accepted (markers are valid or absent).
    """
    markers = list(PATCH_RE.finditer(response_text))
    if not markers:
        if response_text.strip() != original_text.strip():
            logger.warning(
                "LLM changed text without {{old->new}} markers, keeping original: %r -> %r",
                original_text,
                response_text,
            )
            return False
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

    # Verify non-marker text wasn't silently changed: resolving markers back
    # to their old values must reconstruct the original text.
    reconstructed = PATCH_RE.sub(r"\1", response_text)
    if reconstructed.strip() != original_text.strip():
        logger.warning(
            "LLM changed text outside {{old->new}} markers, keeping original: %r -> %r",
            original_text,
            reconstructed,
        )
        return False

    return True


def _apply_patches(response_text: str, original_text: str) -> str | None:
    """Apply {{old->new}} patches from response_text, validating against original.

    Returns the corrected text, or None if validation fails.
    """
    markers = list(PATCH_RE.finditer(response_text))

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
