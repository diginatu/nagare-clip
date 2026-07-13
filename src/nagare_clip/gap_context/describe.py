"""One vision-LLM call per silent gap: frames in, a plain-text description out.

The frames are sent as base64 data-URI ``image_url`` content parts; LiteLLM
converts them to whatever the configured provider expects, so no
provider-specific client is needed.  The response is plain text (no JSON to
fail on): an empty/whitespace answer counts as a failure and is retried.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nagare_clip.gap_context.gaps import Gap
from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import LLM_ERROR, NULL_RECORDER, OK, UNPARSEABLE, Recorder
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, Any]], dict[str, Any]], str]


@dataclass
class GapFrames:
    """A selected gap plus the frames actually extracted for it."""

    start: float
    end: float
    frames: list[Path] = field(default_factory=list)  # absolute, on disk
    relpaths: list[str] = field(default_factory=list)  # recorded in gaps.json

    @property
    def duration(self) -> float:
        return self.end - self.start


def _image_part(path: Path) -> dict[str, Any] | None:
    """Base64 data-URI content part; ``None`` when the file cannot be read."""
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.warning("gap_context: could not read frame %s: %s", path, e)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _header_text(gf: GapFrames, before: str, after: str) -> str:
    lines = [
        f"Silent gap: {gf.start:.1f}s - {gf.end:.1f}s ({gf.duration:.1f}s long).",
        f"{len(gf.frames)} frame(s) sampled in chronological order.",
    ]
    if before:
        lines.append(f"Spoken line before the gap: {before}")
    if after:
        lines.append(f"Spoken line after the gap: {after}")
    return "\n".join(lines)


def _content_parts(
    gf: GapFrames, before: str, after: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """User content parts + the relpaths of the frames that made it in."""
    images: list[dict[str, Any]] = []
    kept: list[str] = []
    for i, path in enumerate(gf.frames):
        part = _image_part(path)
        if part is None:
            continue
        images.append(part)
        if i < len(gf.relpaths):
            kept.append(gf.relpaths[i])
    if not images:
        return [], []
    header = _header_text(
        GapFrames(start=gf.start, end=gf.end, frames=gf.frames[: len(images)], relpaths=kept),
        before,
        after,
    )
    return [{"type": "text", "text": header}, *images], kept


def build_messages(
    gf: GapFrames, cfg: dict[str, Any], *, before: str = "", after: str = ""
) -> list[dict[str, Any]]:
    """System prompt + one multimodal user message (header text, then frames)."""
    parts, _ = _content_parts(gf, before, after)
    return [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": parts},
    ]


def _report_messages(messages: list[dict[str, Any]], relpaths: list[str]) -> list[dict[str, str]]:
    """Flatten for the LLM report: frame PATHS, never base64 payloads."""
    system = str(messages[0].get("content", ""))
    parts = messages[1].get("content", [])
    header = parts[0]["text"] if parts and parts[0].get("type") == "text" else ""
    user = header + "\n\nFrames:\n" + "\n".join(f"- {p}" for p in relpaths)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def describe_gap(
    gf: GapFrames,
    cfg: dict[str, Any],
    *,
    unit: str,
    before: str = "",
    after: str = "",
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> Gap | None:
    """Describe one gap. Returns ``None`` when it should be skipped."""
    parts, relpaths = _content_parts(gf, before, after)
    if not parts:
        logger.warning(
            "gap_context: no readable frame for gap %.1f-%.1f; skipping", gf.start, gf.end
        )
        return None
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": parts},
    ]
    report_messages = _report_messages(messages, relpaths)

    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "gap_context: LLM call failed (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                unit,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=report_messages,
                error=str(e),
                outcome=LLM_ERROR,
                reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        description = (response or "").strip()
        if not description:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=report_messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="empty description",
                cfg=attempt_cfg,
            )
            logger.warning(
                "gap_context: empty description (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                unit,
            )
            continue
        recorder.attempt(
            unit=unit,
            attempt=attempt,
            total=attempts,
            messages=report_messages,
            response=response,
            outcome=OK,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=OK)
        return Gap(start=gf.start, end=gf.end, frames=relpaths, description=description)

    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("gap_context: all %d attempt(s) failed for %s; gap dropped", attempts, unit)
    return None
