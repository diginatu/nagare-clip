# Timelapse Op Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-coordinated `speed` + `keep` + `overlay` timelapse arrangement with a single `timelapse` director op that `guided_edit` desugars into those same markers, with a caption that spans the whole timelapse.

**Architecture:** The director stage gains one op type (`timelapse`, carrying `lines`/`factor`/`text`). A new pure module `guided_edit/timelapse.py` expands each such op into three ordinary `DirectorOp`s before `apply_ops` runs, deriving the caption's duration from the WhisperX segment times `run_guided_edit` already loads. The `intervals` and `blender` stages are untouched, and `_edits.txt` keeps today's syntax.

**Tech Stack:** Python 3 (src layout, `uv run` for everything), pytest, ruff, pydantic-settings config models.

**Spec:** `docs/superpowers/specs/2026-08-01-timelapse-op-design.md`

## Global Constraints

- Run every Python tool through `uv run` (e.g. `uv run pytest`, never bare `pytest`).
- TDD is mandatory: write the test, **run it and see it fail**, then implement. Do not skip the red step — a passing test proves nothing unless you have seen it fail.
- `TIMELAPSE_MIN_FACTOR = 4.0` in `src/nagare_clip/director/director_llm.py` is the single source of truth for the timelapse factor floor. Never hardcode `4.0` in new code; import the constant.
- The caption duration formula is exactly `round((end_b - start_a) / factor, 2)`. No minimum clamp, no maximum clamp.
- `speed` stays a fully available primitive: no parse-time gate on its factor, no auto-promotion to `timelapse`.
- The `intervals` and `blender` packages must not be modified by this work.
- Stage names are never numbered — they are `director`, `guided_edit`, etc.
- Every behaviour change updates `AGENTS.md` and `README.md` (Task 6).
- Commit after each task with a conventional-commit message (`feat(director): ...`, `test(guided_edit): ...`, `docs: ...`).

---

### Task 1: Parse the `timelapse` op

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py` (`VALID_TYPES` ~line 45, `_parse_op` ~lines 137-172)
- Test: `tests/director/test_director_llm.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DirectorOp(type="timelapse", lines=(a, b), factor=float, text=str|None, note=str)`. `duration` is always `None` on a timelapse op. Later tasks rely on `factor` being non-`None` and `>= TIMELAPSE_MIN_FACTOR` for every parsed timelapse op.

Background: `_parse_op` validates one raw dict into a `DirectorOp` or `None`, calling the local `_drop(msg)` for a logged, reportable rejection. It is shared by `parse_director_response` (LLM path) and `ops_from_dict` (hand-edited `_director.json` path), so the floor applies to both — that is intended (see spec §1).

- [ ] **Step 1: Write the failing tests**

Append to `tests/director/test_director_llm.py`. The file already imports `json`, `parse_director_response`, `ops_from_dict`, `ops_to_dict` and `TIMELAPSE_MIN_FACTOR` from `nagare_clip.director.director_llm` — verify those imports exist at the top and add any that are missing.

```python
class TestTimelapseOp:
    """A timelapse is one op carrying the range, the factor and the caption.
    The factor floor is part of what the word means, so it holds on both the
    LLM path and the hand-edited _director.json path (spec 2026-08-01)."""

    def _resp(self, **fields):
        op = {"type": "timelapse", "lines": [10, 20]}
        op.update(fields)
        return json.dumps({"ops": [op]})

    def test_timelapse_at_the_floor_is_accepted(self):
        ops = parse_director_response(
            self._resp(factor=TIMELAPSE_MIN_FACTOR, text="配管の取り付け"), num_lines=40
        )
        assert len(ops) == 1
        assert ops[0].type == "timelapse"
        assert ops[0].lines == (10, 20)
        assert ops[0].factor == TIMELAPSE_MIN_FACTOR
        assert ops[0].text == "配管の取り付け"
        assert ops[0].duration is None

    def test_timelapse_above_the_floor_is_accepted(self):
        ops = parse_director_response(self._resp(factor=8.0, text="作業"), num_lines=40)
        assert [o.factor for o in ops] == [8.0]

    def test_timelapse_below_the_floor_is_dropped(self):
        # 2.0x is a mild speed-up, not a timelapse; accepting it would smuggle
        # an uncapped keep over talking past max_keep_lines.
        drops: list[str] = []
        ops = parse_director_response_with_drops(
            self._resp(factor=2.0, text="作業"), num_lines=40, drops=drops
        )
        assert ops == []
        assert "timelapse" in drops[0] and str(TIMELAPSE_MIN_FACTOR) in drops[0]

    def test_timelapse_without_factor_is_dropped(self):
        assert parse_director_response(self._resp(text="作業"), num_lines=40) == []

    def test_timelapse_text_is_optional(self):
        # No caption is legitimate: the op still fixes continuity.
        ops = parse_director_response(self._resp(factor=6.0), num_lines=40)
        assert len(ops) == 1
        assert ops[0].text is None

    def test_timelapse_blank_text_counts_as_absent(self):
        ops = parse_director_response(self._resp(factor=6.0, text="   "), num_lines=40)
        assert [o.text for o in ops] == [None]

    def test_timelapse_text_newlines_are_normalised(self):
        ops = parse_director_response(
            self._resp(factor=6.0, text="一行目\r\n二行目"), num_lines=40
        )
        assert ops[0].text == "一行目\n二行目"

    def test_timelapse_ignores_a_supplied_duration(self):
        # The caption's on-screen time is derived, never stated.
        ops = parse_director_response(self._resp(factor=6.0, text="作業", duration=2.0), num_lines=40)
        assert ops[0].duration is None

    def test_hand_written_timelapse_gets_the_same_floor(self):
        good = {"ops": [{"type": "timelapse", "lines": [10, 20], "factor": 5.0}]}
        bad = {"ops": [{"type": "timelapse", "lines": [10, 20], "factor": 2.0}]}
        assert len(ops_from_dict(good, num_lines=40)) == 1
        assert ops_from_dict(bad, num_lines=40) == []

    def test_timelapse_round_trips_through_ops_to_dict(self):
        ops = parse_director_response(
            self._resp(factor=8.0, text="配管の取り付け", note="長い作業"), num_lines=40
        )
        data = ops_to_dict(ops)
        assert data["ops"] == [
            {
                "type": "timelapse",
                "lines": [10, 20],
                "factor": 8.0,
                "text": "配管の取り付け",
                "note": "長い作業",
            }
        ]
        assert [o.lines for o in ops_from_dict(data, num_lines=40)] == [(10, 20)]

    def test_a_bare_speed_op_is_untouched_by_the_floor(self):
        # speed stays a primitive: any positive factor, above or below 4.0.
        resp = json.dumps(
            {"ops": [{"type": "speed", "lines": [1, 2], "factor": 1.5},
                     {"type": "speed", "lines": [3, 4], "factor": 8.0}]}
        )
        assert [o.factor for o in parse_director_response(resp, num_lines=40)] == [1.5, 8.0]
```

Add this helper just above the class (the drops list is only reachable through `try_parse_director_response`):

```python
def parse_director_response_with_drops(response, num_lines, drops):
    from nagare_clip.director.director_llm import try_parse_director_response

    return try_parse_director_response(response, num_lines=num_lines, drops=drops) or []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/director/test_director_llm.py::TestTimelapseOp -v`
Expected: FAIL — every case errors or returns `[]`, because `"timelapse"` is not in `VALID_TYPES` so `_parse_op` drops it as `unknown type`.

- [ ] **Step 3: Add the type to `VALID_TYPES`**

In `src/nagare_clip/director/director_llm.py`:

```python
VALID_TYPES = {"cut", "speed", "overlay", "keep", "edit", "timelapse"}
```

- [ ] **Step 4: Validate factor and text in `_parse_op`**

Replace the existing `speed` factor block:

```python
    factor: float | None = None
    if op_type == "speed":
        raw_factor = raw.get("factor")
        if not isinstance(raw_factor, (int, float)) or isinstance(raw_factor, bool):
            _drop("speed op missing factor")
            return None
        factor = float(raw_factor)
        if factor <= 0:
            _drop(f"speed factor {factor!r} <= 0")
            return None
```

with:

```python
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
```

Then, immediately before the existing `if op_type == "overlay":` block, add the timelapse caption handling:

```python
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
```

and delete the now-duplicated `text: str | None = None` / `duration: float | None = None` declarations that currently sit above the overlay block (keep exactly one declaration of each).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/director/test_director_llm.py -v`
Expected: PASS, including all pre-existing tests in the file.

- [ ] **Step 6: Run the full director + config suites**

Run: `uv run pytest tests/director tests/test_config.py -q`
Expected: PASS. (The prompt tests still describe the old prose — that is Task 5. If any fail here, they were already failing; note it and continue.)

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/director/director_llm.py tests/director/test_director_llm.py
git commit -m "feat(director): add a single timelapse op with a 4.0 factor floor"
```

---

### Task 2: Remove the keep-cap timelapse exemption

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py` (`_timelapse_covers` ~lines 179-192, `_apply_keep_cap` ~lines 195-224, `keep_limit_note` ~lines 74-89)
- Test: `tests/director/test_director_llm.py`

**Interfaces:**
- Consumes: `TIMELAPSE_MIN_FACTOR` from Task 1 (unchanged constant, now used only by the parse floor).
- Produces: `_apply_keep_cap(ops, max_keep_lines, drops)` with the same signature but no exemption; `keep_limit_note(max_keep_lines)` returning a note with no exception clause.

Background: the exemption let a wide `keep` survive `max_keep_lines` when a `speed` op with factor >= 4.0 fully contained it. The `timelapse` op replaces that arrangement, and the derived keep never passes through the cap at all (it is created in `guided_edit`, after the director stage). Removing the exemption restores the plain cap for hand-written `speed` + `keep` pairs.

- [ ] **Step 1: Delete the exemption tests**

In `tests/director/test_director_llm.py`, delete the entire `class TestTimelapseKeepExemption` (8 tests, ~lines 306-380) and the method `test_note_states_the_timelapse_exemption` (~lines 295-303).

- [ ] **Step 2: Write the replacement tests**

Add, in the class that previously held `test_note_states_the_timelapse_exemption`:

```python
    def test_note_promises_no_exemption(self):
        """The three-op pairing is gone; a wide keep is capped unconditionally.
        A note still promising an exemption would teach the director to emit
        keeps that are then dropped."""
        note = keep_limit_note(8)
        assert "may exceed this limit" not in note
        assert "fully inside a" not in note
```

and, as a top-level class:

```python
class TestKeepCapHasNoExemption:
    """Regression: a wide keep inside a fast speed op used to be exempt.
    That exemption is removed with the timelapse op (spec 2026-08-01)."""

    def test_wide_keep_inside_a_fast_speed_op_is_now_dropped(self):
        resp = json.dumps(
            {
                "ops": [
                    {"type": "keep", "lines": [10, 21], "note": "n"},
                    {"type": "speed", "lines": [10, 21], "factor": TIMELAPSE_MIN_FACTOR},
                ]
            }
        )
        ops = parse_director_response(resp, num_lines=40, max_keep_lines=8)
        assert [o.type for o in ops] == ["speed"]

    def test_narrow_keep_inside_a_fast_speed_op_still_survives(self):
        resp = json.dumps(
            {
                "ops": [
                    {"type": "keep", "lines": [10, 14], "note": "n"},
                    {"type": "speed", "lines": [10, 21], "factor": TIMELAPSE_MIN_FACTOR},
                ]
            }
        )
        ops = parse_director_response(resp, num_lines=40, max_keep_lines=8)
        assert [o.lines for o in ops if o.type == "keep"] == [(10, 14)]

    def test_hand_written_wide_keep_is_still_uncapped(self):
        data = {"ops": [{"type": "keep", "lines": [10, 21]}]}
        assert [o.lines for o in ops_from_dict(data, num_lines=40)] == [(10, 21)]

    def test_max_keep_lines_zero_disables_the_cap_entirely(self):
        # Carried over from the deleted exemption class: with no limit set, a
        # wide keep survives because the cap is off.
        resp = json.dumps({"ops": [{"type": "keep", "lines": [10, 21], "note": "n"}]})
        ops = parse_director_response(resp, num_lines=40, max_keep_lines=0)
        assert [o.lines for o in ops] == [(10, 21)]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/director/test_director_llm.py::TestKeepCapHasNoExemption -v`
Expected: FAIL on `test_wide_keep_inside_a_fast_speed_op_is_now_dropped` — the exemption still keeps that op, so the assertion sees `["keep", "speed"]`.

Also run: `uv run pytest tests/director/test_director_llm.py -k note_promises_no_exemption -v`
Expected: FAIL — the note still contains the exception clause.

- [ ] **Step 4: Delete `_timelapse_covers` and its use**

Remove the whole `_timelapse_covers` function. In `_apply_keep_cap`, change the condition:

```python
            if span > max_keep_lines and not _timelapse_covers(op, ops):
```

to:

```python
            if span > max_keep_lines:
```

Update `_apply_keep_cap`'s docstring to drop the exemption sentences, leaving:

```python
    """Post-pass: drop a ``keep`` op wider than ``max_keep_lines``.  Kept as a
    pass over the whole parsed list (rather than a check inside
    :func:`_parse_op`) so the drop message/logging stays in one place; tests
    assert on it.
    """
```

Also drop the exemption paragraph from `try_parse_director_response`'s docstring (the sentence beginning "Applied as a post-pass ... so a wide keep fully inside a qualifying timelapse").

- [ ] **Step 5: Shorten `keep_limit_note`**

```python
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/director/test_director_llm.py -v`
Expected: PASS.

Note: `tests/director/test_director_llm.py:282` asserts the system prompt equals `f"P\n\n{keep_limit_note(3)}\n\nCTX"` — it calls the function, so the new wording flows through automatically.

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/director/director_llm.py tests/director/test_director_llm.py
git commit -m "refactor(director): drop the keep-cap timelapse exemption"
```

---

### Task 3: Expand a timelapse into its three markers

**Files:**
- Create: `src/nagare_clip/guided_edit/timelapse.py`
- Test: `tests/guided_edit/test_timelapse.py` (create)

**Interfaces:**
- Consumes: `DirectorOp` (Task 1), `nagare_clip.timing.segment_times` (existing, returns `list[tuple[float | None, float | None]]` — one `(start, end)` per WhisperX segment, index 0 = line 1).
- Produces: `expand_timelapse_ops(ops: list[DirectorOp], seg_times: list[tuple[float | None, float | None]]) -> list[DirectorOp]`. Task 4 calls it with the ops from `ops_from_dict` and passes the result straight to `apply_ops`.

Op ordering matters: `apply_span_op` *prepends* each open tag to the first boundary line, so emitting `overlay`, then `speed`, then `keep` produces `<keep><speed factor="F"><overlay .../>text` on the first line and `text</speed></keep>` on the last.

- [ ] **Step 1: Write the failing tests**

Create `tests/guided_edit/test_timelapse.py`:

```python
"""guided_edit: a timelapse op desugars into overlay + speed + keep."""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops


def _tl(a=1, b=3, factor=4.0, text="作業", note=""):
    return DirectorOp(type="timelapse", lines=(a, b), factor=factor, text=text, note=note)


# Lines 1..4 -> segments; line 1 starts at 10.0, line 3 ends at 50.0 (span 40s).
TIMES = [(10.0, 20.0), (20.0, 30.0), (30.0, 50.0), (50.0, 60.0)]


def test_expands_into_overlay_speed_keep_in_that_order():
    out = expand_timelapse_ops([_tl()], TIMES)
    assert [o.type for o in out] == ["overlay", "speed", "keep"]


def test_overlay_is_a_point_at_the_first_line_and_spans_run_the_range():
    out = expand_timelapse_ops([_tl(a=1, b=3)], TIMES)
    overlay, speed, keep = out
    assert overlay.lines == (1, 1)
    assert speed.lines == (1, 3)
    assert keep.lines == (1, 3)


def test_caption_duration_is_the_span_divided_by_the_factor():
    # (50.0 - 10.0) / 4.0 = 10.0
    out = expand_timelapse_ops([_tl(factor=4.0)], TIMES)
    assert out[0].duration == 10.0


def test_caption_duration_is_rounded_to_two_decimals():
    # (50.0 - 10.0) / 3.0 = 13.333... -> 13.33  (factor is not validated here;
    # the 4.0 floor is enforced at parse time)
    out = expand_timelapse_ops([_tl(factor=3.0)], TIMES)
    assert out[0].duration == 13.33


def test_caption_is_not_clamped_up_for_a_short_timelapse():
    # A 10s span at 8x is a 1.25s caption. A floor would let it outlive its own
    # span and collide with the next timelapse's caption on Blender's single
    # overlay channel.
    out = expand_timelapse_ops([_tl(a=1, b=1, factor=8.0)], TIMES)
    assert out[0].duration == 1.25


def test_derived_ops_carry_the_factor_and_the_note():
    out = expand_timelapse_ops([_tl(factor=6.0, note="配管作業")], TIMES)
    overlay, speed, keep = out
    assert speed.factor == 6.0
    assert keep.factor is None
    assert overlay.text == "作業"
    assert all(o.note == "配管作業" for o in out)


def test_no_text_yields_speed_and_keep_only():
    out = expand_timelapse_ops([_tl(text=None)], TIMES)
    assert [o.type for o in out] == ["speed", "keep"]


def test_missing_times_drop_the_caption_but_keep_the_continuity_fix(caplog):
    out = expand_timelapse_ops([_tl()], [])
    assert [o.type for o in out] == ["speed", "keep"]
    assert "caption dropped" in caplog.text


def test_unknown_boundary_time_drops_the_caption_only():
    times = [(None, 20.0), (20.0, 30.0), (30.0, 50.0)]
    out = expand_timelapse_ops([_tl()], times)
    assert [o.type for o in out] == ["speed", "keep"]


def test_other_ops_pass_through_unchanged_and_in_place():
    cut = DirectorOp(type="cut", lines=(4, 4))
    out = expand_timelapse_ops([cut, _tl(), cut], TIMES)
    assert [o.type for o in out] == ["cut", "overlay", "speed", "keep", "cut"]
    assert out[0] is cut and out[-1] is cut


def test_consecutive_timelapses_produce_captions_that_do_not_overlap():
    # Two phases back to back: each caption lasts exactly its own span / factor,
    # so the second starts where the first ends instead of stacking on it.
    times = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)]
    first = _tl(a=1, b=2, factor=4.0, text="第一段階")
    second = _tl(a=3, b=4, factor=4.0, text="第二段階")
    out = expand_timelapse_ops([first, second], times)
    captions = [o for o in out if o.type == "overlay"]
    assert [c.duration for c in captions] == [5.0, 5.0]
    assert [c.lines for c in captions] == [(1, 1), (3, 3)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/guided_edit/test_timelapse.py -v`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'nagare_clip.guided_edit.timelapse'`.

- [ ] **Step 3: Write the module**

Create `src/nagare_clip/guided_edit/timelapse.py`:

```python
"""Desugar a director ``timelapse`` op into the markers that already exist.

A timelapse is a ``<keep><speed factor="F">…</speed></keep>`` span with one
caption over the whole of it.  Expressing that as three ops the director must
emit with matching ranges made the agreement unenforced — a keep that missed
the speed range by a line silently produced sped-up jump cuts.  The director
now emits one ``timelapse`` op and this module expands it, so the three markers
cannot disagree.

Pure: no I/O, no LLM call.  The caption's duration is derived rather than
stated — the derived ``<keep>`` preserves the whole span, so the edited-timeline
length of the timelapse is exactly ``span / factor``.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from nagare_clip.director.director_llm import DirectorOp

logger = logging.getLogger(__name__)

SegTimes = list[tuple[float | None, float | None]]


def caption_duration(op: DirectorOp, seg_times: SegTimes) -> float | None:
    """On-screen seconds for *op*'s caption, or ``None`` when undeterminable.

    ``(end of the last line - start of the first line) / factor``.  Nothing
    inside the span is cut (the derived keep protects it), so this is the
    timelapse's real length on the edited timeline, not an estimate.  It is
    deliberately unclamped: every overlay shares one Blender channel, so a
    caption padded past its own span would collide with the next timelapse's.
    """
    a, b = op.lines
    factor = op.factor
    if not factor or factor <= 0:
        return None
    if a < 1 or b > len(seg_times):
        return None
    start = seg_times[a - 1][0]
    end = seg_times[b - 1][1]
    if start is None or end is None or end <= start:
        return None
    return round((end - start) / factor, 2)


def expand_timelapse_ops(ops: list[DirectorOp], seg_times: SegTimes) -> list[DirectorOp]:
    """Replace every ``timelapse`` op with ``overlay`` + ``speed`` + ``keep``.

    Emission order matters: :func:`~nagare_clip.guided_edit.apply.apply_span_op`
    prepends each opening tag, so overlay-then-speed-then-keep yields
    ``<keep><speed factor="F"><overlay …/>`` on the first boundary line.

    Non-timelapse ops pass through untouched, in place.  When the caption's
    duration cannot be derived the span ops are still emitted: the continuity
    fix is the valuable half.
    """
    out: list[DirectorOp] = []
    for op in ops:
        if op.type != "timelapse":
            out.append(op)
            continue
        a, b = op.lines
        duration = caption_duration(op, seg_times)
        if op.text and duration is None:
            logger.warning(
                "guided_edit: timelapse [%d-%d] caption dropped: no usable segment times",
                a,
                b,
            )
        if op.text and duration is not None:
            out.append(replace(op, type="overlay", lines=(a, a), factor=None, duration=duration))
        out.append(replace(op, type="speed", lines=(a, b), text=None, duration=None))
        out.append(
            replace(op, type="keep", lines=(a, b), factor=None, text=None, duration=None)
        )
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/guided_edit/test_timelapse.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/nagare_clip/guided_edit/timelapse.py tests/guided_edit/test_timelapse.py && uv run ruff format --check src tests`
Expected: clean. If `format --check` complains, run `uv run ruff format src tests` and re-check.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/guided_edit/timelapse.py tests/guided_edit/test_timelapse.py
git commit -m "feat(guided_edit): desugar a timelapse op into keep+speed+overlay"
```

---

### Task 4: Wire the expansion into the stage

**Files:**
- Modify: `src/nagare_clip/guided_edit/run.py` (whole file, 58 lines)
- Modify: `src/nagare_clip/guided_edit/apply.py` (`apply_ops` loop, ~line 272)
- Test: `tests/guided_edit/test_run.py`, `tests/guided_edit/test_apply.py`

**Interfaces:**
- Consumes: `expand_timelapse_ops` (Task 3), `segment_times` from `nagare_clip.timing`.
- Produces: nothing new for later tasks — this is the integration point.

Background: `run_guided_edit` already receives `json_path` and reads it at the end for its `check_edits` pass. The read moves to the top so `segment_times` can use the same data. The orchestrator (`pipeline/stages.py`) always passes `json_path`; the parameter stays optional and the caption degrades when it is absent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/guided_edit/test_timelapse.py`:

```python
def test_apply_ops_reports_an_unexpanded_timelapse_instead_of_raising():
    """Defence in depth: expansion happens in run_guided_edit, but apply_ops is
    public. An unexpanded op must be reported, not crash _span_tags."""
    from nagare_clip.guided_edit.apply import apply_ops

    lines, unapplied = apply_ops(["あ", "い"], [_tl(a=1, b=2)], {})
    assert lines == ["あ", "い"]
    assert len(unapplied) == 1
    assert "unexpanded" in unapplied[0][1]
```

Append to `tests/guided_edit/test_run.py`:

```python
def test_timelapse_op_becomes_nested_markers_with_a_spanning_caption(tmp_path):
    """One timelapse op produces <keep><speed><overlay/> on the first line and
    the closing tags on the last, with a caption lasting the whole timelapse."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\nさしす\n", encoding="utf-8")
    whisper = tmp_path / "clip.json"
    whisper.write_text(
        json.dumps(
            {
                "segments": [
                    {"text": "あいう", "start": 10.0, "end": 20.0},
                    {"text": "かきく", "start": 20.0, "end": 30.0},
                    {"text": "さしす", "start": 30.0, "end": 50.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    director = tmp_path / "clip_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "timelapse", "lines": [1, 3], "factor": 4.0, "text": "配管作業"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_edits.txt"

    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg={"guided_edit": {"enabled": True}},
        json_path=whisper,
    )

    written = out.read_text(encoding="utf-8").splitlines()
    # (50.0 - 10.0) / 4.0 = 10.0 seconds on the edited timeline.
    assert written[0] == '<keep><speed factor="4.0"><overlay text="配管作業" duration="10.0"/>あいう'
    assert written[1] == "かきく"
    assert written[2] == "さしす</speed></keep>"


def test_timelapse_output_passes_the_edits_checker(tmp_path):
    """The desugared file must be one the intervals stage accepts."""
    from nagare_clip.intervals.check_edits import check_edits

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\nさしす\n", encoding="utf-8")
    whisper_data = {
        "segments": [
            {"text": "あいう", "start": 10.0, "end": 20.0},
            {"text": "かきく", "start": 20.0, "end": 30.0},
            {"text": "さしす", "start": 30.0, "end": 50.0},
        ]
    }
    whisper = tmp_path / "clip.json"
    whisper.write_text(json.dumps(whisper_data), encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "timelapse", "lines": [1, 3], "factor": 8.0, "text": "作業"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_edits.txt"

    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg={"guided_edit": {"enabled": True}},
        json_path=whisper,
    )

    problems = check_edits(out.read_text(encoding="utf-8").splitlines(), whisper_data)
    assert problems == []


def test_timelapse_without_json_still_applies_speed_and_keep(tmp_path):
    """No segment times -> no caption, but the continuity fix still lands."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\n", encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "timelapse", "lines": [1, 2], "factor": 4.0, "text": "作業"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_edits.txt"

    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg={"guided_edit": {"enabled": True}},
    )

    written = out.read_text(encoding="utf-8").splitlines()
    assert written[0] == '<keep><speed factor="4.0">あいう'
    assert written[1] == "かきく</speed></keep>"
    assert "<overlay" not in "\n".join(written)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/guided_edit/test_timelapse.py tests/guided_edit/test_run.py -v`
Expected: FAIL — `apply_ops` raises `ValueError: not a span op: timelapse` from `_span_tags`, and the `run` tests produce untagged output.

- [ ] **Step 3: Guard `apply_ops` against an unexpanded op**

In `src/nagare_clip/guided_edit/apply.py`, inside the `for i, op in ordered:` loop, directly after the `section = ...` line, insert:

```python
        if op.type == "timelapse":
            # run_guided_edit desugars these before we see them (see
            # guided_edit.timelapse); reaching here means a caller skipped that
            # step, and _span_tags has no marker pair for the type.
            reason = "timelapse op reached apply_ops unexpanded"
            recorder.attempt(
                unit=unit,
                attempt=0,
                total=1,
                messages=[],
                outcome=VERIFY_FAIL,
                reason=reason,
                cfg=None,
                deterministic=True,
                section=section,
            )
            logger.warning("guided_edit: op %s dropped: %s", op.type, reason)
            unapplied.append((op, reason))
            continue
```

- [ ] **Step 4: Rewrite `run_guided_edit`**

Replace the body of `src/nagare_clip/guided_edit/run.py` with:

```python
"""guided_edit stage (Pass B2): apply director ops into _edits.txt.

When ``guided_edit.enabled`` is false (default) it copies the input edits
through unchanged so the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.director_llm import ops_from_dict
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops
from nagare_clip.intervals.check_edits import check_edits
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.timing import segment_times


def run_guided_edit(
    edits_txt: Path,
    director_json: Path,
    output: Path,
    cfg: dict,
    *,
    json_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    ge_cfg = cfg["guided_edit"]
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    stem = output.stem.replace("_edits", "")

    # Read once: the segment times size a timelapse's caption, and the same
    # data drives the closing check_edits pass.
    json_data = json.loads(json_path.read_text(encoding="utf-8")) if json_path else None

    if not ge_cfg.get("enabled", False):
        logging.info("guided_edit: disabled, copying edits through")
        result_lines = edit_lines
        unapplied: list = []
    else:
        director_data = json.loads(director_json.read_text(encoding="utf-8"))
        ops = ops_from_dict(director_data, num_lines=len(edit_lines))
        ops = expand_timelapse_ops(ops, segment_times(json_data) if json_data else [])
        logging.info("guided_edit: applying %d director op(s)", len(ops))
        result_lines, unapplied = apply_ops(edit_lines, ops, ge_cfg, recorder=recorder, unit=stem)
        logging.info(
            "guided_edit: %d applied, %d unapplied",
            len(ops) - len(unapplied),
            len(unapplied),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("guided_edit: wrote %s", output)

    if json_data is not None:
        problems = check_edits(result_lines, json_data)
        for p in problems:
            where = "file" if p.line is None else f"line {p.line}"
            logging.warning("check_edits: %s: %s", where, p.message)
        if problems:
            logging.warning("guided_edit: %d check_edits problem(s) in output", len(problems))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/guided_edit -v`
Expected: PASS, including the pre-existing `test_disabled_copies_through` / `test_enabled_applies_ops`.

- [ ] **Step 6: Verify the caption duration is really load-bearing**

Temporarily change `round((end - start) / factor, 2)` in `timelapse.py` to `round(end - start, 2)` (forgetting the factor), run
`uv run pytest tests/guided_edit -k timelapse -v`, and confirm the duration assertions fail. Revert the mutation and re-run to confirm green. Record both outcomes in the commit message body or the task report.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS except the `tests/test_config.py` prompt tests still pinned to the old speed bullet — those are Task 5. Note exactly which fail.

- [ ] **Step 8: Commit**

```bash
git add src/nagare_clip/guided_edit/run.py src/nagare_clip/guided_edit/apply.py tests/guided_edit
git commit -m "feat(guided_edit): expand timelapse ops before applying them"
```

---

### Task 5: Teach the director the new op

**Files:**
- Modify: `src/nagare_clip/config.py` (`DIRECTOR_PROMPT`, lines 157-249)
- Test: `tests/test_config.py` (lines 352-435, 589-655)

**Interfaces:**
- Consumes: `parse_director_response` accepting `timelapse` (Task 1).
- Produces: a prompt whose `- timelapse:` bullet and JSON example parse through the real parser.

Background: `DIRECTOR_PROMPT` is one long implicitly-concatenated string. Several tests select a single prompt *line* by prefix (`- speed:`) or by a phrase, so keep each bullet on its own line exactly as now.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, add next to the existing `_speed_bullet()` helper:

```python
def _timelapse_bullet() -> str:
    """The single DIRECTOR_PROMPT line describing the `timelapse` op."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    return next(ln for ln in prompt.splitlines() if ln.startswith("- timelapse:"))
```

Replace `test_director_prompt_makes_speed_a_two_mode_choice`, `test_director_prompt_timelapse_states_its_price_and_its_partners`, `test_director_prompt_marks_mild_speed_factors_as_the_exception` and `test_director_prompt_speed_example_factor_is_at_least_4` with:

```python
def test_director_prompt_keeps_the_two_mode_choice_across_both_ops():
    """The listening/timelapse choice now spans two ops -- speed for a mild
    accent, timelapse for the real thing. It must still read as a choice, not
    a dial (see docs/superpowers/specs/2026-08-01-speed-two-mode-choice-design.md)."""
    both = (_speed_bullet() + "\n" + _timelapse_bullet()).lower()
    assert "not a dial" in both
    assert "listening" in both
    assert "timelapse" in both
    assert "1x" in both
    assert "cut the weakest parts" in both


def test_director_prompt_timelapse_states_its_price():
    """A timelapse loses intelligible audio; saying so is what forces an honest
    choice instead of a mild speed-up that splits the difference."""
    bullet = _timelapse_bullet().lower()
    assert "4.0" in bullet
    assert "unintelligible" in bullet


def test_director_prompt_timelapse_is_self_contained():
    """The whole point of the op: one op does the arrangement three ops used to.
    The prompt must not ask for a companion keep/speed/overlay, or the director
    will emit the ops the desugaring already creates."""
    bullet = _timelapse_bullet().lower()
    assert 'do not add a separate "keep"' in bullet
    assert "consecutive" in bullet  # how to change the caption partway through


def test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent():
    """1.3-2.0 was the entire observed range of a real run (57% of the finished
    video). With timelapse carrying the fast case, that band is all a bare
    speed op is for."""
    bullet = _speed_bullet().lower()
    assert "1.3" in bullet and "2.0" in bullet
    assert "accent" in bullet


def test_director_prompt_speed_example_is_a_mild_accent():
    """The JSON-shape speed example is what an LLM copies over the prose. Fast
    now belongs to timelapse, so a 4.0+ speed example would teach exactly the
    keepless fast speed-up that plays back as jump cuts."""
    from nagare_clip.director.director_llm import TIMELAPSE_MIN_FACTOR, parse_director_response

    prompt = get_effective_config(None, {})["director"]["prompt"]
    example = next(
        line.strip().rstrip(",") for line in prompt.splitlines() if '"type": "speed"' in line
    )
    ops = parse_director_response('{"ops": [' + example + "]}", num_lines=100)
    assert len(ops) == 1
    assert ops[0].type == "speed"
    assert ops[0].factor is not None and ops[0].factor < TIMELAPSE_MIN_FACTOR


def test_director_prompt_timelapse_example_parses_and_is_fast():
    """Pin the documented example to the real parser: a stale one (missing text,
    or a factor under the floor) would be silently dropped at runtime."""
    from nagare_clip.director.director_llm import TIMELAPSE_MIN_FACTOR, parse_director_response

    prompt = get_effective_config(None, {})["director"]["prompt"]
    example = next(
        line.strip().rstrip(",") for line in prompt.splitlines() if '"type": "timelapse"' in line
    )
    ops = parse_director_response('{"ops": [' + example + "]}", num_lines=100)
    assert len(ops) == 1
    assert ops[0].type == "timelapse"
    assert ops[0].factor is not None and ops[0].factor >= TIMELAPSE_MIN_FACTOR
    assert ops[0].text
```

Then update the two selectors that read the "Prefer speed over cut" paragraph — in `test_director_prompt_does_not_offer_speed_as_a_way_to_tighten_speech`, change:

```python
    para = next(ln for ln in prompt.splitlines() if "Prefer speed over cut" in ln).lower()
```

to:

```python
    para = next(ln for ln in prompt.splitlines() if "Prefer a timelapse over a cut" in ln).lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k director_prompt -v`
Expected: FAIL — `StopIteration` on `_timelapse_bullet()` (no such line), and the speed-example test still sees factor 4.0.

- [ ] **Step 3: Rewrite the ops paragraph and the speed bullet**

In `src/nagare_clip/config.py`, change the paragraph opening (currently `"Prefer speed over cut where the repetition is VISIBLE WORK building "`) to:

```python
    "Operations (reference lines by their 1-based numbers, inclusive). "
    "Prefer a timelapse over a cut where the repetition is VISIBLE WORK building "
    "toward a payoff (failed attempts, assembly, waiting for a result) — "
    "the buildup is part of the story, so timelapse it rather than "
    "deleting it. Where the repetition is SPEECH, speed is not the tool: "
    "leave it at 1x, or cut the weakest passes. Reserve cut for spans "
    "that leave the throughline entirely (digressions, dead ends, "
    "redundant retakes with no payoff):\n"
```

(only the second line changes; the rest must stay word-for-word so the other pinned tests keep passing).

Replace the whole `- speed:` bullet line with:

```python
    '- speed: play a span slightly faster; give "factor". It is an ACCENT, not a dial for shaving time off speech: stay in the 1.3 to 2.0 band, use it on a short span, and never let it become the register the video runs in. It protects nothing — unlike a "keep", the silences and pauses inside its span are still dropped — so a fast factor here plays back as sped-up jump cuts. When a span is manual work worth going genuinely fast over, emit a "timelapse" instead.\n'
```

- [ ] **Step 4: Add the timelapse bullet**

Insert directly after the `- speed:` line:

```python
    '- timelapse: play a long stretch of manual work fast under one on-screen caption; give "factor" (4.0 or more) and "text". Speed is a choice between two modes and this is the second one. LISTENING: the speech carries something the viewer needs — emit no speed op at all, play it at 1x, and if it drags cut the weakest parts instead. TIMELAPSE: the speech is inessential — go genuinely fast and accept that the words become unintelligible; that sacrifice is the point of the mode and is why you must be sure first. One op does the whole arrangement: the work runs continuously (its internal pauses are preserved, so it is not chopped into jump cuts) and the caption stays on screen for the entire timelapse — do not add a separate "keep", "speed" or "overlay" over the same lines. To change the caption partway through, emit consecutive timelapse ops; a new caption means a new phase of work.\n'
```

- [ ] **Step 5: Update the JSON-shape block**

Change the speed example's factor and add a timelapse example:

```python
    "JSON shape:\n"
    '{"ops": [\n'
    '  {"type": "cut", "lines": [12, 18], "note": "why / where precisely"},\n'
    '  {"type": "speed", "lines": [30, 34], "factor": 1.5, "note": "..."},\n'
    '  {"type": "timelapse", "lines": [60, 92], "factor": 8.0, "text": "配管の取り付け", "note": "..."},\n'
    '  {"type": "overlay", "lines": [5, 5], "text": "ポイント", "duration": 2.0, "note": ""},\n'
    '  {"type": "keep", "lines": [40, 42], "note": "..."},\n'
    '  {"type": "edit", "lines": [7, 7], "note": "delete the redundant restatement"}\n'
    "]}\n"
```

- [ ] **Step 6: Run the prompt tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — all `director_prompt` tests, including the ones this task did not touch (`..._documents_speed_does_not_keep_silence` needs `keep` and `silence` in the speed bullet; the rewrite above has both).

- [ ] **Step 7: Check the generated config example for drift**

Run: `make config-example && git diff --stat config.example.yml`
Expected: no diff — the prompt is emitted as a commented `# prompt: "..."` placeholder, not inlined. If a diff does appear, commit it.

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS, all tests.

- [ ] **Step 9: Commit**

```bash
git add src/nagare_clip/config.py tests/test_config.py config.example.yml
git commit -m "feat(director): teach the prompt one timelapse op instead of three"
```

---

### Task 6: Documentation

**Files:**
- Modify: `AGENTS.md` (the `director` and `guided_edit` stage sections)
- Modify: `README.md` (the director/guided_edit paragraphs around lines 32-34)

**Interfaces:**
- Consumes: the finished behaviour from Tasks 1-5.
- Produces: nothing consumed by code.

No `docs/stages/` file covers `director` or `guided_edit`, and `intervals`/`blender` behaviour is unchanged, so no stage deep-dive needs editing. There is no `plan.md` in this repo despite the Documentation Policy naming one — skip it.

- [ ] **Step 1: Update the AGENTS.md director section**

In the `### director — LLM High-Level Edit Operations (Pass A)` section:

- Change the op-type list to `type ∈ {cut, speed, overlay, keep, edit, timelapse}`.
- Add, after the `overlay` op sentence: a `timelapse` op carries `lines`, a `factor` of at least `director_llm.TIMELAPSE_MIN_FACTOR` (4.0, enforced on both the LLM path and a hand-edited `_director.json` — below it the op is dropped, since it would smuggle an uncapped keep over talking) and an optional `text`; it never carries a `duration`, because the caption's on-screen time is derived in `guided_edit` from the span it covers. It replaces the `speed`+`keep`+`overlay` arrangement, which nothing enforced.
- Replace the sentence describing the `max_keep_lines` exemption ("Exempt from the cap: a `keep` op whose line range is fully **contained** in a `speed` op ... reopening the runtime-inflation hole the cap was built to close.") and the following two sentences about `_apply_keep_cap`/`_timelapse_covers` with: the cap now has no exemption — a `timelapse` op's keep is created after the director stage, so it never meets the cap, and a hand-written wide `speed`+`keep` pair is capped like any other.
- In the final sentence about the prompt framing `speed` as a two-mode choice, note that the second mode is now the `timelapse` op and a bare `speed` is limited to the 1.3-2.0 accent band.

- [ ] **Step 2: Update the AGENTS.md guided_edit section**

In `### guided_edit — Apply Director Ops (Pass B2)`, add after the first sentence:

> A `timelapse` op is desugared before anything is applied (`guided_edit.timelapse.expand_timelapse_ops`, called from `run_guided_edit`): it becomes an `overlay` point op at its first line followed by `speed` and `keep` span ops over its whole range, which `apply_span_op`'s prepending yields as `<keep><speed factor="F"><overlay …/>` on the first line and `</speed></keep>` on the last. The caption's duration is `round((end_of_last_line − start_of_first_line) / factor, 2)` — exact, because the derived `keep` preserves the whole span, so the edited-timeline length *is* that quotient. It is deliberately unclamped: all overlays share one Blender channel, so a padded caption would collide with the next consecutive timelapse's. When segment times are unavailable the span ops are still emitted and only the caption is dropped (logged). `apply_ops` reports an unexpanded `timelapse` op as unapplied rather than raising.

Also add `timelapse.py` to the `guided_edit/` entry in the **Project Structure** tree:

```
    timelapse.py              # expand_timelapse_ops(): one timelapse op -> keep+speed+overlay
```

- [ ] **Step 3: Update README.md**

- In the paragraph listing what the director proposes (`cut/speed/overlay/keep/edit`), add `timelapse` and one sentence: a `timelapse` op names a range, a factor of 4.0+ and a caption, and guided_edit expands it into a `<keep><speed>` span with an `<overlay/>` that lasts the whole timelapse — so the three markers cannot disagree, which is what produced sped-up jump cuts when they were three separate ops.
- In the `director.max_keep_lines` paragraph, delete the exemption sentence if present and state that the cap has no exceptions, with `timelapse` being the supported way to protect a long fast span.

- [ ] **Step 4: Verify no stale claims remain**

Run: `grep -rn "timelapse" AGENTS.md README.md`
Read every hit and confirm none still describes the three-op pairing or the keep-cap exemption.

Run: `grep -rn "_timelapse_covers\|may exceed this limit" . --include=*.py --include=*.md | grep -v docs/superpowers`
Expected: no hits outside the spec/plan documents.

- [ ] **Step 5: Full validation**

Run: `make check`
Expected: lint, format check, structural validation and the full pytest suite all pass.

- [ ] **Step 6: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: describe the timelapse op and the removed keep-cap exemption"
```

---

## Verification

After Task 6, confirm end to end:

- [ ] `make check` passes.
- [ ] `uv run pytest -q` reports no failures and no skips that were previously passing.
- [ ] `git log --oneline main..HEAD` shows six commits, one per task.
