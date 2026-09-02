"""The second text call: a headline and the photograph it sits on, together.

The copy call writes hooks from the story and is shown no frames at all; this
one receives those hooks and the frame *descriptions* and decides, per set,
which frame it goes on and where and in what colour the text sits.

**Why it is a separate call, not a fold-in.** Two reasons, both learned here.
First, a concrete example anchors a model harder than the instructions around
it: put two dozen frame descriptions in front of the copy call and it starts
captioning the photographs it can see instead of writing hooks from the story.
Second, image x headline is a multiplication -- if the call that writes copy
also had to see, every re-think of a headline would re-send the shortlist.
Describing each frame once to disk keeps the multiplication in text, where it
is cheap and repeatable, and retyping one hook and re-running only this call is
a real operation.

**It names its frame by INDEX, never by a path.** A path is a string a model
can invent; an index is bounded and checkable. ``publish`` resolves the index
to a path before writing ``publish.json``, because the human editing that file
wants a filename, not a number. An index that does not resolve costs the
background and nothing else.

No images reach this call, so its ``CallLLM`` is ``publish_llm``'s text-only
one.  Values are not validated here either: ``render/thumbnail.py`` already
checks every style value against an allowlist and falls back **per key** to a
preset, so whatever this call fails to decide renders exactly as a project
with no pairing at all does.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from nagare_clip.director.director_llm import _FENCE_RE
from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.publish.describe_frames import FrameDescription
from nagare_clip.publish.publish_llm import CallLLM, PublishCopy
from nagare_clip.render.thumbnail import LINE_KEYS, SET_KEYS, ThumbSet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SetPairing:
    """One set's decision: the frame it goes on, and the look on that frame."""

    frame: int | None = None  # 1-based index into the block that was shown
    style: dict[str, Any] = field(default_factory=dict)  # gravity / offset / shadow
    lines: dict[int, dict[str, Any]] = field(default_factory=dict)  # 1-based line -> style


def format_frame_block(frames: Sequence[FrameDescription]) -> str:
    """The shortlist, numbered, in the terms the response answers in.

    Deliberately **no path**: the response names an index, so a path here
    would only be a second, forgeable way to say the same thing.
    """
    lines: list[str] = []
    for i, frame in enumerate(frames, start=1):
        head = f"{i}. [{frame.kind} {frame.source_time:.1f}s] {frame.label}".rstrip()
        lines.append(f"{head} — {frame.description}" if frame.description else head)
    return "\n".join(lines)


def format_sets_block(sets: Sequence[ThumbSet]) -> str:
    """The copy, with each line numbered so a style can name one."""
    lines: list[str] = []
    for i, thumb_set in enumerate(sets, start=1):
        lines.append(f"Set {i}:")
        lines += [
            f"  {j}. ({line.role}) {line.text}" for j, line in enumerate(thumb_set.lines, start=1)
        ]
    return "\n".join(lines)


def format_pairing_context(sets: Sequence[ThumbSet], frames: Sequence[FrameDescription]) -> str:
    return (
        "Thumbnail copy sets:\n"
        f"{format_sets_block(sets)}\n"
        "\n"
        "Candidate frames:\n"
        f"{format_frame_block(frames)}"
    )


def font_slot_note(fonts: Mapping[str, str]) -> str:
    """The one-line prompt addendum naming the installed font slots.

    Generated rather than written into the prompt so the names the model is
    offered are always the names the renderer can resolve.
    """
    return (
        'A line\'s "font" must be one of these slot names: '
        + ", ".join(sorted(fonts))
        + ". A line with no font, or an unknown one, uses the default face."
    )


def _index_in(value: Any, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= high else None


def _pick(raw: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """Only the style keys we know; anything else never enters the artifact."""
    return {k: raw[k] for k in keys if k in raw}


def _parse_lines(raw: Any, num_lines: int, drop) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        index = _index_in(entry.get("line"), num_lines)
        if index is None:
            drop(f"line style dropped, bad line index {entry.get('line')!r}")
            continue
        out[index] = _pick(entry, LINE_KEYS)
    return out


def try_parse_pairing_response(
    response: str,
    num_sets: int,
    num_frames: int,
    *,
    lines_per_set: Sequence[int] | None = None,
    drops: list[str] | None = None,
) -> dict[int, SetPairing] | None:
    """Parse the pairing response; ``None`` on a hard failure (retryable).

    Hard failure is invalid JSON, a non-object, or no usable ``sets`` array --
    the pairing IS the call, so an attempt that produced none is worth
    retrying.  Everything below that degrades item by item: a set index out of
    range is dropped, and a **frame** index out of range costs only the
    background, since the rest of the decision is still usable and the set
    falls back to the shortlist's first still.
    """

    def drop(msg: str) -> None:
        logger.warning("publish: %s", msg)
        if drops is not None:
            drops.append(msg)

    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("publish: pairing response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sets"), list):
        logger.warning("publish: pairing response has no usable 'sets'; ignoring")
        return None

    out: dict[int, SetPairing] = {}
    for raw in data["sets"]:
        if not isinstance(raw, dict):
            continue
        index = _index_in(raw.get("set"), num_sets)
        if index is None:
            drop(f"pairing dropped, bad set index {raw.get('set')!r}")
            continue
        frame = _index_in(raw.get("frame"), num_frames)
        if frame is None:
            drop(f"set {index}: frame index {raw.get('frame')!r} out of range; no background")
        num_lines = (
            lines_per_set[index - 1]
            if lines_per_set is not None and index <= len(lines_per_set)
            else len(LINE_KEYS)  # generous when the caller did not say
        )
        out[index] = SetPairing(
            frame=frame,
            style=_pick(raw, SET_KEYS),
            lines=_parse_lines(raw.get("lines"), num_lines, drop),
        )
    if not out:
        logger.warning("publish: pairing response named no usable set; ignoring")
        return None
    return out


def apply_pairing(
    sets: Sequence[ThumbSet],
    pairing: Mapping[int, SetPairing],
    frames: Sequence[FrameDescription],
) -> list[ThumbSet]:
    """Resolve each set's frame index to a path and attach its look.

    The copy is never touched -- only the background and the style keys.  A
    set the pairing never mentioned, or whose frame index did not resolve,
    comes back exactly as it went in, which is what falls back to the presets.
    """
    out: list[ThumbSet] = []
    for index, thumb_set in enumerate(sets, start=1):
        decision = pairing.get(index)
        if decision is None:
            out.append(thumb_set)
            continue
        background = ""
        if decision.frame is not None and 1 <= decision.frame <= len(frames):
            background = frames[decision.frame - 1].path
        elif decision.frame is not None:
            logger.warning(
                "publish: set %d named frame %d, which does not exist; no background",
                index,
                decision.frame,
            )
        lines = [
            replace(line, style=decision.lines.get(j, {}))
            for j, line in enumerate(thumb_set.lines, start=1)
        ]
        out.append(ThumbSet(lines=lines, style=dict(decision.style), background=background))
    return out


def generate_pairing(
    copy: PublishCopy,
    frames: Sequence[FrameDescription],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "pairing",
) -> dict[int, SetPairing]:
    """Run the pairing LLM once (with retries); ``{}`` when it never works."""
    sets = copy.thumbnail_copy
    if not sets:
        logger.info("publish: no thumbnail copy; nothing to pair")
        return {}
    if not frames:
        logger.info("publish: no candidate frames; thumbnail copy keeps the preset look")
        return {}

    system_prompt = cfg.get("prompt", "")
    fonts = cfg.get("fonts") or {}
    if fonts:
        system_prompt = f"{system_prompt}\n\n{font_slot_note(fonts)}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": format_pairing_context(sets, frames)},
    ]
    lines_per_set = [len(s.lines) for s in sets]

    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "publish: pairing call failed (attempt %d/%d)", attempt + 1, attempts, exc_info=True
            )
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                error=str(e),
                outcome=LLM_ERROR,
                reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: list[str] = []
        pairing = try_parse_pairing_response(
            response,
            num_sets=len(sets),
            num_frames=len(frames),
            lines_per_set=lines_per_set,
            drops=drops,
        )
        if pairing is None:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="invalid JSON / no usable 'sets'",
                cfg=attempt_cfg,
            )
            logger.warning("publish: pairing unparseable (attempt %d/%d)", attempt + 1, attempts)
            continue
        outcome, reason = (
            (DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)) if drops else (OK, "")
        )
        recorder.attempt(
            unit=unit,
            attempt=attempt,
            total=attempts,
            messages=messages,
            response=response,
            outcome=outcome,
            reason=reason,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=outcome, reason=reason)
        return pairing

    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("publish: all %d pairing attempt(s) failed; the presets stand", attempts)
    return {}
