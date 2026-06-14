# LLM Stage Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write a structured, reviewable per-call LLM report under `output/llm_report/` recording every attempt's prompt, response, retry count, and outcome across all LLM stages.

**Architecture:** A new `nagare_clip.llm_report` module provides a `Recorder` (one markdown file per "unit" with YAML front-matter + per-attempt bodies) and a `rebuild_index()` that regenerates a top-level `index.md` from that front-matter. Each LLM stage's retry loop takes an optional `recorder` (defaulting to a shared disabled `NULL_RECORDER`, so functions stay testable and disabled runs are zero-footprint) and reports each attempt; parse functions gain an optional `drops` accumulator so the currently-silent per-item warnings surface as a `dropped-items` outcome. Each stage CLI builds the real recorder from config, clears its own subdir, and rebuilds the index.

**Tech Stack:** Python 3, `pyyaml` (already a dependency, used by `config.py`), `pytest`, `uv run`.

**Reference spec:** `docs/superpowers/specs/2026-06-14-llm-stage-report-design.md`

**Canonical `Recorder` API (defined in Task 1, referenced by all later tasks):**

```python
# Outcome constants
OK = "ok"; OK_EMPTY = "ok-empty"; LLM_ERROR = "llm-error"
UNPARSEABLE = "unparseable"; VERIFY_FAIL = "verify-fail"; DROPPED_ITEMS = "dropped-items"

NULL_RECORDER  # shared Recorder("", None, enabled=False)

class Recorder:
    def __init__(self, stage: str, report_dir: Path | str | None, enabled: bool = True): ...
    def clear(self) -> None: ...
    def attempt(self, *, unit: str, attempt: int, total: int,
                messages: list[dict], response: str | None = None,
                error: str | None = None, outcome: str, reason: str = "",
                cfg: dict | None = None, section: str = "") -> None: ...
    def flush_unit(self, unit: str, *, outcome: str, reason: str = "") -> None: ...
    def rebuild_index(self) -> None: ...   # no-op when disabled

def recorder_from_config(stage: str, cfg: dict, *, override_dir: str | None = None) -> Recorder: ...
def rebuild_index(report_dir: Path | str) -> None: ...
```

`attempt()` buffers per `unit`; `flush_unit()` writes `report_dir/<stage>/<slug(unit)>.md`. Front-matter keys: `stage, unit, model, attempts, outcome, reason, started_at, duration_ms`. `attempts` = number of buffered attempt records for the unit (for multi-op `guided_edit` files this is the total LLM calls).

---

### Task 1: `Recorder` core — buffering and unit file writing

**Files:**
- Create: `src/nagare_clip/llm_report.py`
- Test: `tests/test_llm_report.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_report.py
"""Tests for the structured LLM report writer."""

from __future__ import annotations

import yaml

from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    Recorder,
    recorder_from_config,
    rebuild_index,
)


def _front_matter(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    _, fm, _ = text.split("---", 2)
    return yaml.safe_load(fm)


class TestUnitFile:
    def test_flush_writes_front_matter_and_bodies(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER"},
        ]
        rec.attempt(
            unit="my_video", attempt=0, total=2, messages=msgs,
            response="bad json", outcome="unparseable", reason="no ops",
            cfg={"temperature": 0.1, "model": "qwen3.5:30b"},
        )
        rec.attempt(
            unit="my_video", attempt=1, total=2, messages=msgs,
            response='{"ops": []}', outcome=OK_EMPTY,
            cfg={"temperature": 0.3, "model": "qwen3.5:30b"},
        )
        rec.flush_unit("my_video", outcome=OK_EMPTY)

        path = tmp_path / "director" / "my_video.md"
        assert path.exists()
        fm = _front_matter(path)
        assert fm["stage"] == "director"
        assert fm["unit"] == "my_video"
        assert fm["attempts"] == 2
        assert fm["outcome"] == OK_EMPTY
        assert fm["model"] == "qwen3.5:30b"

        body = path.read_text(encoding="utf-8")
        assert "SYS" in body and "USER" in body
        assert "bad json" in body and '{"ops": []}' in body
        assert "temperature 0.1" in body and "temperature 0.3" in body
        assert "no ops" in body  # per-attempt reason rendered

    def test_slug_handles_punctuation(self, tmp_path):
        rec = Recorder("text_filter", tmp_path, enabled=True)
        rec.attempt(
            unit="lines 11-20 (size 10)", attempt=0, total=1,
            messages=[{"role": "user", "content": "x"}],
            response="ok", outcome=OK, cfg={"temperature": 0.0},
        )
        rec.flush_unit("lines 11-20 (size 10)", outcome=OK)
        files = list((tmp_path / "text_filter").glob("*.md"))
        assert len(files) == 1
        # slug is filesystem-safe (no spaces/parens)
        assert " " not in files[0].name and "(" not in files[0].name
        # human-readable unit preserved in front-matter
        assert _front_matter(files[0])["unit"] == "lines 11-20 (size 10)"


class TestDisabled:
    def test_disabled_recorder_writes_nothing(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=False)
        rec.attempt(
            unit="u", attempt=0, total=1, messages=[], outcome=OK,
        )
        rec.flush_unit("u", outcome=OK)
        assert not (tmp_path / "director").exists()

    def test_null_recorder_is_disabled(self, tmp_path):
        NULL_RECORDER.attempt(
            unit="u", attempt=0, total=1, messages=[], outcome=OK,
        )
        NULL_RECORDER.flush_unit("u", outcome=OK)
        # nothing to assert beyond "did not raise"; NULL_RECORDER has no dir
        assert NULL_RECORDER.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nagare_clip.llm_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/nagare_clip/llm_report.py
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        self, stage: str, report_dir: Optional[Any], enabled: bool = True
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
        self._started.setdefault(unit, datetime.now())
        self._buffers.setdefault(unit, []).append(
            _Attempt(
                attempt=attempt,
                total=total,
                temperature=temperature,
                model=model,
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
        attempts = self._buffers.pop(unit, [])
        started = self._started.pop(unit, datetime.now())
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        model = attempts[-1].model if attempts else ""
        try:
            self._stage_dir.mkdir(parents=True, exist_ok=True)
            (self._stage_dir / f"{_slug(unit)}.md").write_text(
                _render_unit(
                    self.stage, unit, attempts, outcome, reason, model,
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
    started_at: str,
    duration_ms: int,
) -> str:
    fm = {
        "stage": stage,
        "unit": unit,
        "model": model,
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
        head = f"## "
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


def rebuild_index(report_dir: Any) -> None:  # noqa: F811 - placeholder, real body in Task 2
    raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_report.py::TestUnitFile tests/test_llm_report.py::TestDisabled -v`
Expected: PASS (the `rebuild_index` placeholder is not exercised by these classes)

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/llm_report.py tests/test_llm_report.py
git commit -m "feat: Recorder core for structured LLM report"
```

---

### Task 2: `rebuild_index` — regenerate index.md from front-matter

**Files:**
- Modify: `src/nagare_clip/llm_report.py` (replace the `rebuild_index` placeholder)
- Test: `tests/test_llm_report.py` (add `TestIndex`)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_llm_report.py
from nagare_clip.llm_report import VERIFY_FAIL


class TestIndex:
    def _write_unit(self, tmp_path, stage, unit, outcome, reason=""):
        rec = Recorder(stage, tmp_path, enabled=True)
        rec.attempt(
            unit=unit, attempt=0, total=1,
            messages=[{"role": "user", "content": "x"}],
            response="y", outcome=outcome, cfg={"temperature": 0.0, "model": "m"},
        )
        rec.flush_unit(unit, outcome=outcome, reason=reason)

    def test_index_lists_all_units_in_stage_order(self, tmp_path):
        self._write_unit(tmp_path, "director", "vid_b", OK)
        self._write_unit(tmp_path, "text_filter", "summary_llm", OK)
        self._write_unit(tmp_path, "guided_edit", "vid_a", DROPPED_ITEMS, "1 op unapplied")

        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text(encoding="utf-8")

        # text_filter row appears before director row (STAGE_ORDER)
        assert index.index("text_filter") < index.index("director")
        assert "summary_llm" in index
        assert "1 op unapplied" in index
        # detail links are relative
        assert "director/vid_b.md" in index
        assert "guided_edit/vid_a.md" in index

    def test_index_is_regenerated_not_appended(self, tmp_path):
        self._write_unit(tmp_path, "director", "vid_a", OK)
        rebuild_index(tmp_path)
        # re-run director with a different outcome (refresh own section)
        Recorder("director", tmp_path, enabled=True).clear()
        self._write_unit(tmp_path, "director", "vid_a", VERIFY_FAIL)
        rebuild_index(tmp_path)

        index = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert index.count("director/vid_a.md") == 1
        assert VERIFY_FAIL in index
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_report.py::TestIndex -v`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Write minimal implementation**

Replace the placeholder `rebuild_index` at the bottom of `src/nagare_clip/llm_report.py` with:

```python
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
```

Also remove the old placeholder `def rebuild_index(...): raise NotImplementedError` and its `# noqa` comment.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_report.py -v`
Expected: PASS (all classes)

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/llm_report.py tests/test_llm_report.py
git commit -m "feat: rebuild_index for LLM report"
```

---

### Task 3: Config keys

**Files:**
- Modify: `src/nagare_clip/config.py:13-16` (the `general` block of `DEFAULTS`)
- Modify: `config.example.yml:6-8`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config.py
def test_llm_report_defaults():
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    assert cfg["general"]["llm_report"] is True
    assert cfg["general"]["llm_report_dir"] == "output/llm_report"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_llm_report_defaults -v`
Expected: FAIL with `KeyError: 'llm_report'`

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/config.py`, change the `general` block:

```python
    "general": {
        "log_level": "INFO",
        "log_file": "",
        "llm_report": True,
        "llm_report_dir": "output/llm_report",
    },
```

In `config.example.yml`, under `general:`:

```yaml
general:
  log_level: INFO   # DEBUG | INFO | WARNING | ERROR | CRITICAL
  log_file: ""      # Path to log file; empty = console only (run_pipeline.sh sets this automatically)
  llm_report: true            # write a per-call LLM report under output/llm_report/
  llm_report_dir: "output/llm_report"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_llm_report_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py
git commit -m "feat: config keys for LLM report"
```

---

### Task 4: Director stage — drops accumulator + recorder + CLI

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py` (`_parse_op`, `try_parse_director_response`, `generate_director_ops`)
- Modify: `src/nagare_clip/director/cli.py`
- Test: `tests/director/test_director_llm.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/director/test_director_llm.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder


def _outcome(tmp_path, stage, unit):
    text = (tmp_path / stage / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)["outcome"]


class TestDirectorRecorder:
    def test_records_unparseable_then_ok(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        fake = _seq_llm(["nonsense", '{"ops": []}'])
        ops = generate_director_ops(
            ["a", "b"], {"max_retries": 2}, call_llm=fake,
            recorder=rec, unit="vid",
        )
        assert ops == []
        assert _outcome(tmp_path, "director", "vid") == "ok-empty"
        body = (tmp_path / "director" / "vid.md").read_text(encoding="utf-8")
        assert "nonsense" in body  # failed attempt's response preserved

    def test_records_dropped_items(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        # one valid op, one with out-of-range lines (dropped)
        resp = '{"ops": [{"type":"cut","lines":[1,1]},{"type":"cut","lines":[9,9]}]}'
        fake = _seq_llm([resp])
        ops = generate_director_ops(
            ["a", "b"], {"max_retries": 0}, call_llm=fake,
            recorder=rec, unit="vid",
        )
        assert len(ops) == 1
        assert _outcome(tmp_path, "director", "vid") == "dropped-items"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/director/test_director_llm.py::TestDirectorRecorder -v`
Expected: FAIL with `TypeError: generate_director_ops() got an unexpected keyword argument 'recorder'`

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/director/director_llm.py`, add the import near the top:

```python
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
```

Change `_parse_op` to accept a `drops` list and append a message wherever it currently logs a drop. Replace the function signature and each drop site:

```python
def _parse_op(
    raw: Any, num_lines: int, drops: Optional[List[str]] = None
) -> Optional[DirectorOp]:
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

    note = str(raw.get("note", "") or "")

    factor: Optional[float] = None
    if op_type == "speed":
        raw_factor = raw.get("factor")
        if not isinstance(raw_factor, (int, float)) or isinstance(raw_factor, bool):
            _drop("speed op missing factor")
            return None
        factor = float(raw_factor)
        if factor <= 0:
            _drop(f"speed factor {factor!r} <= 0")
            return None

    text: Optional[str] = None
    if op_type == "overlay":
        raw_text = raw.get("text")
        if not isinstance(raw_text, str) or raw_text == "":
            _drop("overlay op empty/missing text")
            return None
        text = raw_text

    return DirectorOp(type=op_type, lines=lines, note=note, factor=factor, text=text)
```

Change `try_parse_director_response` to thread `drops` through:

```python
def try_parse_director_response(
    response: str, num_lines: int, drops: Optional[List[str]] = None
) -> Optional[List[DirectorOp]]:
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

    ops: List[DirectorOp] = []
    for raw in data["ops"]:
        op = _parse_op(raw, num_lines, drops)
        if op is not None:
            ops.append(op)
    return ops
```

(`parse_director_response` and `ops_from_dict` keep calling `_parse_op`/`try_parse_director_response` with the default `drops=None` — no change needed.)

Replace `generate_director_ops` with:

```python
def generate_director_ops(
    edit_lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    overview_context: str = "",
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
) -> List[DirectorOp]:
    clean_lines = clean_for_display(edit_lines)
    system_prompt = cfg.get("prompt", "")
    if overview_context:
        system_prompt = f"{system_prompt}\n\n{overview_context}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": format_numbered_transcript(clean_lines)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "Director LLM call failed (attempt %d/%d)",
                attempt + 1, attempts, exc_info=True,
            )
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: List[str] = []
        ops = try_parse_director_response(
            response, num_lines=len(clean_lines), drops=drops
        )
        if ops is None:
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid JSON / no 'ops' array", cfg=attempt_cfg,
            )
            logger.warning(
                "Director response unparseable (attempt %d/%d)", attempt + 1, attempts
            )
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} op(s) dropped: " + "; ".join(drops)
        elif not ops:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=unit, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, reason=reason, cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=outcome, reason=reason)
        return ops
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("Director: all %d attempt(s) failed; proceeding with no ops", attempts)
    return []
```

In `src/nagare_clip/director/cli.py`, wire the recorder. Add the import:

```python
from nagare_clip.llm_report import recorder_from_config
```

Add a `--llm-report-dir` argument in `parse_args` (after `--log-file`):

```python
    parser.add_argument("--llm-report-dir", default=None, dest="llm_report_dir")
```

In `main`, after `director_cfg = cfg["director"]`, build the recorder and clear it; pass it into `generate_director_ops`; rebuild the index at the end:

```python
    recorder = recorder_from_config("director", cfg, override_dir=args.llm_report_dir)
    recorder.clear()

    director_cfg = cfg["director"]
    edit_lines = Path(args.edits_txt).read_text(encoding="utf-8").splitlines()
    output = Path(args.output)
    stem = args.stem or output.stem.replace("_director", "")

    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, writing empty op list")
        ops = []
    else:
        overview_context = _build_overview_context(args)
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(
            edit_lines, director_cfg, overview_context=overview_context,
            recorder=recorder, unit=stem,
        )
        logging.info("director: %d operation(s)", len(ops))
```

At the very end of `main`, after the final `logging.info("director: wrote %s", output)`:

```python
    recorder.rebuild_index()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/director/ -v`
Expected: PASS (new `TestDirectorRecorder` + existing director tests unchanged)

- [ ] **Step 5: Mutation check (repo policy)**

Temporarily change the success branch in `generate_director_ops` so `outcome = OK` always (delete the `if drops:` branch), run `uv run pytest tests/director/test_director_llm.py::TestDirectorRecorder::test_records_dropped_items -v`, confirm it FAILS, then revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/director/ tests/director/test_director_llm.py
git commit -m "feat: director feeds LLM report (drops + retries)"
```

---

### Task 5: Plan stage — drops accumulator + recorder + CLI

**Files:**
- Modify: `src/nagare_clip/plan/plan_llm.py` (`try_parse_plan_response`, `generate_plan`)
- Modify: `src/nagare_clip/plan/cli.py`
- Test: `tests/plan/test_plan_llm.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/plan/test_plan_llm.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


def _ps():
    return ProjectSummary(
        summary="overall",
        parts=[
            PartSummary(stem="v", lines=(1, 2), summary="p1"),
            PartSummary(stem="v", lines=(3, 4), summary="p2"),
        ],
    )


def _outcome(tmp_path, unit):
    text = (tmp_path / "plan" / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)["outcome"]


class TestPlanRecorder:
    def test_records_ok(self, tmp_path):
        rec = Recorder("plan", tmp_path, enabled=True)
        resp = '{"directions":[{"index":1,"direction":"keep"},{"index":2,"direction":"cut"}]}'

        def fake(_m, _c):
            return resp

        out = generate_plan(_ps(), {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert len(out) == 2
        assert _outcome(tmp_path, "plan") == "ok"

    def test_records_dropped_items(self, tmp_path):
        rec = Recorder("plan", tmp_path, enabled=True)
        resp = '{"directions":[{"index":1,"direction":"keep"},{"index":9,"direction":"x"}]}'

        def fake(_m, _c):
            return resp

        out = generate_plan(_ps(), {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert len(out) == 1
        assert _outcome(tmp_path, "plan") == "dropped-items"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/plan/test_plan_llm.py::TestPlanRecorder -v`
Expected: FAIL with `TypeError: generate_plan() got an unexpected keyword argument 'recorder'`

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/plan/plan_llm.py`, add imports:

```python
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
```

Change `try_parse_plan_response` to take `drops`:

```python
def try_parse_plan_response(
    response: str, num_parts: int, drops: Optional[List[str]] = None
) -> Optional[Dict[int, str]]:
    def _drop(msg: str) -> None:
        logger.warning("plan: %s", msg)
        if drops is not None:
            drops.append(msg)

    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("plan: response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("directions"), list):
        logger.warning("plan: response has no 'directions' array; ignoring")
        return None

    out: Dict[int, str] = {}
    for raw in data["directions"]:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            _drop(f"direction dropped, bad index {idx!r}")
            continue
        if not (1 <= idx <= num_parts):
            _drop(f"direction dropped, index {idx!r} out of range")
            continue
        direction = raw.get("direction")
        if not isinstance(direction, str) or direction == "":
            _drop("direction dropped, empty/missing text")
            continue
        out[idx] = direction
    return out
```

Replace `generate_plan`:

```python
def generate_plan(
    project_summary: ProjectSummary,
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "plan",
) -> List[PartDirection]:
    parts = project_summary.parts
    if not parts:
        return []
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": _format_parts_for_plan(project_summary)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "plan: LLM call failed (attempt %d/%d)", attempt + 1, attempts,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: List[str] = []
        mapping = try_parse_plan_response(response, num_parts=len(parts), drops=drops)
        if mapping is None:
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid JSON / no 'directions' array", cfg=attempt_cfg,
            )
            logger.warning(
                "plan: response unparseable (attempt %d/%d)", attempt + 1, attempts
            )
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not mapping:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=unit, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, reason=reason, cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=outcome, reason=reason)
        return [
            PartDirection(stem=p.stem, lines=p.lines, direction=mapping[i + 1])
            for i, p in enumerate(parts)
            if (i + 1) in mapping
        ]
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("plan: all %d attempt(s) failed; no directions", attempts)
    return []
```

In `src/nagare_clip/plan/cli.py`: add `from nagare_clip.llm_report import recorder_from_config`; add `--llm-report-dir` arg; after loading cfg build `recorder = recorder_from_config("plan", cfg, override_dir=args.llm_report_dir)`, call `recorder.clear()`; pass `recorder=recorder` into `generate_plan`; call `recorder.rebuild_index()` at the end of `main`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plan/ -v`
Expected: PASS

- [ ] **Step 5: Mutation check**

Temporarily force `outcome = OK` in the success branch, run `tests/plan/test_plan_llm.py::TestPlanRecorder::test_records_dropped_items`, confirm FAIL, revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/plan/ tests/plan/test_plan_llm.py
git commit -m "feat: plan feeds LLM report (drops + retries)"
```

---

### Task 6: Summary stage — drops accumulator + recorder + CLI

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py` (`_parse_parts_response`, `segment_video`, `generate_project_summary`, `build_summary`)
- Modify: `src/nagare_clip/summary/cli.py`
- Test: `tests/summary/test_summarize.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/summary/test_summarize.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder


def _outcome(tmp_path, unit):
    text = (tmp_path / "summary" / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)["outcome"]


class TestSummaryRecorder:
    def test_segment_records_ok(self, tmp_path):
        rec = Recorder("summary", tmp_path, enabled=True)
        resp = '{"parts":[{"lines":[1,2],"summary":"s"}]}'

        def fake(_m, _c):
            return resp

        parts = segment_video("vid", ["a", "b"], {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert len(parts) == 1
        assert _outcome(tmp_path, "vid") == "ok"

    def test_segment_records_dropped_items(self, tmp_path):
        rec = Recorder("summary", tmp_path, enabled=True)
        resp = '{"parts":[{"lines":[1,2],"summary":"s"},{"lines":[9,9],"summary":"x"}]}'

        def fake(_m, _c):
            return resp

        parts = segment_video("vid", ["a", "b"], {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert len(parts) == 1
        assert _outcome(tmp_path, "vid") == "dropped-items"

    def test_overall_records_ok(self, tmp_path):
        rec = Recorder("summary", tmp_path, enabled=True)

        def fake(_m, _c):
            return '{"summary":"all"}'

        parts = [PartSummary(stem="v", lines=(1, 2), summary="p")]
        out = generate_project_summary(parts, {"max_retries": 0}, call_llm=fake, recorder=rec)
        assert out == "all"
        assert _outcome(tmp_path, "overall") == "ok"
```

(Ensure `PartSummary`, `segment_video`, `generate_project_summary` are imported at the top of the test file — they already are for the existing tests.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/summary/test_summarize.py::TestSummaryRecorder -v`
Expected: FAIL with `TypeError: segment_video() got an unexpected keyword argument 'recorder'`

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/summary/summarize.py`, add imports:

```python
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
```

Change `_parse_parts_response` to take `drops` and append at each drop site (mirroring Task 4/5):

```python
def _parse_parts_response(
    response: str, stem: str, num_lines: int, drops: Optional[List[str]] = None
) -> Optional[List[PartSummary]]:
    def _drop(msg: str) -> None:
        logger.warning("summary: %s", msg)
        if drops is not None:
            drops.append(msg)

    try:
        data = json.loads(_strip_fence(response))
    except (ValueError, TypeError):
        logger.warning("summary: parts response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list):
        logger.warning("summary: parts response has no 'parts' array; ignoring")
        return None

    parts: List[PartSummary] = []
    for raw in data["parts"]:
        if not isinstance(raw, dict):
            continue
        lines = _coerce_lines(raw.get("lines"), num_lines)
        if lines is None:
            _drop(f"part dropped, bad lines {raw.get('lines')!r}")
            continue
        summary = raw.get("summary")
        if not isinstance(summary, str) or summary == "":
            _drop("part dropped, empty/missing summary")
            continue
        parts.append(PartSummary(stem=stem, lines=lines, summary=summary))
    return parts
```

Replace `segment_video`:

```python
def segment_video(
    stem: str,
    clean_lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> List[PartSummary]:
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": format_numbered_transcript(clean_lines)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "summary: segment LLM call failed (attempt %d/%d) for %s",
                attempt + 1, attempts, stem, exc_info=True,
            )
            recorder.attempt(
                unit=stem, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: List[str] = []
        parts = _parse_parts_response(
            response, stem, num_lines=len(clean_lines), drops=drops
        )
        if parts is None:
            recorder.attempt(
                unit=stem, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid JSON / no 'parts' array", cfg=attempt_cfg,
            )
            logger.warning(
                "summary: segment response unparseable (attempt %d/%d) for %s",
                attempt + 1, attempts, stem,
            )
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not parts:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=stem, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, reason=reason, cfg=attempt_cfg,
        )
        recorder.flush_unit(stem, outcome=outcome, reason=reason)
        return parts
    recorder.flush_unit(stem, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("summary: all %d attempt(s) failed for %s; no parts", attempts, stem)
    return []
```

Replace `generate_project_summary` (unit `"overall"`; no per-item drops — overall is a single string):

```python
def generate_project_summary(
    parts: List[PartSummary],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> str:
    if not parts:
        return ""
    messages = [
        {"role": "system", "content": cfg.get("overall_prompt", "")},
        {"role": "user", "content": _format_parts_doc(parts)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "summary: overall LLM call failed (attempt %d/%d)",
                attempt + 1, attempts, exc_info=True,
            )
            recorder.attempt(
                unit="overall", attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        summary = _parse_overall_response(response)
        if summary is None:
            recorder.attempt(
                unit="overall", attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE, reason="no 'summary' string",
                cfg=attempt_cfg,
            )
            logger.warning(
                "summary: overall response unparseable (attempt %d/%d)",
                attempt + 1, attempts,
            )
            continue
        outcome = OK_EMPTY if summary == "" else OK
        recorder.attempt(
            unit="overall", attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, cfg=attempt_cfg,
        )
        recorder.flush_unit("overall", outcome=outcome)
        return summary
    recorder.flush_unit("overall", outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("summary: all %d overall attempt(s) failed; empty summary", attempts)
    return ""
```

Thread the recorder through `build_summary`:

```python
def build_summary(
    parts_input: List[Tuple[str, List[str]]],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> ProjectSummary:
    parts: List[PartSummary] = []
    for stem, clean_lines in parts_input:
        parts.extend(
            segment_video(stem, clean_lines, cfg, call_llm=call_llm, recorder=recorder)
        )
    summary = generate_project_summary(parts, cfg, call_llm=call_llm, recorder=recorder)
    return ProjectSummary(summary=summary, parts=parts)
```

In `src/nagare_clip/summary/cli.py`: add `from nagare_clip.llm_report import recorder_from_config`; add `--llm-report-dir` arg; build `recorder = recorder_from_config("summary", cfg, override_dir=args.llm_report_dir)`, `recorder.clear()`; pass `recorder=recorder` into `build_summary`; `recorder.rebuild_index()` at the end of `main`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/ -v`
Expected: PASS

- [ ] **Step 5: Mutation check**

Force `outcome = OK` in `segment_video`'s success branch, run `tests/summary/test_summarize.py::TestSummaryRecorder::test_segment_records_dropped_items`, confirm FAIL, revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/summary/ tests/summary/test_summarize.py
git commit -m "feat: summary feeds LLM report (drops + retries)"
```

---

### Task 7: Guided_edit stage — per-op sections + recorder + CLI

**Files:**
- Modify: `src/nagare_clip/guided_edit/apply.py` (`apply_ops`)
- Modify: `src/nagare_clip/guided_edit/cli.py`
- Test: `tests/guided_edit/test_apply.py`

`apply_ops` records one file per stem (`unit=stem`), with a `section` per op (`f"op {i}: {op.type} [{a}-{b}]"`). Each op-attempt is recorded; the unit's final outcome is `ok` if all ops applied, else `dropped-items`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/guided_edit/test_apply.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder


def _fm(tmp_path, unit):
    text = (tmp_path / "guided_edit" / f"{unit}.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return _yaml.safe_load(fm)


class TestGuidedEditRecorder:
    def test_records_unapplied_op_as_dropped_items(self, tmp_path):
        rec = Recorder("guided_edit", tmp_path, enabled=True)
        op = DirectorOp(type="keep", lines=(1, 1), note="")
        # call_llm returns the line unchanged → op never reflected → verify fails →
        # all retries exhausted → op unapplied.

        def fake(_messages, _cfg):
            return "1: hello world"

        lines = ["hello world"]
        new_lines, unapplied = apply_ops(
            lines, [op], {"max_retries": 1}, call_llm=fake, recorder=rec, unit="vid",
        )
        assert len(unapplied) == 1
        fm = _fm(tmp_path, "vid")
        assert fm["outcome"] == "dropped-items"
        body = (tmp_path / "guided_edit" / "vid.md").read_text(encoding="utf-8")
        assert "op 0: keep" in body  # section header rendered

    def test_records_applied_op_as_ok(self, tmp_path):
        rec = Recorder("guided_edit", tmp_path, enabled=True)
        op = DirectorOp(type="keep", lines=(1, 1), note="")

        def fake(_messages, _cfg):
            return "1: <keep>hello world</keep>"

        new_lines, unapplied = apply_ops(
            ["hello world"], [op], {"max_retries": 0}, call_llm=fake, recorder=rec, unit="vid",
        )
        assert unapplied == []
        assert _fm(tmp_path, "vid")["outcome"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/guided_edit/test_apply.py::TestGuidedEditRecorder -v`
Expected: FAIL with `TypeError: apply_ops() got an unexpected keyword argument 'recorder'`

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/guided_edit/apply.py`, add imports:

```python
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    VERIFY_FAIL,
    Recorder,
)
```

`_apply_one_op` currently builds `messages` internally. To record the prompt, return it too. Change `_apply_one_op` to also expose the messages — simplest is to extract message-building so the loop has them. Replace `_apply_one_op` and `apply_ops`:

```python
def _build_messages(op: DirectorOp, lines: List[str], cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": build_user_prompt(op, lines)},
    ]


def _apply_one_op(
    lines: List[str],
    op: DirectorOp,
    cfg: Dict[str, Any],
    call_llm: CallLLM,
    messages: List[Dict[str, str]],
) -> Tuple[List[str], str]:
    """Returns (new_lines, raw_response)."""
    a, b = op.lines
    allowed = {a} if a == b else {a, b}
    response = call_llm(messages, cfg)
    returned = _parse_returned_lines(response, allowed)
    new_lines = list(lines)
    for n, text in returned.items():
        new_lines[n - 1] = text
    return new_lines, response


def apply_ops(
    edit_lines: List[str],
    ops: List[DirectorOp],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "guided_edit",
) -> Tuple[List[str], List[Unapplied]]:
    lines = list(edit_lines)
    unapplied: List[Unapplied] = []
    attempts = retry_attempts(cfg)
    for i, op in enumerate(ops):
        section = f"op {i}: {op.type} [{op.lines[0]}-{op.lines[1]}]"
        last_reason = f"{op.type} op not applied"
        applied = False
        for attempt in range(attempts):
            attempt_cfg = cfg_for_attempt(cfg, attempt)
            messages = _build_messages(op, lines, attempt_cfg)
            try:
                candidate, response = _apply_one_op(
                    lines, op, attempt_cfg, call_llm, messages
                )
            except Exception as e:  # noqa: BLE001 - LLM/parse failures are recoverable
                last_reason = f"LLM/apply error: {e}"
                logger.warning(
                    "guided_edit: op %s attempt %d/%d failed: %s",
                    op.type, attempt + 1, attempts, e,
                )
                recorder.attempt(
                    unit=unit, attempt=attempt, total=attempts, messages=messages,
                    error=str(e), outcome=LLM_ERROR, reason=last_reason,
                    cfg=attempt_cfg, section=section,
                )
                continue
            reason = verify_op(lines, candidate, op)
            if reason is None:
                recorder.attempt(
                    unit=unit, attempt=attempt, total=attempts, messages=messages,
                    response=response, outcome=OK, cfg=attempt_cfg, section=section,
                )
                lines = candidate
                applied = True
                break
            last_reason = reason
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=VERIFY_FAIL, reason=reason,
                cfg=attempt_cfg, section=section,
            )
            logger.warning(
                "guided_edit: op %s attempt %d/%d reverted: %s",
                op.type, attempt + 1, attempts, reason,
            )
        if not applied:
            unapplied.append((op, last_reason))
    if unapplied:
        outcome = DROPPED_ITEMS
        reason = f"{len(unapplied)}/{len(ops)} op(s) unapplied"
    else:
        outcome = OK
        reason = f"{len(ops)} op(s) applied"
    recorder.flush_unit(unit, outcome=outcome, reason=reason)
    return lines, unapplied
```

In `src/nagare_clip/guided_edit/cli.py`: add `from nagare_clip.llm_report import recorder_from_config`; add `--llm-report-dir` arg; build `recorder = recorder_from_config("guided_edit", cfg, override_dir=args.llm_report_dir)`, `recorder.clear()`. Derive the stem and pass it:

```python
    stem = output.stem.replace("_edits", "")
    result_lines, unapplied = apply_ops(
        edit_lines, ops, ge_cfg, recorder=recorder, unit=stem
    )
```

Call `recorder.rebuild_index()` at the end of `main`. Note: when `guided_edit.enabled` is false the function isn't called, so no file is written for that stem — acceptable (disabled stage = nothing to report).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/guided_edit/ -v`
Expected: PASS

- [ ] **Step 5: Mutation check**

Force `outcome = OK` unconditionally before `flush_unit` in `apply_ops`, run `tests/guided_edit/test_apply.py::TestGuidedEditRecorder::test_records_unapplied_op_as_dropped_items`, confirm FAIL, revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/guided_edit/ tests/guided_edit/test_apply.py
git commit -m "feat: guided_edit feeds LLM report (per-op sections)"
```

---

### Task 8: text_filter stage — batch retries + summary-LLM + CLI

**Files:**
- Modify: `src/nagare_clip/stage2/llm_filter.py` (`filter_transcript`, `_process_batch`)
- Modify: `src/nagare_clip/stage2/summary_llm.py` (`generate_summary`)
- Modify: `src/nagare_clip/stage2/cli.py`
- Test: `tests/stage2/test_llm_filter.py`, `tests/stage2/test_summary_llm.py`

text_filter does not use `llm_retry`; its batch path recursively halves. Record one unit per `_call_llm` (per batch), `unit=f"lines {a}-{b} (size {s})"` where `a`/`b` are 1-based. The summary-LLM is a single call, `unit="summary_llm"`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/stage2/test_llm_filter.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder
from nagare_clip.stage2.llm_filter import filter_transcript


def _units(tmp_path):
    return sorted(p.name for p in (tmp_path / "text_filter").glob("*.md"))


class TestFilterRecorder:
    def test_records_each_batch(self, tmp_path):
        rec = Recorder("text_filter", tmp_path, enabled=True)

        def fake(_messages, _cfg):
            # echo back the two lines unchanged with their numbers
            return "1: alpha\n2: beta"

        out = filter_transcript(
            ["alpha", "beta"],
            {"batch_size": 10, "retry_on_invalid": False, "model": "m"},
            call_llm=fake, recorder=rec,
        )
        assert out == ["alpha", "beta"]
        files = list((tmp_path / "text_filter").glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        _, fm, _ = text.split("---", 2)
        assert _yaml.safe_load(fm)["outcome"] == "ok"
```

```python
# add to tests/stage2/test_summary_llm.py
import yaml as _yaml

from nagare_clip.llm_report import Recorder
from nagare_clip.stage2.summary_llm import generate_summary


class TestSummaryLLMRecorder:
    def test_records_summary_call(self, tmp_path):
        rec = Recorder("text_filter", tmp_path, enabled=True)

        def fake(_messages, _cfg):
            return '{"summary":"s","keywords":["k"]}'

        out = generate_summary("some text", {"model": "m"}, call_llm=fake, recorder=rec)
        assert out is not None
        text = (tmp_path / "text_filter" / "summary_llm.md").read_text(encoding="utf-8")
        _, fm, _ = text.split("---", 2)
        assert _yaml.safe_load(fm)["outcome"] == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/stage2/test_llm_filter.py::TestFilterRecorder tests/stage2/test_summary_llm.py::TestSummaryLLMRecorder -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'recorder'` (both)

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/stage2/llm_filter.py`, add imports and an injectable `call_llm`:

```python
from nagare_clip.llm_report import DROPPED_ITEMS, LLM_ERROR, NULL_RECORDER, OK, Recorder
```

Change `filter_transcript` to accept `call_llm` and `recorder`, and thread them into `_process_batch`:

```python
def filter_transcript(
    lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm=_call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> List[str]:
    if not lines:
        return []

    batch_size = cfg.get("batch_size", 10)
    batches = _batch_lines(lines, batch_size)
    result = list(lines)

    stats: Dict[int, Dict[str, int]] = defaultdict(lambda: {"total": 0, "succeeded": 0})
    for batch in batches:
        _process_batch(batch, result, cfg, batch_size, stats, call_llm=call_llm, recorder=recorder)

    _log_stats(stats, batch_size)
    return result
```

Change `_process_batch` to take `call_llm`/`recorder`, record one unit per call, and pass them through the recursive retry:

```python
def _process_batch(
    batch: List[Tuple[int, str]],
    result: List[str],
    cfg: Dict[str, Any],
    current_size: int,
    stats: Optional[Dict[int, Dict[str, int]]] = None,
    *,
    call_llm=_call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> None:
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
            batch[0][0] + 1, exc_info=True,
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
        len(failed), new_size, current_size,
    )
    for i in range(0, len(failed), new_size):
        _process_batch(
            failed[i : i + new_size], result, cfg, new_size, stats,
            call_llm=call_llm, recorder=recorder,
        )
```

In `src/nagare_clip/stage2/summary_llm.py`, add `from nagare_clip.llm_report import LLM_ERROR, NULL_RECORDER, OK, UNPARSEABLE, Recorder` and make `generate_summary` injectable + recording:

```python
def generate_summary(
    full_text: str,
    cfg: Dict[str, Any],
    *,
    call_llm=_call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> SummaryResult | None:
    if not full_text.strip():
        return None

    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": full_text},
    ]

    try:
        response = call_llm(messages, cfg)
    except Exception as e:  # noqa: BLE001 - recoverable
        logger.warning("Summary LLM call failed, proceeding without summary", exc_info=True)
        recorder.attempt(
            unit="summary_llm", attempt=0, total=1, messages=messages, error=str(e),
            outcome=LLM_ERROR, reason="LLM call failed", cfg=cfg,
        )
        recorder.flush_unit("summary_llm", outcome=LLM_ERROR, reason="LLM call failed")
        return None

    result = parse_summary_response(response)
    if result is None:
        logger.warning("Failed to parse summary LLM response: %s", response[:200])
        recorder.attempt(
            unit="summary_llm", attempt=0, total=1, messages=messages, response=response,
            outcome=UNPARSEABLE, reason="unparseable summary JSON", cfg=cfg,
        )
        recorder.flush_unit("summary_llm", outcome=UNPARSEABLE, reason="unparseable summary JSON")
        return result
    recorder.attempt(
        unit="summary_llm", attempt=0, total=1, messages=messages, response=response,
        outcome=OK, cfg=cfg,
    )
    recorder.flush_unit("summary_llm", outcome=OK)
    return result
```

In `src/nagare_clip/stage2/cli.py`: add `from nagare_clip.llm_report import recorder_from_config`; add `--llm-report-dir` arg; build `recorder = recorder_from_config("text_filter", cfg, override_dir=args.llm_report_dir)`, `recorder.clear()`. Pass `recorder=recorder` into `generate_summary(...)` and `filter_transcript(...)`. Call `recorder.rebuild_index()` at the very end of `main` (after the write), but only when the LLM path ran — simplest is to always call it; with nothing recorded it just writes an empty index for this stage's section. To avoid wiping a useful index on a disabled run, guard: only `recorder.rebuild_index()` when `s2["use_llm"]` is true. (When `use_llm` is false, no LLM call happens; skip clear+rebuild too.)

Concretely, wrap the recorder lifecycle inside the `else` (LLM-enabled) branch:

```python
    if not s2["use_llm"]:
        logging.info("Stage 2: AI filter disabled, writing edits file")
        result_lines = lines
    else:
        recorder = recorder_from_config("text_filter", cfg, override_dir=args.llm_report_dir)
        recorder.clear()
        logging.info("Stage 2: filtering %d lines with AI", len(lines))
        # ... existing summary-LLM block, but call:
        #     summary_result = generate_summary("\n".join(lines), summary_cfg, recorder=recorder)
        # ... then:
        result_lines = filter_transcript(lines, filter_cfg, recorder=recorder)
        changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
        logging.info("Stage 2: %d/%d lines modified by AI", changes, len(lines))
        recorder.rebuild_index()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/stage2/ -v`
Expected: PASS

- [ ] **Step 5: Mutation check**

Force `outcome = OK` in `_process_batch` (ignore `missing`), run `tests/stage2/test_llm_filter.py::TestFilterRecorder` after adding a quick variant where the fake returns only `"1: alpha"` (one line missing) — confirm a `dropped-items` assertion would fail under the mutation, then revert. (If you don't add the variant, instead mutate `generate_summary` to record `UNPARSEABLE` always and confirm `TestSummaryLLMRecorder` fails.)

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/stage2/ tests/stage2/test_llm_filter.py tests/stage2/test_summary_llm.py
git commit -m "feat: text_filter feeds LLM report (batches + summary-LLM)"
```

---

### Task 9: run_pipeline.sh forwarding + docs + full validation

**Files:**
- Modify: `scripts/run_pipeline.sh`
- Modify: `README.md`
- Modify: `CLAUDE.md` (AGENTS.md — same file)

- [ ] **Step 1: Forward `--llm-report-dir` from run_pipeline.sh (optional but explicit)**

Add a `REPORT_DIR` variable (default derived from output root, e.g. `${OUTPUT_DIR}/llm_report` matching the config default `output/llm_report`) and append `--llm-report-dir "$REPORT_DIR"` to each of the five LLM stage invocations: `stage2.cli`, `summary.cli`, `plan.cli`, `director.cli`, `guided_edit.cli` (alongside their existing `--config`/`--log-file` args). If the script has no single OUTPUT_DIR variable, simply pass the literal `output/llm_report` (the project-root-relative default). This step is cosmetic — the config default already produces the same path; verify with `bash -n scripts/run_pipeline.sh`.

- [ ] **Step 2: Verify the script still parses**

Run: `bash -n scripts/run_pipeline.sh`
Expected: no output (exit 0)

- [ ] **Step 3: Update README.md**

Add a short subsection (near the human-editing/outputs docs) describing the LLM report:

```markdown
### LLM report (`output/llm_report/`)

Every LLM stage (`text_filter`, `summary`, `plan`, `director`, `guided_edit`)
writes a per-call record under `output/llm_report/`: an `index.md` table
(stage, unit, attempts, outcome, reason) linking to per-call detail files under
`<stage>/<unit>.md` that hold the full prompt and raw response for every attempt,
including retries. Outcomes: `ok`, `ok-empty`, `llm-error`, `unparseable`,
`verify-fail`, `dropped-items` (a call that parsed but discarded some items).
Re-running a stage refreshes only that stage's section. Toggle with
`general.llm_report` (default `true`) and relocate with `general.llm_report_dir`.
```

- [ ] **Step 4: Update CLAUDE.md (AGENTS.md)**

Add to "Project Structure" the new module line under `src/nagare_clip/`:

```
  llm_report.py               # Structured per-call LLM report: Recorder + rebuild_index (index.md + per-call <stage>/<unit>.md)
```

Add a bullet to "Current Runtime Quirks":

```
- All LLM stages (`text_filter` incl. its summary-LLM and batch-halving retries,
  `summary`, `plan`, `director`, `guided_edit`) record every attempt's prompt,
  raw response, retry count and outcome via `nagare_clip.llm_report.Recorder`
  into `output/llm_report/` (config `general.llm_report` default true,
  `general.llm_report_dir` default `output/llm_report`). Each stage CLI builds a
  recorder, `clear()`s its own `<stage>/` subdir at start, passes it into the
  stage functions (default `NULL_RECORDER` = no-op, keeps functions testable),
  and `rebuild_index()`s `index.md` from every detail file's YAML front-matter.
  Outcomes: ok / ok-empty / llm-error / unparseable / verify-fail / dropped-items;
  the parse helpers take a `drops` accumulator so the previously silent per-item
  drop warnings surface as `dropped-items` with counts.
```

- [ ] **Step 5: Full test run**

Run: `uv run pytest -q`
Expected: all tests PASS (no regressions in any stage).

- [ ] **Step 6: Commit**

```bash
git add scripts/run_pipeline.sh README.md CLAUDE.md
git commit -m "docs: LLM report in run_pipeline, README, AGENTS"
```

---

## Self-Review Notes

- **Spec coverage:** output layout (Tasks 1-2 + per-stage flush), front-matter format (Task 1), index regeneration + stage-order + dedup-on-rerun (Task 2), `clear()` refresh-own-section (Tasks 4-8 CLIs), config keys on-by-default (Task 3), all 5 stages incl. text_filter batch retries + summary-LLM (Tasks 4-8), outcome vocabulary incl. `dropped-items` via `drops` accumulator (Tasks 4-8), best-effort error swallowing (Task 1 `flush_unit`/`clear` try/except), docs (Task 9). The guided_edit `attempts` ambiguity is resolved: front-matter `attempts` = total LLM calls for the stem (Task 1 definition).
- **Type consistency:** `Recorder`, `NULL_RECORDER`, outcome constants, `attempt()`/`flush_unit()`/`rebuild_index()`/`recorder_from_config()` signatures are identical across all tasks.
- **No placeholders:** every code step shows full function bodies; mutation checks are concrete per repo TDD policy.
