# Silence references (`"n~"`) end to end — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a director op address the silence between two transcript lines, so a wait can be fast-forwarded without destroying the speech on either side of it.

**Architecture:** An op's `lines` endpoint may be written `"n~"`, meaning "the silence after source line n". Parsing keeps `lines` as the same `(int, int)` every existing consumer reads, and records the two edges as booleans on `DirectorOp`. Ops carrying such an edge cannot be expressed as text markers in `_edits.txt`, so they are resolved to **time ranges** in code and handed to `run_intervals` alongside the marker-derived ranges. Every other op keeps its current path untouched.

**Tech Stack:** Python 3.12, pytest, uv, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-19-director-agent-loop-design.md`

## Global Constraints

- TDD: write the test, run it, see it fail for the right reason, then implement.
- Every task has a **mutation step**: break the implementation the named way, confirm the *named* test fails, restore. On this branch four tests have passed for the wrong reason; a mutation that leaves the suite green means the test is wrong, not the mutation.
- Undo a mutation by copying the file aside to `/tmp/claude-1000/` and back. **Never** `git checkout -- <file>`, `git reset` or `git stash` — that has destroyed work twice here.
- `rm` is aliased off; use `\rm`.
- Commands: `uv run --project /home/diginah/ghq/github.com/diginatu/nagare-clip pytest -q`, `… ruff check src tests`, `… ruff format --check src tests`. Baseline before this plan: **2038 passed**.
- Never edit `/mnt/work/YouTube/water_pump_4/nagare_config.yml` or anything under `/mnt/work/YouTube/water_pump_4/video-editor-ai/`.
- Every commit message ends with the line `Claude-Session: https://claude.ai/code/session_01KuPe2gfZ9ns9Mm5FRvhpPA`.
- **Backwards compatibility is a hard requirement:** a `_director.json` written before this change must parse to the same ops and produce a byte-identical `_edits.txt`. Task 1 pins it.

## File Structure

| File | Responsibility |
|---|---|
| `src/nagare_clip/director/director_llm.py` (modify) | `DirectorOp` gains `gap_start` / `gap_end`; `_coerce_lines` accepts `"n~"`; `ops_to_dict` writes it back. Nothing else in this file changes. |
| `src/nagare_clip/intervals/op_times.py` (create) | Pure resolver: ops + WhisperX data → keep / speed / overlay **time** ranges. No I/O, no markers. |
| `src/nagare_clip/intervals/run.py` (modify) | `run_intervals` accepts the resolved ranges and unions them with the marker-derived ones. |
| `src/nagare_clip/guided_edit/apply.py` (modify) | Ops with a gap edge are not placed as text markers (they have no words to wrap). |
| `src/nagare_clip/pipeline/stages.py` (modify) | The intervals stage reads `{stem}_director.json`, resolves it, and passes the ranges down. |
| `tests/director/test_silence_refs.py` (create) | Task 1. |
| `tests/intervals/test_op_times.py` (create) | Tasks 2. |
| `tests/intervals/test_run_extra_ranges.py` (create) | Task 3. |
| `tests/guided_edit/test_gap_ops_skip_markers.py` (create) | Task 4. |

---

### Task 1: `"n~"` parses, round-trips, and changes nothing else

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py` (`DirectorOp` ~64-72, `_coerce_lines` ~110-139, `ops_to_dict`)
- Test: `tests/director/test_silence_refs.py` (create)

**Interfaces:**
- Produces: `DirectorOp.gap_start: bool = False`, `DirectorOp.gap_end: bool = False`.
  `_coerce_lines(value, num_lines, first_line=1) -> tuple[tuple[int, int], bool, bool] | None`
  — the existing callers unpack three values now.
  Meaning: `"53~"` as the START edge = the range begins at the silence after line 53 (so line 53's speech is outside it); as the END edge = the range ends after that silence (line 53's speech is inside it). `["53~", "53~"]` is the silence alone.

- [ ] **Step 1: Write the failing test**

```python
"""Silence references: `"n~"` is the silence after source line n."""

from __future__ import annotations

from nagare_clip.director.director_llm import ops_from_dict, ops_to_dict


def _one(lines):
    ops = ops_from_dict({"ops": [{"type": "timelapse", "lines": lines, "factor": 5.0}]}, 100)
    assert len(ops) == 1, f"{lines!r} was dropped"
    return ops[0]


def test_a_silence_only_range_keeps_its_line_number_and_marks_both_edges():
    # The op addresses no speech at all: it starts and ends at the silence
    # after line 53.  `lines` stays (53, 53) so every consumer that blocks,
    # clips or sorts by line number keeps working unchanged.
    op = _one(["53~", "53~"])
    assert op.lines == (53, 53)
    assert (op.gap_start, op.gap_end) == (True, True)


def test_a_line_plus_its_trailing_silence():
    op = _one([53, "53~"])
    assert op.lines == (53, 53)
    assert (op.gap_start, op.gap_end) == (False, True)


def test_a_silence_then_the_lines_after_it():
    op = _one(["53~", 55])
    assert op.lines == (53, 55)
    assert (op.gap_start, op.gap_end) == (True, False)


def test_plain_numbers_are_unchanged():
    op = _one([53, 55])
    assert op.lines == (53, 55)
    assert (op.gap_start, op.gap_end) == (False, False)


def test_malformed_silence_references_are_dropped():
    for bad in (["~53", 55], ["0~", "0~"], ["abc~", 55], ["53~~", 55], [53, "52~"]):
        assert (
            ops_from_dict({"ops": [{"type": "cut", "lines": bad}]}, 100) == []
        ), f"{bad!r} should be dropped"


def test_round_trip_writes_the_silence_form_back():
    ops = ops_from_dict(
        {"ops": [{"type": "timelapse", "lines": ["53~", "53~"], "factor": 5.0}]}, 100
    )
    assert ops_to_dict(ops)["ops"][0]["lines"] == ["53~", "53~"]


def test_round_trip_of_a_plain_op_is_byte_identical():
    # A _director.json written before this feature must reparse and reserialise
    # to exactly what it was.
    before = {"ops": [{"type": "cut", "lines": [12, 18], "note": "why"}]}
    assert ops_to_dict(ops_from_dict(before, 100)) == before
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run --project /home/diginah/ghq/github.com/diginatu/nagare-clip pytest tests/director/test_silence_refs.py -q`
Expected: FAIL — `AttributeError: 'DirectorOp' object has no attribute 'gap_start'` on the first test, and `["53~", "53~"] was dropped` where `_coerce_lines` rejects the string.

- [ ] **Step 3: Implement**

In `DirectorOp`, after `note`:

```python
    # True when that edge of the range is the silence AFTER the line, written
    # "n~" in _director.json.  `lines` stays the speech-line numbers so every
    # consumer that blocks, clips, sorts or reports by line keeps working; only
    # the resolver that turns an op into times reads these.
    gap_start: bool = False
    gap_end: bool = False
```

Replace the endpoint handling in `_coerce_lines`:

```python
_SILENCE_REF_RE = re.compile(r"^(\d+)~$")


def _coerce_endpoint(value: Any) -> tuple[int, bool] | None:
    """One endpoint: ``12`` -> (12, False); ``"12~"`` -> (12, True)."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(value, int):
        return (value, False)
    if isinstance(value, str):
        m = _SILENCE_REF_RE.match(value)
        if m:
            return (int(m.group(1)), True)
    return None
```

and inside `_coerce_lines`, build the pair from `_coerce_endpoint` for both the
scalar and the two-element forms, returning `(lines, gap_start, gap_end)`.
Keep every existing guard: reject `start > end`, `start < max(1, first_line)`,
and `end > num_lines`; a silence edge does not relax them, so `[53, "52~"]` is
rejected like `[53, 52]`.

In `_parse_op`, unpack the three values and pass the flags to `DirectorOp`.

In `ops_to_dict`:

```python
        first = f"{op.lines[0]}~" if op.gap_start else op.lines[0]
        last = f"{op.lines[1]}~" if op.gap_end else op.lines[1]
        entry: dict[str, Any] = {"type": op.type, "lines": [first, last]}
```

- [ ] **Step 4: Run the tests**

Run: `uv run … pytest tests/director/test_silence_refs.py -q` → PASS
Run: `uv run … pytest -q` → 2038 + 7 passed, nothing else changed.

- [ ] **Step 5: Mutations**

For each: apply, run the named test, confirm it fails, restore from the copy.

| Mutation | Test that must fail |
|---|---|
| `_coerce_endpoint` returns `(n + 1, True)` for `"n~"` | `test_a_silence_only_range_keeps_its_line_number_and_marks_both_edges` |
| Swap `gap_start` and `gap_end` when building the op | `test_a_line_plus_its_trailing_silence` |
| `ops_to_dict` always writes plain ints | `test_round_trip_writes_the_silence_form_back` |
| `_SILENCE_REF_RE` becomes `r"(\d+)~"` (unanchored) | `test_malformed_silence_references_are_dropped` (`"~53"` would pass) |
| Drop the `end > num_lines` guard | an existing test in `tests/director/test_director_llm.py` (name it in the report) |

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/director/director_llm.py tests/director/test_silence_refs.py
git commit -m "feat(director): ops can address the silence after a line as \"n~\"

Claude-Session: https://claude.ai/code/session_01KuPe2gfZ9ns9Mm5FRvhpPA"
```

---

### Task 2: Resolve ops with a silence edge to time ranges

**Files:**
- Create: `src/nagare_clip/intervals/op_times.py`
- Test: `tests/intervals/test_op_times.py` (create)

**Interfaces:**
- Consumes: `DirectorOp` with `gap_start` / `gap_end` (Task 1); `intervals.speech.build_speech_spans(whisperx_data) -> list[tuple[float, float]]`, which is already what `run_intervals` uses to decide what silence is dropped (`intervals/run.py:79, 84-92`): each word is capped at `SILENCE_MAX_WORD_SPAN` and at the next word's start, so the gap between consecutive spans is exactly the silence the pipeline drops.
- Produces:

```python
@dataclass(frozen=True)
class OpTimes:
    keeps: list[tuple[float, float]]
    speeds: list[tuple[float, float, float]]   # (start, end, factor)
    overlays: list[tuple[float, str, float]]   # (start, text, duration)

def resolve_op_times(ops: list[DirectorOp], whisperx_data: dict) -> OpTimes: ...
```

Only ops with `gap_start or gap_end` are resolved here; the rest stay on the
marker path and are skipped. `timelapse` is expanded first
(`guided_edit.timelapse.expand_timelapse_ops` semantics: keep + speed over the
same span, plus an overlay at the start), so a `timelapse ["53~","53~"] x5`
yields one keep, one speed and one overlay.

- [ ] **Step 1: Write the failing test**

```python
"""Ops that address a silence resolve to the exact time range the pipeline drops."""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.intervals.op_times import resolve_op_times

# Two segments with one word each and a 10 s wait between them.
WHISPERX = {
    "segments": [
        {"words": [{"word": "あ", "start": 1.0, "end": 1.4}]},
        {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        {"words": [{"word": "う", "start": 12.5, "end": 13.0}]},
    ]
}


def test_a_silence_only_keep_covers_exactly_the_dropped_gap():
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    times = resolve_op_times([op], WHISPERX)
    assert times.keeps == [(1.4, 11.4)]


def test_a_line_plus_its_trailing_silence_starts_at_the_words():
    op = DirectorOp(type="keep", lines=(1, 1), gap_end=True)
    assert resolve_op_times([op], WHISPERX).keeps == [(1.0, 11.4)]


def test_a_silence_then_the_next_line_ends_at_the_words():
    op = DirectorOp(type="keep", lines=(1, 2), gap_start=True)
    assert resolve_op_times([op], WHISPERX).keeps == [(1.4, 12.0)]


def test_a_timelapse_yields_keep_speed_and_overlay_over_the_same_span():
    op = DirectorOp(
        type="timelapse", lines=(1, 1), gap_start=True, gap_end=True, factor=5.0, text="待ち"
    )
    times = resolve_op_times([op], WHISPERX)
    assert times.keeps == [(1.4, 11.4)]
    assert times.speeds == [(1.4, 11.4, 5.0)]
    assert times.overlays == [(1.4, "待ち", 2.0)]  # (10.0 / 5.0) on-screen seconds


def test_ops_without_a_silence_edge_are_left_to_the_markers():
    op = DirectorOp(type="keep", lines=(1, 2))
    times = resolve_op_times([op], WHISPERX)
    assert (times.keeps, times.speeds, times.overlays) == ([], [], [])


def test_the_word_end_is_the_capped_one_the_pipeline_uses():
    # A word whose raw end runs far into the wait: build_speech_spans caps it,
    # and the kept silence must start where the pipeline actually starts
    # dropping, not at the raw end.  On the real project this differs for 5 of
    # 49 gaps, by 6 to 26 seconds.
    data = {
        "segments": [
            {"words": [{"word": "あ", "start": 1.0, "end": 9.0}]},
            {"words": [{"word": "い", "start": 11.4, "end": 12.0}]},
        ]
    }
    op = DirectorOp(type="keep", lines=(1, 1), gap_start=True, gap_end=True)
    (start, _end), = resolve_op_times([op], data).keeps
    assert start < 9.0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run … pytest tests/intervals/test_op_times.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.intervals.op_times'`.

- [ ] **Step 3: Implement**

`src/nagare_clip/intervals/op_times.py`: build the per-line span table once —
for each WhisperX segment, the first and last of its `build_speech_spans`
entries — then

- start time = `spans_of(a).start` normally, `spans_of(a).end` when `gap_start`;
- end time = `spans_of(b).end` normally, `spans_of(b + 1).start` when `gap_end`;
- skip (and log at WARNING) an op whose line has no timed words, or whose
  `gap_end` names the last line of the source (no next line to end at);
- for a `timelapse`, emit keep + speed + overlay with the overlay's duration
  `(end - start) / factor`, matching `guided_edit.timelapse.caption_duration`.

Resolution uses 1-based source line numbers, so index `n - 1` into the segments.

- [ ] **Step 4: Run the tests** → PASS; then `uv run … pytest -q` unchanged.

- [ ] **Step 5: Mutations**

| Mutation | Test that must fail |
|---|---|
| `gap_end` resolves to `spans_of(b + 1).end` | `test_a_silence_then_the_next_line_ends_at_the_words` / `test_a_silence_only_keep_covers_exactly_the_dropped_gap` |
| `gap_start` ignored (always `spans_of(a).start`) | `test_a_silence_only_keep_covers_exactly_the_dropped_gap` |
| Use the raw `word["end"]` instead of the capped span end | `test_the_word_end_is_the_capped_one_the_pipeline_uses` |
| Resolve ops without a gap edge too | `test_ops_without_a_silence_edge_are_left_to_the_markers` |
| Overlay duration `(end - start)` unscaled | `test_a_timelapse_yields_keep_speed_and_overlay_over_the_same_span` |

- [ ] **Step 6: Commit** (`feat(intervals): resolve silence-addressing ops to time ranges`, with the session line).

---

### Task 3: `run_intervals` accepts the resolved ranges

**Files:**
- Modify: `src/nagare_clip/intervals/run.py` (`run_intervals` signature ~35-42; the union at ~55-63 and ~128-135)
- Test: `tests/intervals/test_run_extra_ranges.py` (create)

**Interfaces:**
- Consumes: `OpTimes` (Task 2).
- Produces: `run_intervals(edits_txt, json_path, output, cfg, *, cuts_txt=None, extra=None)` where `extra: OpTimes | None`. `None` keeps today's behaviour exactly.

- [ ] **Step 1: Write the failing test** — a small WhisperX fixture with a 10 s wait, an `_edits.txt` with no markers, run `run_intervals` twice (with and without `extra`), and assert: without it the wait is absent from the kept intervals; with a keep over `(1.4, 11.4)` the kept intervals contain it; with keep + speed the output's speed ranges contain `(1.4, 11.4, 5.0)`. Assert on the written JSON, not on logs.

- [ ] **Step 2: Run it and watch it fail** — `TypeError: run_intervals() got an unexpected keyword argument 'extra'`.

- [ ] **Step 3: Implement** — union `extra.keeps` into `force_keep_ranges` before the `subtract_intervals` at `run.py:~130`, `extra.speeds` into `speed_ranges`, and `extra.overlays` into `overlay_marks`, each immediately after the marker extraction so one code path follows.

- [ ] **Step 4: Run the tests**, then the full suite.

- [ ] **Step 5: Mutations**: union the extra keeps *after* `subtract_intervals` (the wait stays dropped → the with-`extra` assertion fails); drop `extra.speeds` (the speed assertion fails); default `extra` to an empty `OpTimes` built at import time and mutate it in place (the without-`extra` assertion fails on the second call).

- [ ] **Step 6: Commit** (`feat(intervals): run_intervals takes resolved op time ranges`).

---

### Task 4: guided_edit leaves silence-addressing ops alone

**Files:**
- Modify: `src/nagare_clip/guided_edit/apply.py` (`apply_ops` span placement ~245-330)
- Test: `tests/guided_edit/test_gap_ops_skip_markers.py` (create)

**Interfaces:** consumes Task 1's flags. No new exports.

- [ ] **Step 1: Write the failing test** — ops `[keep (1,1) gap_start gap_end]` and `[cut (2,2)]` over a two-line `_edits.txt`: the output must contain the `<cut>` markers and **no** `<keep>` tag, and the recorder must report the keep as placed-by-time rather than dropped (assert on the text and on the report entry).

- [ ] **Step 2: Run it and watch it fail** — today a keep whose span has no words still gets a marker, or is reported as unplaced.

- [ ] **Step 3: Implement** — in the span-op loop, `continue` for `op.gap_start or op.gap_end` before placement, recording the op as resolved elsewhere. Leave the cut-clipped-around-kept-lines rule untouched: a gap op still occupies its lines for blocking, because `lines` is unchanged.

- [ ] **Step 4: Run the tests**, then the full suite, then `ruff check` and `ruff format --check`.

- [ ] **Step 5: Mutations**: skip *all* keeps rather than gap ones (an existing guided_edit keep test fails); skip gap ops from blocking as well (the cut-clipping test fails).

- [ ] **Step 6: Commit** (`feat(guided_edit): silence-addressing ops bypass text markers`).

---

### Task 5: Wire the stage and run the real footage

**Files:**
- Modify: `src/nagare_clip/pipeline/stages.py` (the intervals stage, ~750-762)
- Test: extend `tests/intervals/test_run_extra_ranges.py` with a stage-level test using the real loader and a fake source tree.

- [ ] **Step 1: Write the failing test** — the intervals stage reads `{stem}_director.json`, resolves it with `resolve_op_times`, and passes `extra=` to `run_intervals`; a `_director.json` with `["1~","1~"]` produces kept intervals covering the wait.
- [ ] **Step 2: Run it and watch it fail.**
- [ ] **Step 3: Implement** — load the JSON with `ops_from_dict(..., None)`, resolve against the already-loaded WhisperX data, pass `extra=`. A missing `_director.json` resolves to `None`.
- [ ] **Step 4: Run the tests and the full suite.**
- [ ] **Step 5: Mutations** — pass `extra=None` unconditionally (the new test fails); resolve against the wrong source's WhisperX data (assert the kept interval moves).
- [ ] **Step 6: Commit** (`feat(intervals): the stage resolves director silence refs`).

- [ ] **Step 7 (controller, not the implementer): evidence on the real footage.**

**This step makes LLM calls — declare the cost to the user and wait for approval before running it.** `guided_edit` calls the LLM for `edit` ops; `PXL_20260426_090431216_director.json` has one (`edit [18,18]`), so expect ~1 call to `openai/gpt-5.6-luna`.

```bash
# 1. Back up everything the run overwrites
cp -r /mnt/work/YouTube/water_pump_4/video-editor-ai/{director,guided_edit,intervals,blender} "$TMPDIR/before-silence/"

# 2. Hand-edit the op: [53, 53] -> ["53~", "53~"], factor 5
#    in video-editor-ai/director/PXL_20260426_090431216_director.json

# 3. Run that source only
cd /mnt/work/YouTube/water_pump_4
./run_nagare_clip.sh --source PXL_20260426_090431216.mp4 \
    --from-stage guided_edit --to-stage blender
```

Then check `video-editor-ai/intervals/PXL_20260426_090431216_intervals.json`:
the kept intervals must include **501.897–531.779 s** and the speed ranges must
carry it at 5.0, with line 54 (from 531.779 s) at 1x. A feasibility study
already proved this exact range produces that output when handed to
`run_intervals` directly, so a mismatch means the resolver, not the pipeline.

---

## Increment 2 (outline): silence lines in the director's view

| Task | Files | What its tests prove |
|---|---|---|
| Build the display-line view | new `director/display.py` | A source's speech lines plus one silence line per between-line gap ≥ 5 s, each mapping back to a source line or `"n~"`; counts match the measured 49 gaps on the real project. |
| Render it | `director/director_llm.py` (`render_transcript`) | The rendered transcript shows `54: [silent 29.9s: …]`, the gap-context description becomes its text, and the `gap` part disappears from brackets. |
| Retire the redundant machinery | `config.py` (Timing legend, visual-context paragraph, `[N, N+1]` rule), `gap_context/context.py`, `director/preview.py` | The prompt no longer explains gap rescue; the preview no longer needs its "gap outside this op" fact; prompt length goes down, not up. |
| Ops come back in source coordinates | `director/display.py` | An op over a silence display line serialises to `"n~"`. |

## Increment 3 (outline): one conversation over the whole video

| Task | Files | What its tests prove |
|---|---|---|
| Global numbering across segments | `director/display.py` | Playback-order numbering over all segments; a display line maps to (segment, source, line-or-`n~`); a range crossing a join is split for `cut`/`keep` and refused for `timelapse`/`overlay`. |
| The turn protocol | new `director/loop.py` | An approximate range per turn ("around lines X to Y"), the model's reviewed-through line drives the next request, a reply replaces only its named range, `done` is refused while lines remain unreviewed. |
| Cap and failure | `director/loop.py`, `pipeline/stages.py` | At `ceil(lines / chunk) × 2` turns the stage raises **and still writes** the ops accepted so far, naming the last reviewed line. |
| Delete the per-segment path | `pipeline/stages.py`, `director/run.py`, `director/context.py` | The suite stays green with the per-segment loop, `prior_edits` and seams gone. |
| Measure | — | One director run vs the three earlier ones: range-end claims, unintelligible speech seconds, gaps dropped between neighbouring ops, timelapse share, turns and rewrites used. |
