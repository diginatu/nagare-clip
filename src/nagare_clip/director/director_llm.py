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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.gap_context.context import anchor_gaps, annotate_numbered_transcript
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.intervals.sync_json import (
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)
from nagare_clip.llm_client import with_trace_meta
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
from nagare_clip.text_filter.llm_filter import _call_llm, apply_patches_to_lines
from nagare_clip.timing import format_dur_gap

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]

VALID_TYPES = {"cut", "speed", "overlay", "keep", "edit", "timelapse"}

# What the director LLM may emit.  ``speed`` is off the menu: three rounds of
# prompt tightening bounded the mild 1.3-2.0 band and it kept coming back
# (57.4% -> 1.4% -> 49.0% -> 29.9% of the finished video across four runs of the
# same footage), because a middle option is always cheaper to choose than a
# timelapse.  It stays in VALID_TYPES because it is still the marker a
# timelapse desugars into (guided_edit.timelapse.expand_timelapse_ops, which
# runs after parsing) and because a human may still write one into the
# hand-editable _director.json.
MENU_TYPES = VALID_TYPES - {"speed"}

# The factor DIRECTOR_PROMPT calls a real timelapse; the parse-time floor for
# a "timelapse" op (see _parse_op) below which it is a mild speed-up instead.
TIMELAPSE_MIN_FACTOR = 4.0

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass
class DirectorOp:
    type: str
    lines: tuple[int, int]  # 1-based inclusive (start, end)
    note: str = ""
    factor: float | None = None
    text: str | None = None
    duration: float | None = None  # overlay: on-screen seconds (edited timeline)
    extra: dict = field(default_factory=dict)


def _max_keep_lines(cfg: dict[str, Any]) -> int:
    """Read ``director.max_keep_lines`` defensively (``0``/invalid = no limit)."""
    raw = cfg.get("max_keep_lines", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def keep_limit_note(max_keep_lines: int) -> str:
    """The one-line prompt addendum stating the configured ``keep`` width cap.

    Generated rather than written into ``DIRECTOR_PROMPT`` so the number the
    LLM is told is always the number the parser enforces.
    """
    return (
        f'A "keep" op may span at most {max_keep_lines} line(s); a wider one is '
        "rejected and has no effect. A continuous on-screen event fits well "
        "inside that, because nobody is talking through it. Do not stretch a "
        "keep across a talking span to signal that it matters — say so in a "
        '"note" on another op instead. A "timelapse" op needs no keep of its '
        "own; it protects its whole range by itself."
    )


def _coerce_lines(value: Any, num_lines: int | None) -> tuple[int, int] | None:
    """Validate a ``lines`` value into a 1-based inclusive (start, end) range.

    ``num_lines`` of ``None`` drops the upper bound, for reading a *different*
    video's ``_director.json`` (whose transcript is not at hand).
    """
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
    if not (1 <= start <= end):
        return None
    if num_lines is not None and end > num_lines:
        return None
    return (start, end)


def _parse_op(
    raw: Any,
    num_lines: int | None,
    drops: list[str] | None = None,
) -> DirectorOp | None:
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

    # The max_keep_lines cap itself is enforced as a post-pass over the whole
    # op list (see _apply_keep_cap), not here.

    note = str(raw.get("note", "") or "")

    factor: float | None = None
    if op_type in ("speed", "timelapse"):
        raw_factor = raw.get("factor")
        if not isinstance(raw_factor, (int, float)) or isinstance(raw_factor, bool):
            _drop(f"{op_type} op missing factor")
            return None
        factor = float(raw_factor)
        if factor <= 0:
            _drop(f"{op_type} factor {factor!r} <= 0")
            return None
        # The floor is part of what "timelapse" means, not a policy cap: the op
        # carries an uncapped keep, and below this a mild speed-up would smuggle
        # one over talking. A slow span is a plain `speed` op instead.
        if op_type == "timelapse" and factor < TIMELAPSE_MIN_FACTOR:
            _drop(
                f"timelapse factor {factor!r} < {TIMELAPSE_MIN_FACTOR}; "
                "below that it is a mild speed-up, not a timelapse"
            )
            return None

    text: str | None = None
    duration: float | None = None
    if op_type == "timelapse":
        # The caption is optional — a timelapse with no text is still a valid
        # continuity fix.  Its on-screen duration is derived downstream, never
        # stated, so any supplied `duration` is ignored.
        raw_text = raw.get("text")
        if isinstance(raw_text, str):
            normalised = raw_text.replace("\r\n", "\n").replace("\r", "\n")
            text = normalised if normalised.strip() else None

    if op_type == "overlay":
        raw_text = raw.get("text")
        if not isinstance(raw_text, str) or raw_text == "":
            _drop("overlay op empty/missing text")
            return None
        # A multi-line caption is fine — Blender renders the break — but only
        # "\n" is: a lone CR would survive into the single-line _edits.txt
        # marker and split it on any reader that honours it.
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            _drop("overlay op text is blank")
            return None
        # The duration is what makes the op applicable at all: an overlay's
        # on-screen time is stated, never derived from where a tag landed.
        raw_duration = raw.get("duration")
        if not isinstance(raw_duration, (int, float)) or isinstance(raw_duration, bool):
            _drop("overlay op missing duration")
            return None
        duration = float(raw_duration)
        if duration <= 0:
            _drop(f"overlay duration {duration!r} <= 0")
            return None

    return DirectorOp(
        type=op_type, lines=lines, note=note, factor=factor, text=text, duration=duration
    )


def _apply_keep_cap(
    ops: list[DirectorOp],
    max_keep_lines: int,
    drops: list[str] | None,
) -> list[DirectorOp]:
    """Post-pass: drop a ``keep`` op wider than ``max_keep_lines``.  Kept as a
    pass over the whole parsed list (rather than a check inside
    :func:`_parse_op`) so the drop message/logging stays in one place; tests
    assert on it.
    """
    if max_keep_lines <= 0:
        return ops
    kept: list[DirectorOp] = []
    for op in ops:
        if op.type == "keep":
            span = op.lines[1] - op.lines[0] + 1
            if span > max_keep_lines:
                msg = (
                    f"keep op spans {span} lines {list(op.lines)} > "
                    f"max_keep_lines={max_keep_lines}; a keep may span a "
                    "continuous on-screen event, not a talking span"
                )
                logger.warning("Director op dropped: %s", msg)
                if drops is not None:
                    drops.append(msg)
                continue
        kept.append(op)
    return kept


def _drop_off_menu_ops(
    ops: list[DirectorOp],
    drops: list[str] | None,
) -> list[DirectorOp]:
    """Post-pass: drop an op whose type is not on the director's menu.

    Currently that is ``speed`` only (see :data:`MENU_TYPES`).  A post-pass
    rather than a check inside :func:`_parse_op` — like :func:`_apply_keep_cap`
    — so the drop message/logging stays in one place and
    :func:`ops_from_dict` (the hand-edit path) is left uncapped.
    """
    kept: list[DirectorOp] = []
    for op in ops:
        if op.type not in MENU_TYPES:
            msg = (
                f"{op.type!r} op {list(op.lines)} is not an operation the "
                'director may emit; a stretch worth going fast over is a "timelapse", '
                'anything else plays at 1x or is a "cut"'
            )
            logger.warning("Director op dropped: %s", msg)
            if drops is not None:
                drops.append(msg)
            continue
        kept.append(op)
    return kept


def try_parse_director_response(
    response: str,
    num_lines: int,
    drops: list[str] | None = None,
    max_keep_lines: int = 0,
) -> list[DirectorOp] | None:
    """Parse the director LLM response, distinguishing failure from empty.

    Returns ``None`` on a *hard* parse failure (invalid JSON / no ``ops``
    array) so the caller can retry; returns the (possibly empty) validated op
    list otherwise.  A valid ``{"ops": []}`` is a legitimate "no edits" result
    and yields ``[]`` (not ``None``).  Individual malformed ops are skipped
    (logged) rather than aborting the whole list.

    ``max_keep_lines`` (config ``director.max_keep_lines``, ``0`` = no limit)
    drops a ``keep`` op wider than that many lines: dropping it degrades to the
    default behaviour (speech kept, internal silence cut) instead of restoring
    a span's worth of dead air. Applied as a post-pass (:func:`_apply_keep_cap`)
    after every op is parsed.
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

    ops: list[DirectorOp] = []
    for raw in data["ops"]:
        op = _parse_op(raw, num_lines, drops)
        if op is not None:
            ops.append(op)
    return _apply_keep_cap(_drop_off_menu_ops(ops, drops), max_keep_lines, drops)


def parse_director_response(
    response: str, num_lines: int, max_keep_lines: int = 0
) -> list[DirectorOp]:
    """Parse the director LLM response into validated ops.

    Thin wrapper over :func:`try_parse_director_response` that collapses a hard
    parse failure to ``[]`` (backward-compatible).
    """
    return try_parse_director_response(response, num_lines, max_keep_lines=max_keep_lines) or []


def ops_from_dict(data: Any, num_lines: int | None) -> list[DirectorOp]:
    """Load validated ops from a parsed ``_director.json`` dict.

    Same validation as :func:`parse_director_response`; invalid/out-of-range
    ops are skipped.  ``num_lines=None`` skips the range check, for reading
    another video's file where the transcript length is unknown.  The ``max_keep_lines`` cap is deliberately NOT applied
    here: ``_director.json`` is a hand-editable intermediate, so a human who
    writes a wide ``keep`` into it means it.  The cap guards the LLM's output
    only, at generation time.
    """
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return []
    ops: list[DirectorOp] = []
    for raw in data["ops"]:
        op = _parse_op(raw, num_lines)
        if op is not None:
            ops.append(op)
    return ops


def collect_overlay_texts(ops: Sequence[DirectorOp]) -> list[str]:
    """The captions the director placed, in order, deduped.

    These are the moments the edit itself calls out — a mishap, a result, a
    conclusion — which is exactly the register a title or a thumbnail hook
    wants, so they are handed to the LLM verbatim.
    """
    out: list[str] = []
    for op in ops:
        if op.type not in ("overlay", "timelapse"):
            continue
        text = (op.text or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def clean_for_display(edit_lines: list[str]) -> list[str]:
    """Render edit lines as plain readable text for the director.

    Applies ``{{old->new}}`` patches and strips any marker tags so the LLM
    sees clean prose, not edit syntax.
    """
    stripped = [
        CUT_TAG_RE.sub(
            "",
            OVERLAY_TAG_RE.sub("", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))),
        )
        for line in edit_lines
    ]
    return apply_patches_to_lines(stripped)


def format_numbered_transcript(clean_lines: list[str]) -> str:
    """Format clean lines as ``N: text`` (1-based), matching the filter LLM."""
    return "\n".join(f"{i + 1}: {text}" for i, text in enumerate(clean_lines))


def format_numbered_transcript_timed(
    clean_lines: list[str],
    seg_times: list[tuple[float | None, float | None]],
    silences: list[float] | None = None,
) -> str:
    """``N: text  [dur, gap]`` (1-based), gap = time to the next line.

    Per line: ``dur = end - start``; ``gap = next.start - this.end`` (the last
    line has no gap).  When *silences* is given (audio_silence-cut seconds
    inside each line's span, from :func:`nagare_clip.timing.segment_silences`),
    a line with significant internal silence renders
    ``[12.9s speech, 62.9s silence]`` — speech-only duration — so the LLM
    never judges pacing from span time that is mostly already-dropped silence.
    A missing ``start``/``end`` degrades that line's bracket via
    :func:`format_dur_gap` (possibly to no bracket at all).
    """
    out: list[str] = []
    for i, text in enumerate(clean_lines):
        start, end = seg_times[i]
        raw = end - start if start is not None and end is not None else None
        sil = silences[i] if silences is not None and i < len(silences) else None
        dur = max(raw - sil, 0.0) if raw is not None and sil else raw
        gap: float | None = None
        if i + 1 < len(clean_lines):
            nxt_start = seg_times[i + 1][0]
            if end is not None and nxt_start is not None:
                gap = nxt_start - end
        bracket = format_dur_gap(dur, gap, sil)
        out.append(f"{i + 1}: {text}  {bracket}".rstrip())
    return "\n".join(out)


def ops_to_dict(ops: list[DirectorOp]) -> dict[str, Any]:
    """Serialise ops to the ``{stem}_director.json`` shape."""
    out: list[dict[str, Any]] = []
    for op in ops:
        entry: dict[str, Any] = {
            "type": op.type,
            "lines": [op.lines[0], op.lines[1]],
        }
        if op.factor is not None:
            entry["factor"] = op.factor
        if op.text is not None:
            entry["text"] = op.text
        if op.duration is not None:
            entry["duration"] = op.duration
        if op.note:
            entry["note"] = op.note
        out.append(entry)
    return {"ops": out}


def generate_director_ops(
    edit_lines: list[str],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    overview_context: str = "",
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
    seg_times: list[tuple[float | None, float | None]] | None = None,
    gaps: list[Gap] | None = None,
    silences: list[float] | None = None,
) -> list[DirectorOp]:
    """Run the director LLM over the transcript and return validated ops.

    Retries (config ``max_retries``) on an LLM exception or a hard parse
    failure, nudging temperature up each attempt.  A valid empty op list is
    accepted without retry.  Returns ``[]`` after all attempts fail (graceful
    no-op), so the pipeline proceeds with the unedited transcript.

    ``overview_context`` (from the summary/plan stages) is appended to the system
    prompt when non-empty; an empty string leaves the prompt unchanged.

    ``gaps`` (from the gap_context stage) are anchored to the timed transcript
    and inserted as indented, un-numbered ``[silent gap …]`` lines so the
    director can issue a ``keep`` op over the adjacent lines to rescue a gap
    worth keeping. Only applies when ``seg_times`` is also present (the
    annotation needs anchor times); an empty/absent ``gaps`` leaves the user
    content byte-identical to before this feature existed.

    ``silences`` (per-line audio_silence overlap, same length as ``seg_times``)
    splits each bracket into speech/silence; ``None`` keeps the output
    byte-identical.
    """
    clean_lines = clean_for_display(edit_lines)
    max_keep_lines = _max_keep_lines(cfg)
    system_prompt = cfg.get("prompt", "")
    if max_keep_lines > 0:
        system_prompt = f"{system_prompt}\n\n{keep_limit_note(max_keep_lines)}"
    if overview_context:
        system_prompt = f"{system_prompt}\n\n{overview_context}"
    if seg_times is not None and len(seg_times) == len(clean_lines):
        user_content = format_numbered_transcript_timed(clean_lines, seg_times, silences=silences)
        if gaps:
            user_content = annotate_numbered_transcript(user_content, anchor_gaps(gaps, seg_times))
    else:
        user_content = format_numbered_transcript(clean_lines)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
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
        ops = try_parse_director_response(
            response,
            num_lines=len(clean_lines),
            drops=drops,
            max_keep_lines=max_keep_lines,
        )
        if ops is None:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="invalid JSON / no 'ops' array",
                cfg=attempt_cfg,
            )
            logger.warning("Director response unparseable (attempt %d/%d)", attempt + 1, attempts)
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} op(s) dropped: " + "; ".join(drops)
        elif not ops:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
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
        return ops
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("Director: all %d attempt(s) failed; proceeding with no ops", attempts)
    return []
