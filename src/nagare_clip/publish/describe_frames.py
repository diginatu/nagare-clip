"""One vision call per candidate still: what is actually *visible* in it.

The frame shortlist carries the director's own labels — but a label says what
it thought was happening at that moment, not what a viewer can make out.  A
frame labelled 「まさかの水漏れ発覚」 may show a person's back.  Only looking
can tell, so each still is looked at once and written down as prose: what is
legible, where the subject sits, which regions are empty, and what colour and
lightness those empty regions are.

The label is deliberately **not** shown to the vision model.  The description
exists to catch a label whose frame does not match it; showing the claim to the
model that is meant to check it would only get the claim confirmed.

**Described once, ever.**  ``publish/frames.json`` keys each description to a
content hash of the JPEG's own bytes, so a re-run over an unchanged shortlist
costs nothing, a re-extracted frame whose picture changed is described again,
and a description a human rewrote by hand survives — if you disagree with what
the model saw, correct the prose and it sticks.

This module owns its own multimodal ``CallLLM`` (``list[dict[str, Any]]``),
following ``gap_context/describe.py`` rather than inventing a second way to
send an image, so ``publish_llm.py``'s text-only alias never has to widen.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import LLM_ERROR, NULL_RECORDER, OK, UNPARSEABLE, Recorder
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, Any]], dict[str, Any]], str]

# Neutral, and it says nothing about the moment: the header exists because a
# content list of images alone is not accepted everywhere, not to tell the
# model anything.
FRAME_HEADER = "One still frame from a video."


@dataclass
class FrameDescription:
    """One shortlist still, and what looking at it showed."""

    stem: str
    source_time: float
    kind: str
    label: str  # the director's claim about the moment; NOT shown to the model
    path: str  # relative to the publish stage dir
    hash: str  # sha256 of the JPEG's bytes -- the reuse key
    description: str = ""


def content_hash(path: Path) -> str:
    """sha256 of the file's bytes, or ``""`` when it cannot be read.

    Over the **bytes**, deliberately: a path and a timestamp are stable across
    a re-extraction that changed the picture, and change across an extraction
    that did not.  Only the content answers "is this the frame I described?".
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def frames_to_dict(entries: Sequence[FrameDescription]) -> dict[str, Any]:
    return {"frames": [asdict(e) for e in entries]}


def frames_from_dict(data: Any) -> list[FrameDescription]:
    """Lenient, like every hand-editable intermediate here: a malformed entry
    is dropped, never raised."""
    raw_frames = data.get("frames") if isinstance(data, dict) else None
    out: list[FrameDescription] = []
    for raw in raw_frames if isinstance(raw_frames, list) else []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path", "")).strip()
        if not path:
            continue
        try:
            source_time = float(raw.get("source_time", 0.0))
        except (TypeError, ValueError):
            source_time = 0.0
        out.append(
            FrameDescription(
                stem=str(raw.get("stem", "")),
                source_time=source_time,
                kind=str(raw.get("kind", "")),
                label=str(raw.get("label", "")),
                path=path,
                hash=str(raw.get("hash", "")),
                description=" ".join(str(raw.get("description", "")).split()),
            )
        )
    return out


def load_frames(path: Path | None) -> list[FrameDescription]:
    """``frames.json`` from a previous run, or nothing."""
    if path is None or not Path(path).is_file():
        return []
    try:
        return frames_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (ValueError, OSError):
        logger.warning("publish: could not read %s", path)
        return []


def _image_part(path: Path) -> dict[str, Any] | None:
    """Base64 data-URI content part; ``None`` when the file cannot be read."""
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.warning("publish: could not read frame %s: %s", path, e)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def build_messages(path: Path, cfg: dict[str, Any]) -> list[dict[str, Any]] | None:
    """System prompt + one multimodal user message; ``None`` if unreadable."""
    part = _image_part(path)
    if part is None:
        return None
    return [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": [{"type": "text", "text": FRAME_HEADER}, part]},
    ]


def _report_messages(messages: list[dict[str, Any]], relpath: str) -> list[dict[str, str]]:
    """Flatten for the LLM report: the frame PATH, never the base64 payload."""
    return [
        {"role": "system", "content": str(messages[0].get("content", ""))},
        {"role": "user", "content": f"{FRAME_HEADER}\n\nFrame:\n- {relpath}"},
    ]


def describe_frame(
    path: Path,
    relpath: str,
    cfg: dict[str, Any],
    *,
    unit: str,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> str:
    """One frame's description, or ``""`` when every attempt failed.

    Plain prose, not JSON: it is read by a model and by a human, and neither
    needs a schema.  An empty answer counts as a failure and is retried.
    """
    messages = build_messages(path, cfg)
    if messages is None:
        return ""
    report_messages = _report_messages(messages, relpath)

    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "publish: frame description failed (attempt %d/%d) for %s",
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
        # Collapsed the way gap_context collapses a description: this text is
        # rendered into a numbered block for the pairing call, and a stray
        # newline there would look like another entry.
        description = " ".join((response or "").split())
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
                "publish: empty frame description (attempt %d/%d) for %s",
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
        return description

    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("publish: no description for %s after %d attempt(s)", unit, attempts)
    return ""


def _unit(shot: Any) -> str:
    return f"frame_{shot.stem}_{shot.time:.3f}"


def describe_frames(
    shots: Sequence[Any],
    publish_dir: Path,
    cfg: dict[str, Any],
    *,
    previous: Sequence[FrameDescription] = (),
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> list[FrameDescription]:
    """Describe every candidate still, skipping the ones already described.

    Reuse is keyed on the hash alone, not on the path: identical bytes are the
    same picture whatever it is filed under, and describing it twice would buy
    nothing.  An entry with an empty description is not reusable — that is a
    call that failed, and the next run should retry it rather than cache the
    failure.
    """
    enabled = bool(cfg.get("enabled", False))
    known: dict[str, str] = {}
    for entry in previous:
        if entry.hash and entry.description:
            known.setdefault(entry.hash, entry.description)

    out: list[FrameDescription] = []
    described = reused = 0
    for shot in shots:
        path = publish_dir / shot.path
        digest = content_hash(path)
        if not digest:
            logger.warning("publish: no still at %s; dropped from frames.json", shot.path)
            continue
        description = known.get(digest, "")
        if description:
            reused += 1
        elif enabled:
            description = describe_frame(
                path,
                shot.path,
                cfg,
                unit=_unit(shot),
                call_llm=call_llm,
                recorder=recorder,
            )
            described += 1
        out.append(
            FrameDescription(
                stem=shot.stem,
                source_time=round(float(shot.time), 3),
                kind=shot.kind,
                label=shot.label,
                path=shot.path,
                hash=digest,
                description=description,
            )
        )
    logger.info(
        "publish: %d frame(s) described, %d reused by hash, %d total",
        described,
        reused,
        len(out),
    )
    return out
