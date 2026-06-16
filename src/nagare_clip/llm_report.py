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
from typing import Any, Dict, List, Optional, Union

import yaml

logger = logging.getLogger(__name__)

OK = "ok"
OK_EMPTY = "ok-empty"
LLM_ERROR = "llm-error"
UNPARSEABLE = "unparseable"
VERIFY_FAIL = "verify-fail"
DROPPED_ITEMS = "dropped-items"

# Stage display/sort order for the index.
STAGE_ORDER = ["text_filter", "summary", "plan", "director", "guided_edit"]


@dataclass
class _Attempt:
    attempt: int
    total: int
    temperature: Optional[float]
    model: str
    thinking: Any
    messages: List[Dict[str, str]]
    response: Optional[str]
    error: Optional[str]
    outcome: str
    reason: str
    section: str


def _slug(unit: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", unit.strip())
    return s.strip("_") or "unit"


def _fence(text: str) -> List[str]:
    return ["```", text, "```"]


class Recorder:
    def __init__(
        self, stage: str, report_dir: Optional[Union[str, Path]], enabled: bool = True
    ) -> None:
        self.stage = stage
        self.report_dir = Path(report_dir) if report_dir else None
        self.enabled = bool(enabled) and self.report_dir is not None
        self._buffers: Dict[str, List[_Attempt]] = {}
        self._started: Dict[str, datetime] = {}

    @property
    def _stage_dir(self) -> Optional[Path]:
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

    def attempt(
        self,
        *,
        unit: str,
        attempt: int,
        total: int,
        messages: List[Dict[str, str]],
        response: Optional[str] = None,
        error: Optional[str] = None,
        outcome: str,
        reason: str = "",
        cfg: Optional[Dict[str, Any]] = None,
        section: str = "",
    ) -> None:
        if not self.enabled:
            return
        temperature = cfg.get("temperature") if cfg else None
        model = str(cfg.get("model", "")) if cfg else ""
        thinking = cfg.get("thinking", False) if cfg else False
        self._started.setdefault(unit, datetime.now())
        self._buffers.setdefault(unit, []).append(
            _Attempt(
                attempt=attempt,
                total=total,
                temperature=temperature,
                model=model,
                thinking=thinking,
                messages=[dict(m) for m in messages],
                response=response,
                error=error,
                outcome=outcome,
                reason=reason,
                section=section,
            )
        )

    def flush_unit(self, unit: str, *, outcome: str, reason: str = "") -> None:
        if not self.enabled:
            return
        assert self._stage_dir is not None
        attempts = self._buffers.pop(unit, [])
        started = self._started.pop(unit, datetime.now())
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        model = attempts[-1].model if attempts else ""
        thinking = attempts[-1].thinking if attempts else False
        try:
            self._stage_dir.mkdir(parents=True, exist_ok=True)
            (self._stage_dir / f"{_slug(unit)}.md").write_text(
                _render_unit(
                    self.stage, unit, attempts, outcome, reason, model, thinking,
                    started.isoformat(timespec="seconds"), duration_ms,
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
    attempts: List[_Attempt],
    outcome: str,
    reason: str,
    model: str,
    thinking: Any,
    started_at: str,
    duration_ms: int,
) -> str:
    fm = {
        "stage": stage,
        "unit": unit,
        "model": model,
        "thinking": thinking,
        "attempts": len(attempts),
        "outcome": outcome,
        "reason": reason,
        "started_at": started_at,
        "duration_ms": duration_ms,
    }
    out: List[str] = [
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
        if att.reason:
            out.append(f"**Result:** {att.reason}")
        out.append("")
    return "\n".join(out)


def recorder_from_config(
    stage: str, cfg: Dict[str, Any], *, override_dir: Optional[str] = None
) -> Recorder:
    general = cfg.get("general", {}) if isinstance(cfg, dict) else {}
    enabled = bool(general.get("llm_report", True))
    report_dir = override_dir or general.get("llm_report_dir", "output/llm_report")
    return Recorder(stage, report_dir, enabled=enabled)


NULL_RECORDER = Recorder("", None, enabled=False)


def _read_front_matter(path: Path) -> Optional[Dict[str, Any]]:
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


def rebuild_index(report_dir: Any) -> None:
    report_dir = Path(report_dir)
    rows: List[tuple] = []
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

    counts: Dict[str, int] = {}
    for fm, _ in rows:
        counts[str(fm.get("outcome", ""))] = counts.get(str(fm.get("outcome", "")), 0) + 1
    totals = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no calls recorded"

    lines = [
        "# LLM Report",
        "",
        f"{len(rows)} call(s) — {totals}",
        "",
        "| Stage | Unit | Attempts | Outcome | Reason | Detail |",
        "|---|---|---|---|---|---|",
    ]
    for fm, rel in rows:
        lines.append(
            "| {stage} | {unit} | {attempts} | {outcome} | {reason} | [detail]({link}) |".format(
                stage=_cell(fm.get("stage", "")),
                unit=_cell(fm.get("unit", "")),
                attempts=_cell(fm.get("attempts", "")),
                outcome=_cell(fm.get("outcome", "")),
                reason=_cell(fm.get("reason", "")),
                link=str(rel).replace("\\", "/"),
            )
        )
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("llm_report: could not write index: %s", e)
