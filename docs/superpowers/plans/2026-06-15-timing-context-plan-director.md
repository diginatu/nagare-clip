# Duration Context for Plan & Director Stages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface calculated durations + in-between gaps to the plan stage (per part) and the director stage (per sentence) so the LLMs can reason about pacing.

**Architecture:** A new pure `timing.py` module formats duration/gap brackets. The summary stage reads each video's WhisperX JSON, computes per-part `start`/`end`, and stores them in `summary.json`; the plan stage renders durations/gaps from those stored times (no JSON access). The director stage reads its own `{stem}.json` and annotates each line of its numbered transcript with `[dur, gap]`. All additions degrade gracefully to current behavior when timing is absent.

**Tech Stack:** Python 3, `uv run pytest`, dataclasses, WhisperX JSON (`segments[].start/end`).

**Spec:** `docs/superpowers/specs/2026-06-15-timing-context-plan-director-design.md`

---

## File Structure

- **Create** `src/nagare_clip/timing.py` — pure helpers: `segment_times`, `format_dur_gap`.
- **Create** `tests/test_timing.py` — unit tests for the above.
- **Modify** `src/nagare_clip/director/director_llm.py` — add `format_numbered_transcript_timed`; add `seg_times` param to `generate_director_ops`.
- **Modify** `src/nagare_clip/director/cli.py` — add `--json`, load segment times, pass through.
- **Modify** `tests/director/test_director_llm.py` — timed formatting + fallback tests.
- **Modify** `tests/director/test_cli.py` — `--json` wiring smoke test.
- **Modify** `src/nagare_clip/summary/summarize.py` — `PartSummary.start/end`, dict round-trip, `_attach_part_times`, `build_summary(seg_times_by_stem=...)`.
- **Modify** `src/nagare_clip/summary/cli.py` — repeated `--json`, build `seg_times_by_stem`.
- **Modify** `tests/summary/test_summarize.py` — part-time attachment + dict round-trip.
- **Modify** `tests/summary/test_cli.py` — `--json` wiring smoke test.
- **Modify** `src/nagare_clip/plan/plan_llm.py` — render dur/gap in `_format_parts_for_plan`.
- **Modify** `tests/plan/test_plan_llm.py` — rendering tests.
- **Modify** `scripts/run_pipeline.sh` — pass `--json` to summary and director stages.
- **Modify** docs: `README.md`, `plan.md`, `CLAUDE.md`.

---

## Task 1: `timing.py` — pure timing helpers

**Files:**
- Create: `src/nagare_clip/timing.py`
- Test: `tests/test_timing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_timing.py`:

```python
"""Tests for the pure timing helpers (no I/O, no network)."""

from __future__ import annotations

from nagare_clip.timing import format_dur_gap, segment_times


class TestSegmentTimes:
    def test_extracts_start_end_per_segment(self):
        data = {"segments": [
            {"start": 1.0, "end": 3.5, "text": "a"},
            {"start": 4.0, "end": 6.0, "text": "b"},
        ]}
        assert segment_times(data) == [(1.0, 3.5), (4.0, 6.0)]

    def test_missing_keys_become_none(self):
        data = {"segments": [{"text": "a"}, {"start": 2.0}]}
        assert segment_times(data) == [(None, None), (2.0, None)]

    def test_no_segments_key(self):
        assert segment_times({}) == []


class TestFormatDurGap:
    def test_dur_none_is_empty(self):
        assert format_dur_gap(None, 0.8) == ""

    def test_gap_none_shows_dur_only(self):
        assert format_dur_gap(4.2, None) == "[4.2s]"

    def test_dur_and_gap(self):
        assert format_dur_gap(4.24, 0.81) == "[4.2s, gap 0.8s]"

    def test_negative_gap_clamped_to_zero(self):
        assert format_dur_gap(4.2, -0.5) == "[4.2s, gap 0.0s]"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_timing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.timing'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/nagare_clip/timing.py`:

```python
"""Pure timing helpers shared by the plan and director stages.

Extract per-segment times from a WhisperX JSON and render a compact
``[dur, gap]`` bracket.  No I/O, no internal imports — safe to import anywhere.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def segment_times(
    json_data: Dict[str, Any]
) -> List[Tuple[Optional[float], Optional[float]]]:
    """Return ``(start, end)`` per WhisperX segment (``None`` when missing)."""
    out: List[Tuple[Optional[float], Optional[float]]] = []
    for seg in json_data.get("segments", []):
        if not isinstance(seg, dict):
            out.append((None, None))
            continue
        start = seg.get("start")
        end = seg.get("end")
        start = float(start) if isinstance(start, (int, float)) else None
        end = float(end) if isinstance(end, (int, float)) else None
        out.append((start, end))
    return out


def format_dur_gap(dur: Optional[float], gap: Optional[float]) -> str:
    """Compact bracket: ``[4.2s, gap 0.8s]``.

    - ``dur is None`` -> ``""`` (no bracket at all).
    - ``gap is None`` -> ``"[4.2s]"``.
    - negative ``gap`` is clamped to ``0.0``.
    """
    if dur is None:
        return ""
    if gap is None:
        return f"[{dur:.1f}s]"
    if gap < 0:
        gap = 0.0
    return f"[{dur:.1f}s, gap {gap:.1f}s]"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_timing.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Mutation-catch verification**

Temporarily change `return f"[{dur:.1f}s, gap {gap:.1f}s]"` to drop the clamp
(`# if gap < 0: gap = 0.0`), run `uv run pytest tests/test_timing.py -v`,
confirm `test_negative_gap_clamped_to_zero` FAILS, then revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/timing.py tests/test_timing.py
git commit -m "feat: add timing helpers (segment_times, format_dur_gap)"
```

---

## Task 2: Director timed numbered transcript

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py`
- Test: `tests/director/test_director_llm.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/director/test_director_llm.py`. First extend the import block at
the top to include the new function:

```python
from nagare_clip.director.director_llm import (
    DirectorOp,
    clean_for_display,
    format_numbered_transcript,
    format_numbered_transcript_timed,
    generate_director_ops,
    ops_to_dict,
    parse_director_response,
    try_parse_director_response,
)
```

Then add a new test class:

```python
class TestTimedTranscript:
    def test_annotates_dur_and_gap_last_line_no_gap(self):
        seg = [(1.0, 3.0), (4.0, 6.5)]  # gap after line1 = 1.0s
        out = format_numbered_transcript_timed(["あい", "うえ"], seg)
        assert out == "1: あい  [2.0s, gap 1.0s]\n2: うえ  [2.5s]"

    def test_missing_times_degrade_to_plain_line(self):
        seg = [(None, None), (4.0, 6.0)]
        out = format_numbered_transcript_timed(["あ", "い"], seg)
        # line1 has no dur -> no bracket (trailing spaces stripped); line2 last -> dur only
        assert out == "1: あ\n2: い  [2.0s]"

    def test_generate_uses_timed_format_when_seg_times_given(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(
            ["あ", "い"], {"prompt": "P"}, call_llm=fake_llm,
            seg_times=[(1.0, 3.0), (4.0, 6.5)],
        )
        assert captured["user"] == "1: あ  [2.0s, gap 1.0s]\n2: い  [2.5s]"

    def test_generate_falls_back_byte_identical_without_seg_times(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(["あ", "い"], {"prompt": "P"}, call_llm=fake_llm)
        assert captured["user"] == "1: あ\n2: い"

    def test_generate_falls_back_on_length_mismatch(self):
        captured = {}

        def fake_llm(messages, cfg):
            captured["user"] = messages[1]["content"]
            return '{"ops": []}'

        generate_director_ops(
            ["あ", "い"], {"prompt": "P"}, call_llm=fake_llm,
            seg_times=[(1.0, 3.0)],  # only 1 entry for 2 lines
        )
        assert captured["user"] == "1: あ\n2: い"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/director/test_director_llm.py::TestTimedTranscript -v`
Expected: FAIL — `ImportError: cannot import name 'format_numbered_transcript_timed'`.

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/director/director_llm.py`, add the import near the top
(after the existing `from nagare_clip.stage3.sync_json import (...)` block):

```python
from nagare_clip.timing import format_dur_gap
```

Add this function directly below the existing `format_numbered_transcript`
(around line 195):

```python
def format_numbered_transcript_timed(
    clean_lines: List[str],
    seg_times: List[Tuple[Optional[float], Optional[float]]],
) -> str:
    """``N: text  [dur, gap]`` (1-based), gap = time to the next line.

    Per line: ``dur = end - start``; ``gap = next.start - this.end`` (the last
    line has no gap).  A missing ``start``/``end`` degrades that line's bracket
    via :func:`format_dur_gap` (possibly to no bracket at all).
    """
    out: List[str] = []
    for i, text in enumerate(clean_lines):
        start, end = seg_times[i]
        dur = end - start if start is not None and end is not None else None
        gap: Optional[float] = None
        if i + 1 < len(clean_lines):
            nxt_start = seg_times[i + 1][0]
            if end is not None and nxt_start is not None:
                gap = nxt_start - end
        bracket = format_dur_gap(dur, gap)
        out.append(f"{i + 1}: {text}  {bracket}".rstrip())
    return "\n".join(out)
```

Note: `Optional` and `Tuple` are already imported at the top of this module
(`from typing import Any, Callable, Dict, List, Optional, Tuple`).

Then change `generate_director_ops`'s signature and body. Replace the signature:

```python
def generate_director_ops(
    edit_lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    overview_context: str = "",
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
    seg_times: Optional[List[Tuple[Optional[float], Optional[float]]]] = None,
) -> List[DirectorOp]:
```

And replace the message-building block (currently lines ~234-241):

```python
    clean_lines = clean_for_display(edit_lines)
    system_prompt = cfg.get("prompt", "")
    if overview_context:
        system_prompt = f"{system_prompt}\n\n{overview_context}"
    if seg_times is not None and len(seg_times) == len(clean_lines):
        user_content = format_numbered_transcript_timed(clean_lines, seg_times)
    else:
        user_content = format_numbered_transcript(clean_lines)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/director/test_director_llm.py -v`
Expected: PASS (new `TestTimedTranscript` + all pre-existing director tests).

- [ ] **Step 5: Mutation-catch verification**

Temporarily change `gap = nxt_start - end` to `gap = end - nxt_start`, run
`uv run pytest tests/director/test_director_llm.py::TestTimedTranscript -v`,
confirm `test_annotates_dur_and_gap_last_line_no_gap` FAILS (gap sign wrong),
then revert. Also temporarily change the fallback condition to
`if seg_times is not None:` (drop the length check), run the suite, confirm
`test_generate_falls_back_on_length_mismatch` FAILS (IndexError / wrong output),
then revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/director/director_llm.py tests/director/test_director_llm.py
git commit -m "feat: per-sentence durations in director numbered transcript"
```

---

## Task 3: Director CLI `--json` wiring

**Files:**
- Modify: `src/nagare_clip/director/cli.py`
- Test: `tests/director/test_cli.py`

- [ ] **Step 1: Write the failing test**

First inspect the existing CLI test to match its invocation style:

Run: `sed -n '1,60p' tests/director/test_cli.py`

Add a test that runs the director CLI (enabled) with `--json` and asserts the
recorded prompt contains a duration bracket. Use the same subprocess/monkeypatch
pattern already used in that file. Concretely, append:

```python
def test_cli_passes_seg_times_from_json(tmp_path, monkeypatch):
    """With --json + director enabled, the user prompt is timed."""
    import json as _json

    from nagare_clip.director import cli as director_cli

    edits = tmp_path / "v_edits.txt"
    edits.write_text("あ\nい\n", encoding="utf-8")
    js = tmp_path / "v.json"
    js.write_text(
        _json.dumps({"segments": [
            {"start": 1.0, "end": 3.0, "text": "あ"},
            {"start": 4.0, "end": 6.5, "text": "い"},
        ]}),
        encoding="utf-8",
    )
    out = tmp_path / "v_director.json"

    captured = {}

    def fake_call_llm(messages, cfg):
        captured["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(
        "nagare_clip.director.director_llm._call_llm", fake_call_llm
    )

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("director:\n  enabled: true\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        [
            "director",
            "--edits-txt", str(edits),
            "--output", str(out),
            "--json", str(js),
            "--stem", "v",
            "--config", str(cfg_file),
        ],
    )
    director_cli.main()
    assert "[2.0s, gap 1.0s]" in captured["user"]
```

Adapt the monkeypatch target / config-enabling to whatever pattern the existing
tests in this file already use (check Step 1's `sed` output — if they enable the
stage differently or patch `generate_director_ops`, follow that instead).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/director/test_cli.py::test_cli_passes_seg_times_from_json -v`
Expected: FAIL — argparse rejects unknown `--json` (`SystemExit`).

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/director/cli.py`:

Add the import near the other imports:

```python
from nagare_clip.timing import segment_times
```

Add the argument in `parse_args()` (after `--stem`):

```python
    parser.add_argument(
        "--json",
        dest="json",
        default=None,
        help="This video's WhisperX {stem}.json (per-sentence timing)",
    )
```

In `main()`, inside the `else:` branch (director enabled), load segment times
before calling `generate_director_ops` and pass them through. Replace the
`else:` body so it reads:

```python
    else:
        overview_context = _build_overview_context(args)
        seg_times = None
        if args.json and Path(args.json).is_file():
            try:
                seg_times = segment_times(
                    json.loads(Path(args.json).read_text(encoding="utf-8"))
                )
            except (ValueError, OSError):
                logging.warning("director: could not read --json %s", args.json)
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(
            edit_lines, director_cfg, overview_context=overview_context,
            recorder=recorder, unit=stem, seg_times=seg_times,
        )
        logging.info("director: %d operation(s)", len(ops))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/director/test_cli.py -v`
Expected: PASS (new test + pre-existing director CLI tests).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/director/cli.py tests/director/test_cli.py
git commit -m "feat: director CLI --json feeds per-sentence timing"
```

---

## Task 4: Summary part-time storage

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py`
- Test: `tests/summary/test_summarize.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/summary/test_summarize.py`:

```python
class TestPartTimes:
    def test_build_summary_attaches_part_times(self):
        resp = '{"parts": [{"lines": [1, 2], "summary": "intro"}]}'
        overall = '{"summary": "S"}'
        seg_times = {"v": [(1.0, 3.0), (4.0, 6.5)]}
        project = build_summary(
            [("v", ["あ", "い"])],
            {"prompt": "p", "overall_prompt": "o"},
            call_llm=_seq_llm([resp, overall]),
            seg_times_by_stem=seg_times,
        )
        p = project.parts[0]
        assert p.start == 1.0 and p.end == 6.5

    def test_build_summary_without_seg_times_leaves_none(self):
        resp = '{"parts": [{"lines": [1, 2], "summary": "intro"}]}'
        overall = '{"summary": "S"}'
        project = build_summary(
            [("v", ["あ", "い"])],
            {"prompt": "p", "overall_prompt": "o"},
            call_llm=_seq_llm([resp, overall]),
        )
        assert project.parts[0].start is None
        assert project.parts[0].end is None

    def test_to_dict_omits_none_times(self):
        ps = ProjectSummary(
            summary="S",
            parts=[PartSummary(stem="v", lines=(1, 2), summary="x")],
        )
        d = summary_to_dict(ps)
        assert "start" not in d["parts"][0]
        assert "end" not in d["parts"][0]

    def test_to_dict_includes_times_when_set(self):
        ps = ProjectSummary(
            summary="S",
            parts=[PartSummary(stem="v", lines=(1, 2), summary="x",
                               start=1.0, end=6.5)],
        )
        d = summary_to_dict(ps)
        assert d["parts"][0]["start"] == 1.0
        assert d["parts"][0]["end"] == 6.5

    def test_from_dict_round_trip_times(self):
        data = {"summary": "S", "parts": [
            {"stem": "v", "lines": [1, 2], "summary": "x",
             "start": 1.0, "end": 6.5}]}
        ps = summary_from_dict(data)
        assert ps.parts[0].start == 1.0 and ps.parts[0].end == 6.5

    def test_from_dict_missing_times_are_none(self):
        data = {"summary": "S", "parts": [
            {"stem": "v", "lines": [1, 2], "summary": "x"}]}
        ps = summary_from_dict(data)
        assert ps.parts[0].start is None and ps.parts[0].end is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/summary/test_summarize.py::TestPartTimes -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'start'` (PartSummary has no `start`) and `build_summary()` has no `seg_times_by_stem`.

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/summary/summarize.py`:

Extend `PartSummary`:

```python
@dataclass
class PartSummary:
    stem: str
    lines: Tuple[int, int]  # 1-based inclusive (start, end)
    summary: str
    start: Optional[float] = None  # part start time (s), from WhisperX segments
    end: Optional[float] = None    # part end time (s)
```

Add a helper above `build_summary`:

```python
def _attach_part_times(
    part: PartSummary,
    seg_times: Optional[List[Tuple[Optional[float], Optional[float]]]],
) -> None:
    """Set ``part.start``/``part.end`` from segment times for its line range."""
    if not seg_times:
        return
    a, b = part.lines
    if not (1 <= a <= b <= len(seg_times)):
        return
    part.start = seg_times[a - 1][0]
    part.end = seg_times[b - 1][1]
```

Change `build_summary` signature and body:

```python
def build_summary(
    parts_input: List[Tuple[str, List[str]]],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    seg_times_by_stem: Optional[
        Dict[str, List[Tuple[Optional[float], Optional[float]]]]
    ] = None,
) -> ProjectSummary:
    """Map (``segment_video`` per video) then reduce (``generate_project_summary``)."""
    parts: List[PartSummary] = []
    for stem, clean_lines in parts_input:
        parts.extend(
            segment_video(stem, clean_lines, cfg, call_llm=call_llm, recorder=recorder)
        )
    if seg_times_by_stem:
        for p in parts:
            _attach_part_times(p, seg_times_by_stem.get(p.stem))
    summary = generate_project_summary(parts, cfg, call_llm=call_llm, recorder=recorder)
    return ProjectSummary(summary=summary, parts=parts)
```

Update `summary_to_dict` to include times only when set:

```python
def summary_to_dict(ps: ProjectSummary) -> Dict[str, Any]:
    parts: List[Dict[str, Any]] = []
    for p in ps.parts:
        entry: Dict[str, Any] = {
            "stem": p.stem,
            "lines": [p.lines[0], p.lines[1]],
            "summary": p.summary,
        }
        if p.start is not None:
            entry["start"] = p.start
        if p.end is not None:
            entry["end"] = p.end
        parts.append(entry)
    return {"summary": ps.summary, "parts": parts}
```

Update `summary_from_dict`'s part loop to read times. Replace the inner part
construction (currently builds `PartSummary(stem=stem, lines=lines, summary=s)`):

```python
            stem = raw.get("stem")
            lines = _coerce_pair(raw.get("lines"))
            s = raw.get("summary")
            if not isinstance(stem, str) or lines is None or not isinstance(s, str):
                continue
            start = raw.get("start")
            end = raw.get("end")
            start = float(start) if isinstance(start, (int, float)) else None
            end = float(end) if isinstance(end, (int, float)) else None
            parts.append(
                PartSummary(stem=stem, lines=lines, summary=s, start=start, end=end)
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/summary/test_summarize.py -v`
Expected: PASS (new `TestPartTimes` + all pre-existing summary tests).

- [ ] **Step 5: Mutation-catch verification**

Temporarily change `_attach_part_times` to `part.end = seg_times[b - 1][0]`
(end uses start), run `uv run pytest tests/summary/test_summarize.py::TestPartTimes -v`,
confirm `test_build_summary_attaches_part_times` FAILS, then revert. Also
temporarily make `summary_to_dict` always write `start`, run the suite, confirm
`test_to_dict_omits_none_times` FAILS, then revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/summary/summarize.py tests/summary/test_summarize.py
git commit -m "feat: store per-part start/end times in summary"
```

---

## Task 5: Summary CLI `--json` wiring

**Files:**
- Modify: `src/nagare_clip/summary/cli.py`
- Test: `tests/summary/test_cli.py`

- [ ] **Step 1: Write the failing test**

First inspect the existing test style:

Run: `sed -n '1,60p' tests/summary/test_cli.py`

Add a test that runs the summary CLI (enabled) with one `--edits-txt` and one
matching `--json`, then asserts the written `summary.json` part carries `start`
and `end`. Follow the existing file's monkeypatch/config-enabling pattern.
Concretely:

```python
def test_cli_json_populates_part_times(tmp_path, monkeypatch):
    import json as _json

    from nagare_clip.summary import cli as summary_cli

    edits = tmp_path / "v_edits.txt"
    edits.write_text("あ\nい\n", encoding="utf-8")
    js = tmp_path / "v.json"
    js.write_text(
        _json.dumps({"segments": [
            {"start": 1.0, "end": 3.0}, {"start": 4.0, "end": 6.5}]}),
        encoding="utf-8",
    )
    out = tmp_path / "summary.json"

    seq = iter([
        '{"parts": [{"lines": [1, 2], "summary": "intro"}]}',
        '{"summary": "S"}',
    ])

    def fake_call_llm(messages, cfg):
        return next(seq)

    monkeypatch.setattr("nagare_clip.summary.summarize._call_llm", fake_call_llm)

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("summary:\n  enabled: true\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", [
        "summary",
        "--edits-txt", str(edits),
        "--json", str(js),
        "--output", str(out),
        "--config", str(cfg_file),
    ])
    summary_cli.main()

    data = _json.loads(out.read_text(encoding="utf-8"))
    assert data["parts"][0]["start"] == 1.0
    assert data["parts"][0]["end"] == 6.5
```

Adapt the `_call_llm` monkeypatch target / enabling to the existing file's
pattern if it differs.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/summary/test_cli.py::test_cli_json_populates_part_times -v`
Expected: FAIL — argparse rejects unknown `--json` (`SystemExit`).

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/summary/cli.py`:

Add the import:

```python
from nagare_clip.timing import segment_times
```

Add the argument in `parse_args()` (after `--edits-txt`):

```python
    parser.add_argument(
        "--json",
        action="append",
        default=None,
        dest="json",
        help="WhisperX {stem}.json for per-part timing (repeat, matched by stem)",
    )
```

In `main()`, inside the enabled `else:` branch, build `seg_times_by_stem` from
the `--json` paths (keyed by file stem) and pass it to `build_summary`. Replace
the `else:` body:

```python
    else:
        parts_input = []
        for raw in args.edits_txt:
            path = Path(raw)
            stem = _stem_from_edits(path)
            clean_lines = clean_for_display(
                path.read_text(encoding="utf-8").splitlines()
            )
            parts_input.append((stem, clean_lines))
        seg_times_by_stem = {}
        for raw in args.json or []:
            jpath = Path(raw)
            if jpath.is_file():
                try:
                    seg_times_by_stem[jpath.stem] = segment_times(
                        json.loads(jpath.read_text(encoding="utf-8"))
                    )
                except (ValueError, OSError):
                    logging.warning("summary: could not read --json %s", jpath)
        logging.info("summary: analysing %d video(s) with LLM", len(parts_input))
        project = build_summary(
            parts_input, summary_cfg, recorder=recorder,
            seg_times_by_stem=seg_times_by_stem or None,
        )
        logging.info(
            "summary: %d part(s) across %d video(s)",
            len(project.parts),
            len(parts_input),
        )
```

Note: `jpath.stem` (e.g. `v.json` -> `v`) matches `_stem_from_edits`
(`v_edits.txt` -> `v`), so the same stem keys both maps.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/summary/test_cli.py -v`
Expected: PASS (new test + pre-existing summary CLI tests).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/summary/cli.py tests/summary/test_cli.py
git commit -m "feat: summary CLI --json populates per-part timing"
```

---

## Task 6: Plan stage renders part durations + gaps

**Files:**
- Modify: `src/nagare_clip/plan/plan_llm.py`
- Test: `tests/plan/test_plan_llm.py`

- [ ] **Step 1: Write the failing test**

First inspect the existing test to confirm `_format_parts_for_plan` import / usage:

Run: `grep -n "_format_parts_for_plan\|import" tests/plan/test_plan_llm.py`

Add a test class (import `_format_parts_for_plan`, `ProjectSummary`,
`PartSummary` as needed — `PartSummary`/`ProjectSummary` come from
`nagare_clip.summary.summarize`):

```python
from nagare_clip.plan.plan_llm import _format_parts_for_plan
from nagare_clip.summary.summarize import PartSummary, ProjectSummary


class TestFormatPartsTiming:
    def test_same_stem_gap_and_last_part_no_gap(self):
        ps = ProjectSummary(summary="", parts=[
            PartSummary("v", (1, 2), "intro", start=0.0, end=10.0),
            PartSummary("v", (3, 4), "demo", start=11.5, end=19.5),
        ])
        out = _format_parts_for_plan(ps)
        assert "1: v [1-2] [10.0s, gap 1.5s] — intro" in out
        assert "2: v [3-4] [8.0s] — demo" in out

    def test_cross_video_boundary_has_no_gap(self):
        ps = ProjectSummary(summary="", parts=[
            PartSummary("a", (1, 2), "x", start=0.0, end=10.0),
            PartSummary("b", (1, 2), "y", start=2.0, end=8.0),
        ])
        out = _format_parts_for_plan(ps)
        # part 1 is last of stem "a" -> dur only, no gap into "b"
        assert "1: a [1-2] [10.0s] — x" in out
        assert "2: b [1-2] [6.0s] — y" in out

    def test_missing_times_no_bracket(self):
        ps = ProjectSummary(summary="", parts=[
            PartSummary("v", (1, 2), "intro"),
        ])
        out = _format_parts_for_plan(ps)
        assert out == "1: v [1-2] — intro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/plan/test_plan_llm.py::TestFormatPartsTiming -v`
Expected: FAIL — assertions mismatch (current output has no bracket).

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/plan/plan_llm.py`, add the import near the top:

```python
from nagare_clip.timing import format_dur_gap
```

Replace `_format_parts_for_plan`:

```python
def _format_parts_for_plan(project_summary: ProjectSummary) -> str:
    lines: List[str] = []
    if project_summary.summary:
        lines.append(f"Overall: {project_summary.summary}")
        lines.append("")
    parts = project_summary.parts
    for i, p in enumerate(parts):
        dur = p.end - p.start if p.start is not None and p.end is not None else None
        gap: Optional[float] = None
        if i + 1 < len(parts):
            nxt = parts[i + 1]
            if (
                nxt.stem == p.stem
                and p.end is not None
                and nxt.start is not None
            ):
                gap = nxt.start - p.end
        bracket = format_dur_gap(dur, gap)
        prefix = f"{i + 1}: {p.stem} [{p.lines[0]}-{p.lines[1]}]"
        head = f"{prefix} {bracket}" if bracket else prefix
        lines.append(f"{head} — {p.summary}")
    return "\n".join(lines)
```

Note: `Optional` is already imported (`from typing import ... Optional ...`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/plan/test_plan_llm.py -v`
Expected: PASS (new `TestFormatPartsTiming` + all pre-existing plan tests).

- [ ] **Step 5: Mutation-catch verification**

Temporarily drop the `nxt.stem == p.stem` condition (compute gap across stems),
run `uv run pytest tests/plan/test_plan_llm.py::TestFormatPartsTiming -v`,
confirm `test_cross_video_boundary_has_no_gap` FAILS, then revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/plan/plan_llm.py tests/plan/test_plan_llm.py
git commit -m "feat: plan stage renders part durations and in-between gaps"
```

---

## Task 7: Pipeline wiring (`run_pipeline.sh`)

**Files:**
- Modify: `scripts/run_pipeline.sh`

- [ ] **Step 1: Add `--json` to the summary stage invocation**

In the summary block (around line 381-390), build per-stem `--json` args
alongside the existing `--edits-txt`. Replace the loop + invocation:

```bash
  EDITS_ARGS=()
  for STEM in "${ALL_STEMS[@]}"; do
    EDITS_ARGS+=(--edits-txt "${STAGE3_DIR}/${STEM}_edits.txt")
    EDITS_ARGS+=(--json "${STAGE1_DIR}/${STEM}.json")
  done
  uv run --project "$PROJECT_ROOT" python -m nagare_clip.summary.cli \
    "${EDITS_ARGS[@]}" \
    --output "${SUMMARY_DIR}/summary.json" \
    "${CONFIG_ARGS[@]}" \
    --log-file "$LOG_FILE" \
    --llm-report-dir "$LLM_REPORT_DIR"
```

- [ ] **Step 2: Add `--json` to the director stage invocation**

In the director block (around line 426-435), add the `--json` flag:

```bash
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.director.cli \
      --edits-txt "${STAGE3_DIR}/${STEM}_edits.txt" \
      --output "${DIRECTOR_DIR}/${STEM}_director.json" \
      --summary "${SUMMARY_DIR}/summary.json" \
      --plan "${PLAN_DIR}/plan.json" \
      --stem "${STEM}" \
      --json "${STAGE1_DIR}/${STEM}.json" \
      "${CONFIG_ARGS[@]}" \
      --log-file "$LOG_FILE" \
      --llm-report-dir "$LLM_REPORT_DIR" \
      "${REPORT_KEEP_DIR[@]}"
```

- [ ] **Step 3: Syntax-check the script**

Run: `bash -n scripts/run_pipeline.sh`
Expected: no output (exit 0).

- [ ] **Step 4: Commit**

```bash
git add scripts/run_pipeline.sh
git commit -m "feat: pass --json to summary and director stages in pipeline"
```

---

## Task 8: Full test sweep + documentation

**Files:**
- Modify: `README.md`, `plan.md`, `CLAUDE.md`

- [x] **Step 1: Run the whole test suite**

Run: `uv run pytest -q`
Expected: all pass (no regressions). **Done — 506 passed.**

- [x] **Step 2: Update `CLAUDE.md`**

In the `### plan — Cross-Video Rough Directions` section, add to the description
that the plan input now includes calculated **per-part durations and in-between
gaps** (gap only between consecutive parts of the same video), derived from
`summary.json` part `start`/`end`. In the `### director` section, note that the
numbered transcript now annotates each line with `[dur, gap]` from the video's
`{stem}.json` (graceful fallback to untimed when JSON is absent/mismatched). In
the `### summary` section, note `summary.json` parts now carry optional
`start`/`end` times. In the "Current Runtime Quirks" list, add one bullet
summarizing the new `timing.py` helper and the `--json` flags on summary/director.

- [x] **Step 3: Update `README.md`**

Add a short note (wherever the plan/director stages are described for users)
that durations/gaps are now shown to those LLMs, and that the director/summary
stages accept `--json` pointing at the WhisperX `{stem}.json`. **Done — added a
pacing/`--json` paragraph after the summary/plan stage description.**

- [x] **Step 4: Update `plan.md`** — *N/A: `plan.md` was removed from the repo in
commit 657102f ("Remove plan.md"); status is now tracked in these
`docs/superpowers/plans/` files instead, so the entry is recorded here.*

**Status (2026-06-15, feature COMPLETE):** Duration context for the plan &
director stages shipped. New pure `src/nagare_clip/timing.py` module
(`segment_times`, `format_dur_gap`). The summary stage computes per-part
`start`/`end` from each video's WhisperX `{stem}.json` and stores them in
`summary.json` (omitted when unavailable; backward compatible). The plan stage
renders per-part duration + in-between gap from those stored times (gap only
between consecutive parts of the same video; no JSON read). The director stage
annotates each numbered-transcript line with an inline `[dur, gap]` bracket
sourced from `{stem}.json`, with byte-identical fallback to the untimed
transcript when JSON is absent/mismatched. New `--json` flags on the summary
(repeatable) and director CLIs; `run_pipeline.sh` wires the stage1 JSON to both.
All 506 tests pass.

- [ ] **Step 5: Re-run the suite once more after doc edits (sanity)**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add README.md plan.md CLAUDE.md
git commit -m "docs: document duration context for plan & director stages"
```

---

## Self-Review Notes

- **Spec coverage:** timing.py (Task 1), director sentence timing inline (Tasks 2-3), plan part timing within-stem gaps (Tasks 4-6), summary.json storage + backward-compat omission (Task 4), CLI `--json` wiring (Tasks 3, 5, 7), graceful fallback (Tasks 2, 3, 5, 6), docs (Task 8). All spec sections mapped.
- **Type consistency:** `segment_times`/`format_dur_gap` signatures identical across Tasks 1-6; `seg_times` typed `Optional[List[Tuple[Optional[float], Optional[float]]]]` everywhere; `PartSummary.start/end` are `Optional[float]` in storage, dict, and plan rendering.
- **Backward compat:** director without `--json` and plan without part times are asserted byte-identical / bracket-free (Tasks 2, 6).
