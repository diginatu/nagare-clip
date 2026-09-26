"""Structured per-call LLM report writer for the LLM stages.

Each stage records every attempt of every LLM call through a :class:`Recorder`.
Records are written as one markdown file per "unit" (a stem, a batch, or a group
of ops) with YAML front-matter plus a per-attempt body holding the full prompt
and response.  A top-level ``index.md`` is rebuilt from that front-matter.

Best-effort: any write failure is logged and swallowed so the report never
breaks the pipeline.  A disabled recorder (the shared :data:`NULL_RECORDER`) is a
no-op, keeping stage functions testable and disabled runs zero-footprint.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

OK = "ok"
OK_EMPTY = "ok-empty"
LLM_ERROR = "llm-error"
UNPARSEABLE = "unparseable"
VERIFY_FAIL = "verify-fail"
DROPPED_ITEMS = "dropped-items"

# Stage display/sort order for the index.
STAGE_ORDER = [
    "text_filter",
    "gap_context",
    "summary",
    "director",
    "guided_edit",
    "publish",
]


@dataclass
class _Attempt:
    attempt: int
    total: int
    temperature: float | None
    model: str
    reasoning_effort: Any
    messages: list[dict[str, str]]
    response: str | None
    error: str | None
    outcome: str
    reason: str
    section: str
    deterministic: bool = False
    usage: dict[str, int] | None = None


# Token-count keys, in the order they are rendered.  ``llm_client.extract_usage``
# produces exactly these names (LiteLLM's normalised spellings for Anthropic's
# cache counters), and a key absent from a provider's usage stays absent here —
# an omitted key means "not measured", a zero means "measured, and it was zero".
USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
)

_USAGE_LABELS = {
    "prompt_tokens": "prompt",
    "completion_tokens": "completion",
    "total_tokens": "total",
    "cache_read_input_tokens": "cache read",
    "cache_creation_input_tokens": "cache write",
    "reasoning_tokens": "reasoning",
}


def _sum_usage(usages: list[dict[str, int] | None]) -> dict[str, int]:
    """Add up the per-attempt usages, keeping only keys someone reported."""
    total: dict[str, int] = {}
    for usage in usages:
        if not usage:
            continue
        for key in USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


def _usage_line(usage: dict[str, int]) -> str:
    parts = [f"{_USAGE_LABELS[k]} {usage[k]}" for k in USAGE_KEYS if k in usage]
    return "**Tokens:** " + ", ".join(parts)


def _slug(unit: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", unit.strip())
    return s.strip("_") or "unit"


def _fence(text: str) -> list[str]:
    return ["```", text, "```"]


class Recorder:
    def __init__(self, stage: str, report_dir: str | Path | None, enabled: bool = True) -> None:
        self.stage = stage
        self.report_dir = Path(report_dir) if report_dir else None
        self.enabled = bool(enabled) and self.report_dir is not None
        self._buffers: dict[str, list[_Attempt]] = {}
        self._started: dict[str, datetime] = {}

    @property
    def _stage_dir(self) -> Path | None:
        return self.report_dir / self.stage if self.report_dir else None

    def clear(self) -> None:
        if not self.enabled:
            return
        assert self._stage_dir is not None
        try:
            if self._stage_dir.exists():
                shutil.rmtree(self._stage_dir)
        except OSError as e:
            logger.warning("llm_report: could not clear %s: %s", self._stage_dir, e)

    def begin(self, unit: str) -> None:
        """Mark the unit's real start, BEFORE its first LLM call.

        Stages record an attempt only after the call returns, so without this
        the start time defaults to first-attempt time and ``duration_ms``
        misses the calls entirely (every report said 0).
        """
        if not self.enabled:
            return
        self._started.setdefault(unit, datetime.now())

    def attempt(
        self,
        *,
        unit: str,
        attempt: int,
        total: int,
        messages: list[dict[str, str]],
        response: str | None = None,
        error: str | None = None,
        outcome: str,
        reason: str = "",
        cfg: dict[str, Any] | None = None,
        section: str = "",
        deterministic: bool = False,
        usage: dict[str, int] | None = None,
    ) -> None:
        # ``CallLLM`` is typed to return a plain ``str``, so ``call_llm`` parks
        # the provider's token counts in a thread-local slot instead of the
        # return value; pop them here rather than making every stage thread an
        # extra argument through.  Imported lazily so importing the report
        # writer alone does not drag in litellm.
        if usage is None:
            from nagare_clip.llm_client import take_last_usage

            usage = take_last_usage()
        if not self.enabled:
            return
        temperature = cfg.get("temperature") if cfg else None
        model = str(cfg.get("model", "")) if cfg else ""
        reasoning_effort = cfg.get("reasoning_effort") if cfg else None
        self._started.setdefault(unit, datetime.now())
        self._buffers.setdefault(unit, []).append(
            _Attempt(
                attempt=attempt,
                total=total,
                temperature=temperature,
                model=model,
                reasoning_effort=reasoning_effort,
                messages=[dict(m) for m in messages],
                response=response,
                error=error,
                outcome=outcome,
                reason=reason,
                section=section,
                deterministic=deterministic,
                usage=usage,
            )
        )

    def flush_unit(self, unit: str, *, outcome: str, reason: str = "") -> None:
        if not self.enabled:
            return
        assert self._stage_dir is not None
        attempts = self._buffers.pop(unit, [])
        started = self._started.pop(unit, datetime.now())
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        # Both walk backwards on the same `a.model` predicate (not
        # `a.reasoning_effort` for the second) so `model`/`reasoning_effort`
        # describe the SAME attempt — the last one with a real LLM call,
        # skipping deterministic attempts
        # (cfg=None -> model="") like guided_edit's span-op verification.
        model = next((a.model for a in reversed(attempts) if a.model), "")
        reasoning_effort = next((a.reasoning_effort for a in reversed(attempts) if a.model), None)
        try:
            self._stage_dir.mkdir(parents=True, exist_ok=True)
            (self._stage_dir / f"{_slug(unit)}.md").write_text(
                _render_unit(
                    self.stage,
                    unit,
                    attempts,
                    outcome,
                    reason,
                    model,
                    reasoning_effort,
                    started.isoformat(timespec="seconds"),
                    duration_ms,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("llm_report: could not write unit %r: %s", unit, e)

    def rebuild_index(self) -> None:
        if not self.enabled:
            return
        rebuild_index(self.report_dir)


def _render_unit(
    stage: str,
    unit: str,
    attempts: list[_Attempt],
    outcome: str,
    reason: str,
    model: str,
    reasoning_effort: Any,
    started_at: str,
    duration_ms: int,
) -> str:
    fm = {
        "stage": stage,
        "unit": unit,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "attempts": len(attempts),
        "outcome": outcome,
        "reason": reason,
        "started_at": started_at,
        "duration_ms": duration_ms,
    }
    # Token counts, summed over the unit's attempts.  Omitted entirely when no
    # attempt measured any, so a provider that reports no usage leaves the
    # front matter exactly as it was before this existed.
    fm.update(_sum_usage([a.usage for a in attempts]))
    out: list[str] = [
        "---",
        yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip(),
        "---",
        "",
        f"# {stage} — {unit}",
        "",
    ]
    for att in attempts:
        head = "## "
        if att.section:
            head += f"[{att.section}] "
        if att.deterministic:
            head += "deterministic"
        else:
            head += f"Attempt {att.attempt + 1}/{att.total}"
            if att.temperature is not None:
                head += f" — temperature {att.temperature}"
        head += f" — {att.outcome}"
        out.append(head)
        out.append("")
        for m in att.messages:
            out.append(f"### {m.get('role', '?')} prompt")
            out.extend(_fence(m.get("content", "")))
        if att.error is not None:
            out.append("### Error")
            out.extend(_fence(att.error))
        else:
            out.append("### Response")
            out.extend(_fence(att.response or ""))
        if att.usage:
            out.append(_usage_line(att.usage))
        if att.reason:
            out.append(f"**Result:** {att.reason}")
        out.append("")
    return "\n".join(out)


def recorder_from_config(
    stage: str, cfg: dict[str, Any], *, override_dir: str | None = None
) -> Recorder:
    general = cfg.get("general", {}) if isinstance(cfg, dict) else {}
    enabled = bool(general.get("llm_report", True))
    report_dir = override_dir or general.get("llm_report_dir", "output/llm_report")
    return Recorder(stage, report_dir, enabled=enabled)


NULL_RECORDER = Recorder("", None, enabled=False)


def _read_front_matter(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _stage_rank(stage: str) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)


def _cell(value: Any) -> str:
    return str(value).replace("|", r"\|").replace("\n", " ")


def _tokens_cell(fm: dict[str, Any]) -> str:
    """``in+out`` for a row, with the cache-read share when there was one.

    Empty when the unit's front matter carries no token counts, so files
    written before usage was recorded (or by a provider that reports none)
    render an empty cell rather than a misleading zero.
    """
    prompt = fm.get("prompt_tokens")
    completion = fm.get("completion_tokens")
    if not isinstance(prompt, int) and not isinstance(completion, int):
        return ""
    cell = f"{prompt or 0}+{completion or 0}"
    cached = fm.get("cache_read_input_tokens")
    if isinstance(cached, int) and cached > 0:
        cell += f" ({cached} cached)"
    return cell


def rebuild_index(report_dir: Any) -> None:
    report_dir = Path(report_dir)
    rows: list[tuple] = []
    try:
        detail_files = sorted(report_dir.glob("*/*.md"))
    except OSError:
        return
    for path in detail_files:
        fm = _read_front_matter(path)
        if fm is None:
            continue
        rows.append((fm, path.relative_to(report_dir)))
    rows.sort(key=lambda r: (_stage_rank(str(r[0].get("stage", ""))), str(r[1])))

    counts: dict[str, int] = {}
    for fm, _ in rows:
        counts[str(fm.get("outcome", ""))] = counts.get(str(fm.get("outcome", "")), 0) + 1
    totals = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no calls recorded"

    lines = [
        "# LLM Report",
        "",
        f"{len(rows)} call(s) — {totals}",
    ]
    # Run-wide token spend, when anything measured it.  Every stage's units are
    # in this index, so this is the whole run's bill in one line.
    run_usage = _sum_usage([fm for fm, _ in rows])
    if run_usage:
        lines.append("")
        lines.append(f"Tokens — {_usage_line(run_usage).removeprefix('**Tokens:** ')}")
    lines += [
        "",
        "| Stage | Unit | Attempts | Outcome | Reason | Tokens | Detail |",
        "|---|---|---|---|---|---|---|",
    ]
    for fm, rel in rows:
        lines.append(
            "| {stage} | {unit} | {attempts} | {outcome} | {reason} | {tokens} "
            "| [detail]({link}) |".format(
                stage=_cell(fm.get("stage", "")),
                unit=_cell(fm.get("unit", "")),
                attempts=_cell(fm.get("attempts", "")),
                outcome=_cell(fm.get("outcome", "")),
                reason=_cell(fm.get("reason", "")),
                tokens=_cell(_tokens_cell(fm)),
                link=str(rel).replace("\\", "/"),
            )
        )
    # Deterministic findings (no LLM call) live in notes/ so they survive the
    # next stage's rebuild of this index.
    for note in sorted(report_dir.glob("notes/*.md")):
        try:
            body = note.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            lines.extend(["", body])

    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("llm_report: could not write index: %s", e)
