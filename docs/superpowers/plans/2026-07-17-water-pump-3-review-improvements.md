# water_pump_3 Review Improvements Implementation Plan

> **STATUS: COMPLETE (2026-07-27).** All 10 tasks implemented, reviewed, and
> merged to `main`; `make check` green (834 tests). Two defaults were changed
> from this document's original proposals during implementation — **the code is
> authoritative, not the step text below**:
>
> - `intervals.min_cut` shipped at **0.4** (not 0.25), at the user's choice.
> - `gap_context.static_ssim` shipped at **0.95** (not 0.99), derived from real
>   calibration: max ACTION-gap SSIM 0.9416, max STATIC-gap 0.9763, so 0.99
>   would have made the prefilter inert on the actual corpus. See the
>   calibration note in `docs/stages/gap_context.md`.
>
> Also note: Task 2's Step 5 (the out-of-repo `nagare_config.yml` edit) and
> Task 10's Step 3 (real-data spot check) were **not** performed — both touch
> paths outside the sandbox. Step 3's stale "no gap below 0.25s" criterion also
> predates the 0.4 default.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement the improvements accepted from the water_pump_3 real-run review: micro-jump-cut merging (`intervals.min_cut`), text_filter repeated-phrase prompt fix, three observability/log polish items, silence-aware duration brackets for director/plan, and a gap_context pixel-static SSIM prefilter.

**Architecture:** Every change extends an existing stage module following its current patterns: pure helpers get unit tests, config keys are added to the pydantic models in `config.py` (single source of truth; `config.example.yml` is regenerated), and orchestrator plumbing goes through the stage adapters in `pipeline/stages.py`. No new dependencies; media tooling stays inside the whisperx Docker image.

**Tech Stack:** Python 3.12, uv, pytest, pydantic v2, LiteLLM, ffmpeg-in-Docker.

## Global Constraints

- Dependency management uses uv + pyproject.toml — add **no** new dependencies.
- Media tooling (ffmpeg) runs only inside the whisperx Docker image (no host binaries, no Python image libs).
- `config.example.yml` is generated: after ANY change to `src/nagare_clip/config.py` models/prompts, run `uv run python -m nagare_clip.config --write-example` and commit the regenerated file in the same commit (`tests/test_config.py::test_example_file_matches_generator` enforces this).
- All Python invocations via `uv run` (e.g. `uv run pytest tests/intervals -v`).
- TDD: write the failing test, SEE it fail, then implement. Where a test is added for existing behavior, briefly mutate the implementation to confirm the test catches it, then revert.
- Documentation policy: when a task changes stage behavior, update that stage's `docs/stages/<stage>.md` **and** the stage overview in `AGENTS.md` in the same commit. (README.md/plan.md sync happens once in the final task.)
- Reference review data (for context, do not modify): `/mnt/work/YouTube/water_pump_3/video-editor-ai/`.

---

### Task 1: `intervals.min_cut` — absorb micro jump cuts

> **DONE 2026-07-27** (commit `22fef51`, branch `feat/intervals-min-cut`). Shipped with default **0.4** (not 0.25) at the user's choice; design spec at `docs/superpowers/specs/2026-07-27-intervals-min-cut-design.md`. Skip this task.

Real-run evidence: 42/341 inter-keep gaps in one video were <0.3s (smallest 1–2ms) — visible jump cuts that save nothing, created by keep/caption margins eating exclude gaps from both sides.

**Files:**
- Modify: `src/nagare_clip/intervals/intervals.py` (append new function)
- Modify: `src/nagare_clip/intervals/run.py:188-193` (wire after `enforce_min_keep_duration`)
- Modify: `src/nagare_clip/config.py` (`IntervalsConfig`, after `keep_post_margin` ~line 631)
- Modify: `config.example.yml` (regenerated)
- Modify: `docs/stages/intervals.md` ("Margins & captions" section), `AGENTS.md` (intervals overview)
- Test: `tests/intervals/test_intervals.py` (append), `tests/test_config.py` (existing generator test covers regen)

**Interfaces:**
- Produces: `merge_close_intervals(keep_intervals: list[dict], min_cut: float) -> list[dict]` — input sorted disjoint `{"start","end"}` dicts; config key `intervals.min_cut` (float, default 0.25).

- [x] **Step 1: Write the failing tests** — append to `tests/intervals/test_intervals.py`:

```python
class TestMergeCloseIntervals:
    def test_sub_threshold_gap_absorbed(self):
        from nagare_clip.intervals.intervals import merge_close_intervals

        ivs = [{"start": 0.0, "end": 5.0}, {"start": 5.05, "end": 10.0}]
        assert merge_close_intervals(ivs, 0.25) == [{"start": 0.0, "end": 10.0}]

    def test_gap_at_threshold_kept(self):
        from nagare_clip.intervals.intervals import merge_close_intervals

        ivs = [{"start": 0.0, "end": 5.0}, {"start": 5.25, "end": 10.0}]
        assert merge_close_intervals(ivs, 0.25) == ivs

    def test_chain_of_slivers_all_absorbed(self):
        from nagare_clip.intervals.intervals import merge_close_intervals

        ivs = [
            {"start": 0.0, "end": 1.0},
            {"start": 1.001, "end": 2.0},
            {"start": 2.01, "end": 3.0},
            {"start": 4.0, "end": 5.0},
        ]
        assert merge_close_intervals(ivs, 0.25) == [
            {"start": 0.0, "end": 3.0},
            {"start": 4.0, "end": 5.0},
        ]

    def test_zero_disables(self):
        from nagare_clip.intervals.intervals import merge_close_intervals

        ivs = [{"start": 0.0, "end": 5.0}, {"start": 5.001, "end": 10.0}]
        assert merge_close_intervals(ivs, 0.0) == ivs

    def test_empty_input(self):
        from nagare_clip.intervals.intervals import merge_close_intervals

        assert merge_close_intervals([], 0.25) == []

    def test_input_not_mutated(self):
        from nagare_clip.intervals.intervals import merge_close_intervals

        ivs = [{"start": 0.0, "end": 5.0}, {"start": 5.05, "end": 10.0}]
        merge_close_intervals(ivs, 0.25)
        assert ivs[0] == {"start": 0.0, "end": 5.0}
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/intervals/test_intervals.py::TestMergeCloseIntervals -v` — Expected: FAIL (ImportError: cannot import name `merge_close_intervals`).

- [x] **Step 3: Implement** — append to `src/nagare_clip/intervals/intervals.py`:

```python
def merge_close_intervals(keep_intervals: list[dict], min_cut: float) -> list[dict]:
    """Absorb gaps between adjacent keep intervals shorter than *min_cut*.

    Keep/caption margins eat into exclude gaps from both sides, so a cut can
    survive as a millisecond sliver — a visible jump cut that saves nothing.
    Input must be sorted and disjoint (the earlier passes guarantee it).
    ``min_cut <= 0`` disables (returns the input unchanged).
    """
    if min_cut <= 0.0 or not keep_intervals:
        return keep_intervals
    merged = [dict(keep_intervals[0])]
    for iv in keep_intervals[1:]:
        if float(iv["start"]) - float(merged[-1]["end"]) < min_cut:
            merged[-1]["end"] = max(float(merged[-1]["end"]), float(iv["end"]))
        else:
            merged.append(dict(iv))
    return merged
```

- [x] **Step 4: Run tests** — `uv run pytest tests/intervals/test_intervals.py::TestMergeCloseIntervals -v` — Expected: PASS.

- [x] **Step 5: Wire into the stage** — in `src/nagare_clip/intervals/run.py`: add `merge_close_intervals` to the existing `from nagare_clip.intervals.intervals import (...)` block, then insert directly after the `enforce_min_keep_duration` block (after the `"After min_keep enforcement"` log line, before `output_data = {`):

```python
    if ivl["min_cut"] > 0.0:
        keep_intervals_dicts = merge_close_intervals(keep_intervals_dicts, ivl["min_cut"])
        logging.info(
            "After min_cut merge (gaps < %.2fs absorbed): %d interval(s)",
            ivl["min_cut"],
            len(keep_intervals_dicts),
        )
```

- [x] **Step 6: Add the config key** — in `src/nagare_clip/config.py`, inside `IntervalsConfig` after `keep_post_margin`:

```python
    min_cut: float = Field(
        0.25,
        description="Merge adjacent keep intervals separated by less than this (seconds); absorbs millisecond jump cuts left over from margin arithmetic. 0 disables",
    )
```

- [x] **Step 7: Regenerate the example config** — `uv run python -m nagare_clip.config --write-example`, then `uv run pytest tests/test_config.py -v` — Expected: PASS.

- [x] **Step 8: Full intervals suite** — `uv run pytest tests/intervals tests/test_config.py -v` — Expected: PASS (no existing test asserts interval counts through `run_intervals` with sub-0.25s gaps; if one fails, inspect whether its fixture legitimately produces a sliver and set `min_cut: 0` in that test's cfg instead of weakening the assert).

- [x] **Step 9: Update docs** — `docs/stages/intervals.md` "Margins & captions" bullet list: add one bullet: "`intervals.min_cut` (default 0.25s) runs last (after caption expansion and `min_keep`): any gap between adjacent keep intervals shorter than it is absorbed — margins eating a cut from both sides otherwise leave millisecond jump-cut slivers. A deliberate sub-threshold deletion's audio becomes audible again; consistent with the pre-margin already keeping 0.5–1.0s of audio. `0` disables." In `AGENTS.md` intervals overview paragraph, append sentence: "A final `min_cut` merge absorbs sub-threshold gaps between keep intervals so margin arithmetic can't leave millisecond jump cuts."

- [x] **Step 10: Commit**

```bash
git add src/nagare_clip/intervals/ src/nagare_clip/config.py config.example.yml tests/intervals/test_intervals.py docs/stages/intervals.md AGENTS.md
git commit -m "feat(intervals): min_cut merge absorbs micro jump cuts left by margins"
```

---

### Task 2: text_filter repeated-phrase marker example (+ user config fix)

Real-run evidence: all 3 dropped text_filter batches were the LLM deleting a repeated phrase as a bare rewrite (no `{{...->}}` markers), tripping the safety check.

**Files:**
- Modify: `src/nagare_clip/config.py:51-71` (`TEXT_FILTER_PROMPT`)
- Modify: `config.example.yml` (regenerated)
- Modify: `docs/stages/text_filter.md` (mention the repeated-phrase rule), `AGENTS.md` (only if text_filter overview mentions prompt rules — otherwise skip)
- Modify (out-of-repo, no commit): `/mnt/work/YouTube/water_pump_3/nagare_config.yml`
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `TEXT_FILTER_PROMPT` containing rule "keep the later occurrence… delete the earlier one with a {{...->}} marker" and worked example `4: {{映ってる->}}映ってるね`.

- [x] **Step 1: Write the failing test** — append to `tests/test_config.py`:

```python
def test_text_filter_prompt_repeated_phrase_example_is_valid_patch_syntax():
    """Real-run failure mode: told to de-duplicate repeated phrases but shown
    no marker example, the filter LLM rewrites the line bare and the safety
    check drops it.  The default prompt must state the rule AND show a worked
    marker example — and the example must round-trip through the real patch
    applier."""
    from nagare_clip.text_filter.llm_filter import apply_patches_to_lines

    cfg = get_effective_config(None, {})
    prompt = cfg["text_filter"]["prompt"]
    assert "repeated" in prompt
    assert "映ってる映ってるね" in prompt  # example input line
    assert "{{映ってる->}}映ってるね" in prompt  # example output line
    assert apply_patches_to_lines(["{{映ってる->}}映ってるね"]) == ["映ってるね"]
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/test_config.py::test_text_filter_prompt_repeated_phrase_example_is_valid_patch_syntax -v` — Expected: FAIL on `assert "repeated" in prompt`. (If `apply_patches_to_lines` has a different signature — check `src/nagare_clip/text_filter/llm_filter.py` — adapt the last assert to the real API before proceeding; the point is pinning the example to the real parser.)

- [x] **Step 3: Implement** — replace `TEXT_FILTER_PROMPT` in `src/nagare_clip/config.py` with:

```python
TEXT_FILTER_PROMPT = (
    "Fix speech recognition errors in Japanese text.\n"
    "Remove filler words (あのー, えーと) and noise like (雑音).\n"
    "Only fix clear mistakes. Do NOT rephrase correct text.\n"
    "\n"
    "Rules:\n"
    "- Copy each line fully with its number.\n"
    "- Wrap ONLY the erroneous part: {{error->fix}} or {{delete->}}.\n"
    "- Keep all surrounding text unchanged.\n"
    "- If a phrase is repeated, keep the later occurrence and delete the "
    "earlier one with a {{...->}} marker — never rewrite the line without "
    "markers.\n"
    "\n"
    "Example:\n"
    "Input:\n"
    "1: えーとそれは急はいい天気ですね\n"
    "2: 正しい文です\n"
    "3: (雑音)\n"
    "4: 映ってる映ってるね\n"
    "\n"
    "Output:\n"
    "1: {{えーと->}}それは{{急は->今日は}}いい天気ですね\n"
    "2: 正しい文です\n"
    "3: {{(雑音)->}}\n"
    "4: {{映ってる->}}映ってるね"
)
```

- [x] **Step 4: Regenerate + run tests** — `uv run python -m nagare_clip.config --write-example && uv run pytest tests/test_config.py tests/text_filter -v` — Expected: PASS.

- [x] **Step 5: Fix the user's project config** (out-of-repo edit, no git): in `/mnt/work/YouTube/water_pump_3/nagare_config.yml`, inside `text_filter.prompt`:
  - Fix typo `Do not add panctuation or spaces.` → `Do not add punctuation or spaces.`
  - Replace the rule line `- If similar phrases are repeated, keep the later occurrence.` with `- If similar phrases are repeated, keep the later occurrence and delete the earlier one with a {{...->}} marker — never rewrite the line without markers.`
  - In the Example block, append input line `3: 映ってる映ってるね` and output line `3: {{映ってる->}}映ってるね` (keep the existing 1:/2: lines).

- [x] **Step 6: Update docs** — `docs/stages/text_filter.md`: in the prompt/LLM-contract discussion add one sentence: "The default prompt demonstrates repeated-phrase deletion in marker form (`{{映ってる->}}映ってるね`) — real-project runs showed the LLM otherwise performs the deduplication as a bare rewrite, which the marker safety check must drop."

- [x] **Step 7: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py docs/stages/text_filter.md
git commit -m "feat(text_filter): repeated-phrase deletion rule + worked marker example in the default prompt"
```

---

### Task 3: llm_report — mark deterministic (non-LLM) records

Real-run evidence: guided_edit span ops are applied with no LLM call, yet their report entries read "Attempt 1/1 — temperature 0.1" with the configured model name — indistinguishable from real calls.

**Files:**
- Modify: `src/nagare_clip/llm_report.py` (`_Attempt`, `Recorder.attempt`, `_render_unit`, `flush_unit` model pick)
- Modify: `src/nagare_clip/guided_edit/apply.py:258-291` (the two span-op `recorder.attempt(...)` calls)
- Modify: `docs/stages/observability.md`
- Test: `tests/test_llm_report.py` (append), `tests/guided_edit/` (existing suite must stay green)

**Interfaces:**
- Produces: `Recorder.attempt(..., deterministic: bool = False)`; deterministic records render as `deterministic — <outcome>` with no temperature, and contribute no model to the unit front-matter.

- [x] **Step 1: Write the failing tests** — append to `tests/test_llm_report.py`:

```python
class TestDeterministicRecords:
    def test_deterministic_attempt_renders_without_temperature_or_attempt_count(self, tmp_path):
        from nagare_clip.llm_report import OK, Recorder

        rec = Recorder("guided_edit", tmp_path)
        rec.attempt(
            unit="u",
            attempt=0,
            total=1,
            messages=[],
            response="<keep>x</keep>",
            outcome=OK,
            deterministic=True,
        )
        rec.flush_unit("u", outcome=OK)
        text = (tmp_path / "guided_edit" / "u.md").read_text(encoding="utf-8")
        assert "deterministic — ok" in text
        assert "temperature" not in text
        assert "Attempt" not in text

    def test_llm_attempt_still_renders_attempt_header(self, tmp_path):
        from nagare_clip.llm_report import OK, Recorder

        rec = Recorder("guided_edit", tmp_path)
        rec.attempt(
            unit="u",
            attempt=0,
            total=2,
            messages=[{"role": "user", "content": "hi"}],
            response="ok",
            outcome=OK,
            cfg={"temperature": 0.4, "model": "m1"},
        )
        rec.flush_unit("u", outcome=OK)
        text = (tmp_path / "guided_edit" / "u.md").read_text(encoding="utf-8")
        assert "Attempt 1/2 — temperature 0.4 — ok" in text
        assert "model: m1" in text

    def test_front_matter_model_prefers_real_llm_attempt(self, tmp_path):
        """A unit whose LAST record is deterministic must still report the
        model of the LLM attempt that actually ran."""
        from nagare_clip.llm_report import OK, Recorder

        rec = Recorder("guided_edit", tmp_path)
        rec.attempt(unit="u", attempt=0, total=1, messages=[], response="r",
                    outcome=OK, cfg={"temperature": 0.1, "model": "m1"})
        rec.attempt(unit="u", attempt=0, total=1, messages=[], response="r2",
                    outcome=OK, deterministic=True)
        rec.flush_unit("u", outcome=OK)
        text = (tmp_path / "guided_edit" / "u.md").read_text(encoding="utf-8")
        assert "model: m1" in text
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/test_llm_report.py::TestDeterministicRecords -v` — Expected: FAIL (unexpected keyword `deterministic`).

- [x] **Step 3: Implement in `src/nagare_clip/llm_report.py`:**
  - `_Attempt`: add field `deterministic: bool = False` (after `section`).
  - `Recorder.attempt`: add keyword-only param `deterministic: bool = False`; pass `deterministic=deterministic` into the `_Attempt(...)` construction.
  - `flush_unit`: replace `model = attempts[-1].model if attempts else ""` with `model = next((a.model for a in reversed(attempts) if a.model), "")` (same one-line treatment for `thinking`: `thinking = next((a.thinking for a in reversed(attempts) if a.model), False)`).
  - `_render_unit` header block — replace:

```python
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
```

- [x] **Step 4: Run tests** — `uv run pytest tests/test_llm_report.py -v` — Expected: PASS.

- [x] **Step 5: Use it in guided_edit** — in `src/nagare_clip/guided_edit/apply.py`, in the span-op branch (`if op.type != "edit":`) both `recorder.attempt(...)` calls (the clipped-to-nothing drop record and the apply record): change `cfg=cfg,` to `cfg=None, deterministic=True,`. The `edit`-op branch records are untouched.

- [x] **Step 6: Run the guided_edit suite** — `uv run pytest tests/guided_edit tests/test_llm_report.py -v` — Expected: PASS (fix any test that pinned the old header text — the new expected header for span ops is `## [op N: <type> [a-b]] deterministic — ok`).

- [x] **Step 7: Update docs** — `docs/stages/observability.md`: in the report-format description add: "Span ops in guided_edit are applied deterministically (no LLM call); their records render as `deterministic — <outcome>` with no temperature, and never contribute a model name to the unit front-matter."

- [x] **Step 8: Commit**

```bash
git add src/nagare_clip/llm_report.py src/nagare_clip/guided_edit/apply.py tests/test_llm_report.py tests/guided_edit docs/stages/observability.md
git commit -m "feat(llm_report): render deterministic guided_edit span ops as such, not as fake LLM attempts"
```

---

### Task 4: Blender — silence the 1-frame tail clamp

Real-run evidence: `Strip 516: interval clamped to clip duration (18640 frames). Requested frames 18412-18641, applied 18412-18640` — sec→frame rounding at the video tail overshoots by exactly one frame; a WARNING for a 1-frame no-op is noise.

**Files:**
- Create: `src/nagare_clip/blender/frames.py` (pure, no bpy — host-testable)
- Modify: `src/nagare_clip/blender/timeline.py:273-288`
- Modify: `docs/stages/blender.md`
- Test: `tests/blender/test_frames.py` (new; runs on host, no Blender needed)

**Interfaces:**
- Produces: `clamp_frames(src_start: int, src_end: int, full_duration: int) -> tuple[int, int, bool]` — returns `(bounded_start, bounded_end, negligible)`; `negligible=True` iff the start is unchanged and the end overshoots by at most 1 frame.

- [x] **Step 1: Write the failing tests** — create `tests/blender/test_frames.py`:

```python
"""Host-side tests for the pure frame-clamp helper (no bpy, no Blender)."""

from __future__ import annotations

from nagare_clip.blender.frames import clamp_frames


class TestClampFrames:
    def test_in_range_untouched(self):
        assert clamp_frames(10, 100, 200) == (10, 100, True)

    def test_one_frame_tail_overshoot_is_negligible(self):
        # the real water_pump_3 case: requested 18412-18641 vs 18640 frames
        assert clamp_frames(18412, 18641, 18640) == (18412, 18640, True)

    def test_multi_frame_overshoot_is_not_negligible(self):
        assert clamp_frames(10, 205, 200) == (10, 200, False)

    def test_start_clamp_is_not_negligible(self):
        bounded_start, bounded_end, negligible = clamp_frames(250, 260, 200)
        assert bounded_start == 199
        assert bounded_end == 200
        assert negligible is False

    def test_end_forced_after_start(self):
        # degenerate zero/negative-length request still yields >= 1 frame
        assert clamp_frames(50, 50, 200)[:2] == (50, 51)
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/blender/test_frames.py -v` — Expected: FAIL (ModuleNotFoundError `nagare_clip.blender.frames`).

- [x] **Step 3: Implement** — create `src/nagare_clip/blender/frames.py`:

```python
"""Pure frame-range helpers for the VSE layout (no bpy import — host-testable)."""

from __future__ import annotations


def clamp_frames(src_start: int, src_end: int, full_duration: int) -> tuple[int, int, bool]:
    """Clamp a strip's source frame range to the clip length.

    Returns ``(bounded_start, bounded_end, negligible)``.  *negligible* is
    True when the start is unchanged and the end overshoots the clip by at
    most one frame — the sec->frame rounding artifact at a video's tail,
    which merits a debug line rather than a WARNING.
    """
    bounded_start = min(src_start, full_duration - 1)
    bounded_end = min(max(src_end, bounded_start + 1), full_duration)
    negligible = bounded_start == src_start and 0 <= src_end - bounded_end <= 1
    return bounded_start, bounded_end, negligible
```

- [x] **Step 4: Run tests** — `uv run pytest tests/blender/test_frames.py -v` — Expected: PASS.

- [x] **Step 5: Wire into timeline.py** — add `from nagare_clip.blender.frames import clamp_frames` to the imports, then replace lines 273-288 (the `bounded_start = ...` through the `logging.warning(...)` block) with:

```python
        bounded_start, bounded_end, negligible = clamp_frames(
            src_start_frame, src_end_frame, full_duration
        )
        keep_frame_count = bounded_end - bounded_start

        if bounded_start != src_start_frame or bounded_end != src_end_frame:
            log = logging.debug if negligible else logging.warning
            log(
                "%sStrip %d: interval clamped to clip duration (%d frames). "
                "Requested frames %d-%d, applied %d-%d",
                src_tag,
                idx,
                full_duration,
                src_start_frame,
                src_end_frame,
                bounded_start,
                bounded_end,
            )
```

- [x] **Step 6: Validate** — `uv run python -m py_compile src/nagare_clip/blender/timeline.py src/nagare_clip/blender/frames.py && uv run pytest tests/blender -v` — Expected: PASS (Blender-process tests skip if `blender` not on PATH; run them if available).

- [x] **Step 7: Update docs** — `docs/stages/blender.md`: add a sentence where strip placement/clamping is described: "A 1-frame end overshoot at the clip tail (sec→frame rounding) is clamped silently at debug level; anything larger still warns (`blender/frames.py::clamp_frames`)."

- [x] **Step 8: Commit**

```bash
git add src/nagare_clip/blender/frames.py src/nagare_clip/blender/timeline.py tests/blender/test_frames.py docs/stages/blender.md
git commit -m "fix(blender): clamp 1-frame tail overshoot silently instead of warning"
```

---

### Task 5: quiet LiteLLM's log noise in pipeline.log

Real-run evidence: every stage start dumps `Proxy Server is not installed. Skipping OpenTelemetry initialization.` (with a partial traceback) plus per-call `LiteLLM completion() model=...` / `Wrapper: Completed Call` INFO chatter into pipeline.log.

**Files:**
- Modify: `src/nagare_clip/logging_setup.py`
- Modify: `docs/stages/observability.md`
- Test: `tests/test_logging_setup.py` (append)

**Interfaces:**
- Produces: `setup_logging` additionally caps the `LiteLLM*`/`httpx` loggers at WARNING (unless the root level is DEBUG) and filters the "Proxy Server is not installed" record.

- [x] **Step 1: Write the failing tests** — append to `tests/test_logging_setup.py`:

```python
class TestLiteLLMNoiseSuppression:
    def test_litellm_loggers_capped_at_warning_on_info_root(self):
        import logging

        from nagare_clip.logging_setup import setup_logging

        setup_logging("INFO")
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_debug_root_leaves_litellm_verbose(self):
        import logging

        from nagare_clip.logging_setup import setup_logging

        # reset any level set by a previous test in this process
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx"):
            logging.getLogger(name).setLevel(logging.NOTSET)
        setup_logging("DEBUG")
        assert logging.getLogger("LiteLLM").level == logging.NOTSET

    def test_proxy_not_installed_record_is_filtered(self):
        import logging

        from nagare_clip.logging_setup import setup_logging

        setup_logging("INFO")
        lg = logging.getLogger("LiteLLM")
        noisy = logging.LogRecord(
            "LiteLLM", logging.WARNING, __file__, 1,
            "Proxy Server is not installed. Skipping OpenTelemetry initialization.",
            None, None,
        )
        useful = logging.LogRecord(
            "LiteLLM", logging.WARNING, __file__, 1, "rate limited, retrying", None, None
        )
        assert lg.filter(noisy) is False
        assert lg.filter(useful) is True

    def test_filter_not_stacked_on_repeated_setup(self):
        import logging

        from nagare_clip.logging_setup import setup_logging

        setup_logging("INFO")
        setup_logging("INFO")
        lg = logging.getLogger("LiteLLM")
        from nagare_clip.logging_setup import _DropLiteLLMNoise

        assert sum(isinstance(f, _DropLiteLLMNoise) for f in lg.filters) == 1
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/test_logging_setup.py::TestLiteLLMNoiseSuppression -v` — Expected: FAIL (levels are 0 / no `_DropLiteLLMNoise`).

- [x] **Step 3: Implement** — in `src/nagare_clip/logging_setup.py`, add after the imports:

```python
_NOISE_SNIPPETS = ("Proxy Server is not installed",)

# LiteLLM logs one multi-line INFO banner per completion() call plus a
# harmless proxy/OTel warning (with traceback) at import; both drown
# pipeline.log. Keep them only when the user asked for DEBUG.
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx")


class _DropLiteLLMNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in _NOISE_SNIPPETS)


def _quiet_litellm(root_level: int) -> None:
    if root_level <= logging.DEBUG:
        return
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    lg = logging.getLogger("LiteLLM")
    if not any(isinstance(f, _DropLiteLLMNoise) for f in lg.filters):
        lg.addFilter(_DropLiteLLMNoise())
```

and at the end of `setup_logging` (after the file-handler block) add:

```python
    _quiet_litellm(root.level)
```

- [x] **Step 4: Run tests** — `uv run pytest tests/test_logging_setup.py -v` — Expected: PASS.

- [x] **Step 5: Update docs** — `docs/stages/observability.md`: add: "`setup_logging` caps the `LiteLLM*`/`httpx` loggers at WARNING and drops the known-noise 'Proxy Server is not installed' record; set `general.log_level: DEBUG` to see LiteLLM's full chatter again."

- [x] **Step 6: Commit**

```bash
git add src/nagare_clip/logging_setup.py tests/test_logging_setup.py docs/stages/observability.md
git commit -m "fix(logging): quiet LiteLLM per-call banners and the proxy/OTel import warning"
```

---

### Task 6: timing core — internal-silence helpers + silence-aware bracket

Real-run evidence: a director line rendered `[75.8s]` where 62.9s of the span sits inside audio_silence cut ranges (already dropped by default) — the director sped it up based on a 6x-inflated number.

**Files:**
- Modify: `src/nagare_clip/timing.py`
- Test: `tests/test_timing.py` (append)

**Interfaces:**
- Produces (Tasks 7–8 consume these exact signatures):
  - `span_silence(start: float | None, end: float | None, cuts: list[tuple[float, float]]) -> float`
  - `segment_silences(seg_times: list[tuple[float | None, float | None]], cuts: list[tuple[float, float]]) -> list[float]`
  - `format_dur_gap(dur, gap, silence: float | None = None) -> str` — third positional-or-keyword param, default `None` (existing 2-arg call sites unchanged). `dur` is the **speech-only** duration; a significant `silence` renders `[13.0s speech, 62.9s silence]` (plus the usual `, gap 0.8s` when present); a `None`/negligible silence renders exactly today's bracket.

- [x] **Step 1: Write the failing tests** — append to `tests/test_timing.py`:

```python
class TestSpanSilence:
    def test_no_cuts_zero(self):
        from nagare_clip.timing import span_silence

        assert span_silence(0.0, 10.0, []) == 0.0

    def test_cut_clipped_to_span(self):
        from nagare_clip.timing import span_silence

        assert span_silence(5.0, 15.0, [(0.0, 8.0)]) == 3.0

    def test_overlapping_cuts_merged_before_summing(self):
        from nagare_clip.timing import span_silence

        assert span_silence(0.0, 10.0, [(1.0, 4.0), (3.0, 6.0)]) == 5.0

    def test_none_times_zero(self):
        from nagare_clip.timing import span_silence

        assert span_silence(None, 10.0, [(1.0, 2.0)]) == 0.0
        assert span_silence(1.0, None, [(1.0, 2.0)]) == 0.0

    def test_cut_outside_span_ignored(self):
        from nagare_clip.timing import span_silence

        assert span_silence(0.0, 5.0, [(6.0, 9.0)]) == 0.0


class TestSegmentSilences:
    def test_per_segment_mapping(self):
        from nagare_clip.timing import segment_silences

        seg_times = [(0.0, 10.0), (10.0, 20.0), (None, None)]
        cuts = [(8.0, 12.0)]
        assert segment_silences(seg_times, cuts) == [2.0, 2.0, 0.0]


class TestFormatDurGapSilence:
    def test_silence_renders_speech_silence_bracket(self):
        assert format_dur_gap(13.0, None, 62.9) == "[13.0s speech, 62.9s silence]"

    def test_silence_with_gap(self):
        assert format_dur_gap(13.0, 0.8, 62.9) == "[13.0s speech, 62.9s silence, gap 0.8s]"

    def test_negligible_silence_omitted(self):
        assert format_dur_gap(4.2, 0.8, 0.02) == "[4.2s, gap 0.8s]"
        assert format_dur_gap(4.2, None, None) == "[4.2s]"

    def test_dur_none_still_empty(self):
        assert format_dur_gap(None, None, 62.9) == ""
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/test_timing.py -v -k "Silence or SilenceS"` — Expected: FAIL (ImportError / TypeError extra arg).

- [x] **Step 3: Implement** — in `src/nagare_clip/timing.py` (module keeps its "no internal imports" invariant — the tiny merge is local):

```python
def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def span_silence(
    start: float | None, end: float | None, cuts: list[tuple[float, float]]
) -> float:
    """Seconds of ``[start, end]`` covered by the (merged) *cuts* ranges."""
    if start is None or end is None or end <= start:
        return 0.0
    total = 0.0
    for c_start, c_end in _merge_ranges(cuts):
        lo, hi = max(start, c_start), min(end, c_end)
        if hi > lo:
            total += hi - lo
    return total


def segment_silences(
    seg_times: list[tuple[float | None, float | None]],
    cuts: list[tuple[float, float]],
) -> list[float]:
    """Per-segment internal-silence seconds (0.0 for unknown times)."""
    merged = _merge_ranges(cuts)
    out: list[float] = []
    for start, end in seg_times:
        if start is None or end is None or end <= start:
            out.append(0.0)
            continue
        total = 0.0
        for c_start, c_end in merged:
            lo, hi = max(start, c_start), min(end, c_end)
            if hi > lo:
                total += hi - lo
        out.append(total)
    return out
```

and replace `format_dur_gap` with:

```python
def format_dur_gap(dur: float | None, gap: float | None, silence: float | None = None) -> str:
    """Compact bracket: ``[4.2s, gap 0.8s]`` / ``[13.0s speech, 62.9s silence]``.

    - ``dur is None`` -> ``""`` (no bracket at all).
    - ``gap is None`` -> no gap part.
    - a negligible gap (would render as ``0.0s``, incl. negative) is omitted
      the same way — "gap 0.0s" on every contiguous line/part is pure noise.
    - a significant *silence* (audio_silence-cut seconds inside the span)
      splits the duration into ``Xs speech, Ys silence`` — *dur* is then the
      speech-only figure; a negligible/absent silence renders the plain form.
    """
    if dur is None:
        return ""
    if silence is not None and f"{max(silence, 0.0):.1f}" != "0.0":
        core = f"{dur:.1f}s speech, {silence:.1f}s silence"
    else:
        core = f"{dur:.1f}s"
    if gap is None or f"{max(gap, 0.0):.1f}" == "0.0":
        return f"[{core}]"
    return f"[{core}, gap {gap:.1f}s]"
```

- [x] **Step 4: Run tests** — `uv run pytest tests/test_timing.py tests/test_config.py -v` — Expected: PASS (existing 2-arg calls unaffected).

- [x] **Step 5: Commit**

```bash
git add src/nagare_clip/timing.py tests/test_timing.py
git commit -m "feat(timing): internal-silence helpers + speech/silence duration bracket"
```

---

### Task 7: director — silence-aware brackets end-to-end

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py:198-219` (`format_numbered_transcript_timed`), `generate_director_ops` (~line 240)
- Modify: `src/nagare_clip/director/run.py` (`run_director` gains `cuts_txt`)
- Modify: `src/nagare_clip/pipeline/stages.py:320-338` (`_director_run` passes `cuts_txt`)
- Modify: `src/nagare_clip/config.py` (`DIRECTOR_PROMPT` Timing paragraph)
- Modify: `config.example.yml` (regenerated), `AGENTS.md` (director overview sentence)
- Test: `tests/director/` (append), `tests/test_config.py:363-379` (extend the pinned bracket test), `tests/pipeline/test_stages.py` (extend the director-adapter wiring pin)

**Interfaces:**
- Consumes: `segment_silences`, `format_dur_gap(dur, gap, silence)` from Task 6; `read_cuts(path) -> list[tuple[float, float]]` from `nagare_clip.audio_silence.cuts_file`.
- Produces: `format_numbered_transcript_timed(clean_lines, seg_times, silences: list[float] | None = None)`; `generate_director_ops(..., silences: list[float] | None = None)`; `run_director(..., cuts_txt: Path | None = None)`.

- [x] **Step 1: Write the failing tests** — append to `tests/director/test_director_llm.py` (match the file's existing import style):

```python
class TestTimedTranscriptSilences:
    def test_silence_split_bracket_rendered(self):
        from nagare_clip.director.director_llm import format_numbered_transcript_timed

        out = format_numbered_transcript_timed(
            ["long line", "short line"],
            [(0.0, 75.8), (80.0, 84.2)],
            silences=[62.9, 0.0],
        )
        lines = out.split("\n")
        # speech = 75.8 - 62.9 = 12.9; gap to next = 80.0 - 75.8 = 4.2
        assert lines[0] == "1: long line  [12.9s speech, 62.9s silence, gap 4.2s]"
        assert lines[1] == "2: short line  [4.2s]"

    def test_no_silences_byte_identical_to_before(self):
        from nagare_clip.director.director_llm import format_numbered_transcript_timed

        args = (["a", "b"], [(0.0, 4.2), (5.0, 9.0)])
        assert format_numbered_transcript_timed(*args) == format_numbered_transcript_timed(
            *args, silences=None
        )

    def test_speech_clamped_non_negative(self):
        from nagare_clip.director.director_llm import format_numbered_transcript_timed

        # silence bigger than the span (rounding artifacts) must not render negative speech
        out = format_numbered_transcript_timed(["x"], [(0.0, 5.0)], silences=[5.5])
        assert "-" not in out.split("  ")[1]
```

And append to `tests/director/test_run.py` (follow that file's existing fixture/fake-LLM pattern for `run_director`; the key new test):

```python
def test_run_director_reads_cuts_txt_for_silence_brackets(tmp_path, monkeypatch):
    """cuts_txt spans inside a segment must surface as 'Ns speech, Ms silence'
    in the LLM user content."""
    from nagare_clip.director.run import run_director

    edits = tmp_path / "v_edits.txt"
    edits.write_text("hello\n", encoding="utf-8")
    jsn = tmp_path / "v.json"
    jsn.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 75.8, "text": "hello"}]}),
        encoding="utf-8",
    )
    cuts = tmp_path / "v_cuts.txt"
    cuts.write_text("10.000 - 72.900\n", encoding="utf-8")

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr("nagare_clip.director.director_llm._call_llm", fake_llm)
    run_director(
        edits,
        tmp_path / "v_director.json",
        {"director": {"enabled": True, "prompt": "p", "max_retries": 0}},
        stem="v",
        json_path=jsn,
        cuts_txt=cuts,
    )
    assert "speech" in seen["user"] and "silence" in seen["user"]
```

(Adapt the cuts-file line format to whatever `write_cuts`/`read_cuts` in `nagare_clip/audio_silence/cuts_file.py` actually use — check that file first; if constructing by hand is brittle, build it with `write_cuts`.)

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/director -v -k "silence or Silences"` — Expected: FAIL (unexpected keyword `silences` / `cuts_txt`).

- [x] **Step 3: Implement `format_numbered_transcript_timed`** — replace the function body:

```python
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
```

- [x] **Step 4: Thread through `generate_director_ops`** — add keyword param `silences: list[float] | None = None` and change the timed branch to `format_numbered_transcript_timed(clean_lines, seg_times, silences=silences)`. Document in the docstring: "``silences`` (per-line audio_silence overlap, same length as ``seg_times``) splits each bracket into speech/silence; ``None`` keeps the output byte-identical."

- [x] **Step 5: Thread through `run_director`** — add param `cuts_txt: Path | None = None`; add imports `from nagare_clip.audio_silence.cuts_file import read_cuts` and `from nagare_clip.timing import segment_silences`; after the `seg_times` block:

```python
        silences = None
        if seg_times and cuts_txt and Path(cuts_txt).is_file():
            silences = segment_silences(seg_times, read_cuts(Path(cuts_txt)))
```

and pass `silences=silences` into `generate_director_ops`.

- [x] **Step 6: Orchestrator wiring** — in `pipeline/stages.py` `_director_run`, add to the `run_director(...)` call: `cuts_txt=ctx.stage_dir("audio_silence") / f"{src.stem}_cuts.txt",`. Extend the director-adapter wiring pin in `tests/pipeline/test_stages.py` (locate the existing test that asserts `run_director`'s kwargs — e.g. json_path/gaps — and add the analogous assert that `cuts_txt` points at `audio_silence/{stem}_cuts.txt`).

- [x] **Step 7: Update `DIRECTOR_PROMPT`** — in `config.py`, in the Timing paragraph, after the sentence ending `"...a line with unknown timing has no bracket."` insert:

```python
    "A line containing long internal silences splits its duration — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken; the "
    "62.9 silent seconds are dropped by default (a \"keep\" over the line "
    "preserves them). Judge pacing from the speech figure, never from "
    "speech+silence. "
```

(Keep the rest of the paragraph intact.) Extend the pinned test `tests/test_config.py::test_prompt_documents_duration_and_gap_bracket` with:

```python
    assert format_dur_gap(13.0, None, 62.9) in prompt  # "[13.0s speech, 62.9s silence]"
```

NOTE: this test is parametrized over `["director", "plan"]` — the plan prompt gets its matching paragraph in Task 8. If executing Task 7 standalone, temporarily expect the plan case to fail and complete Task 8 before pushing; when executing sequentially in one session, add the assert here and let Task 8 turn it green for plan (run only the director case in this task: `-k director`).

- [x] **Step 8: Regenerate + run** — `uv run python -m nagare_clip.config --write-example && uv run pytest tests/director tests/pipeline/test_stages.py "tests/test_config.py::test_prompt_documents_duration_and_gap_bracket[director]" -v` — Expected: PASS.

- [x] **Step 9: Update docs** — `AGENTS.md` director overview: extend the timing sentence to mention that lines with long internal silences render `[12.9s speech, 62.9s silence]` computed from the audio_silence cuts file (passed as `cuts_txt`), so pacing judgements use speech time; add `cuts_txt` to the director "Inputs" line.

- [x] **Step 10: Commit**

```bash
git add src/nagare_clip/director/ src/nagare_clip/pipeline/stages.py src/nagare_clip/config.py config.example.yml tests/director tests/pipeline/test_stages.py tests/test_config.py AGENTS.md
git commit -m "feat(director): speech/silence split in duration brackets via audio_silence cuts"
```

---

### Task 8: summary + plan — per-part silence, silence-aware part brackets

Plan reads only `summary.json`, so the per-part silence must be computed at summary time and stored there.

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py` (`PartSummary`, `build_summary`, `summary_to_dict`, `summary_from_dict`)
- Modify: `src/nagare_clip/summary/run.py` (`run_summary` gains `cuts_paths`)
- Modify: `src/nagare_clip/pipeline/stages.py:247-261` (`_summary_run` passes `cuts_paths`)
- Modify: `src/nagare_clip/plan/plan_llm.py:48-72` (`_format_parts_for_plan`)
- Modify: `src/nagare_clip/config.py` (`PLAN_PROMPT` Timing paragraph)
- Modify: `config.example.yml` (regenerated), `AGENTS.md` (summary + plan overviews)
- Test: `tests/summary/`, `tests/plan/`, `tests/pipeline/test_stages.py`, `tests/test_config.py` (plan case of the pinned bracket test)

**Interfaces:**
- Consumes: `span_silence`, `format_dur_gap(dur, gap, silence)` from Task 6; `read_cuts` from `audio_silence.cuts_file`.
- Produces: `PartSummary.silence: float | None = None`; `build_summary(..., cuts_by_stem: dict[str, list[tuple[float, float]]] | None = None)`; `run_summary(..., cuts_paths: list[Path] | None = None)`; `summary.json` parts optionally carry `"silence": <float>` (older files without it load fine).

- [x] **Step 1: Write the failing tests** — append to `tests/summary/test_summarize.py` (follow the file's existing construction patterns):

```python
class TestPartSilence:
    def test_build_summary_attaches_part_silence(self):
        # parts spanning lines whose seg span overlaps cut ranges get .silence
        # (use the file's existing fake-LLM plumbing that returns one part
        #  covering lines [1, 2]); seg_times (0.0, 10.0), (10.0, 20.0);
        # cuts_by_stem={"v": [(5.0, 15.0)]} -> part start 0.0 end 20.0 -> silence 10.0
        ...

    def test_summary_dict_round_trips_silence(self):
        from nagare_clip.summary.summarize import (
            PartSummary,
            ProjectSummary,
            summary_from_dict,
            summary_to_dict,
        )

        ps = ProjectSummary(
            summary="s",
            parts=[
                PartSummary(
                    stem="v", lines=(1, 2), summary="p", start=0.0, end=20.0, silence=10.0
                )
            ],
        )
        d = summary_to_dict(ps)
        assert d["parts"][0]["silence"] == 10.0
        back = summary_from_dict(d)
        assert back.parts[0].silence == 10.0

    def test_from_dict_without_silence_is_none(self):
        from nagare_clip.summary.summarize import summary_from_dict

        back = summary_from_dict(
            {"summary": "s", "parts": [{"stem": "v", "lines": [1, 2], "summary": "p"}]}
        )
        assert back.parts[0].silence is None
```

(Fill the first test's `...` with the file's real fake-LLM fixture — read `tests/summary/test_summarize.py` first; the assertion is `project.parts[0].silence == 10.0`.)

Append to `tests/plan/test_plan_llm.py`:

```python
def test_format_parts_renders_speech_silence_bracket():
    from nagare_clip.plan.plan_llm import _format_parts_for_plan
    from nagare_clip.summary.summarize import PartSummary, ProjectSummary

    ps = ProjectSummary(
        summary="",
        parts=[
            PartSummary(
                stem="v", lines=(1, 5), summary="long part",
                start=0.0, end=75.8, silence=62.9,
            )
        ],
    )
    out = _format_parts_for_plan(ps)
    assert "[12.9s speech, 62.9s silence]" in out


def test_format_parts_without_silence_unchanged():
    from nagare_clip.plan.plan_llm import _format_parts_for_plan
    from nagare_clip.summary.summarize import PartSummary, ProjectSummary

    ps = ProjectSummary(
        summary="",
        parts=[PartSummary(stem="v", lines=(1, 5), summary="p", start=0.0, end=4.2)],
    )
    assert "[4.2s]" in _format_parts_for_plan(ps)
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/summary tests/plan -v -k silence` — Expected: FAIL (unexpected keyword `silence`).

- [x] **Step 3: Implement summarize.py:**
  - `PartSummary`: add field `silence: float | None = None  # audio_silence-cut seconds inside [start, end]` after `end`.
  - `build_summary`: add keyword param `cuts_by_stem: dict[str, list[tuple[float, float]]] | None = None`; import `span_silence` from `nagare_clip.timing`; after the `_attach_part_times` loop add:

```python
    if cuts_by_stem:
        for p in parts:
            cuts = cuts_by_stem.get(p.stem)
            if cuts and p.start is not None and p.end is not None:
                sil = span_silence(p.start, p.end, cuts)
                if sil > 0.0:
                    p.silence = sil
```

  - `summary_to_dict`: after the `end` emission add:

```python
        if p.silence is not None:
            entry["silence"] = p.silence
```

  - `summary_from_dict`: read leniently next to start/end:

```python
            silence = raw.get("silence")
            silence = float(silence) if isinstance(silence, (int, float)) else None
```

    and pass `silence=silence` into the `PartSummary(...)` construction.

- [x] **Step 4: Implement run_summary** — add param `cuts_paths: list[Path] | None = None`; import `read_cuts` from `nagare_clip.audio_silence.cuts_file`; in the enabled branch build:

```python
        cuts_by_stem: dict[str, list[tuple[float, float]]] = {}
        for cpath in cuts_paths or []:
            if cpath.is_file():
                stem = cpath.stem.removesuffix("_cuts")
                cuts_by_stem[stem] = read_cuts(cpath)
```

and pass `cuts_by_stem=cuts_by_stem or None` to `build_summary`.

- [x] **Step 5: Orchestrator wiring** — `_summary_run` in `pipeline/stages.py` gains `cuts_paths=[ctx.stage_dir("audio_silence") / f"{s}_cuts.txt" for s in ctx.stems],`. Extend the summary-adapter wiring pin in `tests/pipeline/test_stages.py` accordingly.

- [x] **Step 6: Implement plan formatting** — in `_format_parts_for_plan`, replace the `dur`/`bracket` lines with:

```python
        raw = p.end - p.start if p.start is not None and p.end is not None else None
        sil = p.silence if isinstance(p.silence, (int, float)) and p.silence > 0 else None
        dur = max(raw - sil, 0.0) if raw is not None and sil else raw
```

(keep the existing `gap` computation) and `bracket = format_dur_gap(dur, gap, sil)`.

- [x] **Step 7: Update `PLAN_PROMPT`** — in its Timing paragraph, after `"...a part with unknown timing has no bracket."` insert:

```python
    "A part containing long internal silences splits its duration — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken; the "
    "silent seconds are dropped by default (\"keep\" preserves them). Judge "
    "pacing from the speech figure. "
```

- [x] **Step 8: Regenerate + run** — `uv run python -m nagare_clip.config --write-example && uv run pytest tests/summary tests/plan tests/pipeline/test_stages.py tests/test_config.py -v` — Expected: PASS, including both parametrized cases of `test_prompt_documents_duration_and_gap_bracket`.

- [x] **Step 9: Update docs** — `AGENTS.md`: summary overview — mention parts optionally carry `silence` (audio_silence overlap, via `cuts_paths`) next to the existing `start`/`end` sentence, and add `cuts_paths` to summary's Inputs; plan overview — extend the context-line example note to mention the speech/silence split form.

- [x] **Step 10: Commit**

```bash
git add src/nagare_clip/summary/ src/nagare_clip/plan/plan_llm.py src/nagare_clip/pipeline/stages.py src/nagare_clip/config.py config.example.yml tests/summary tests/plan tests/pipeline/test_stages.py tests/test_config.py AGENTS.md
git commit -m "feat(summary,plan): per-part internal silence stored in summary.json and rendered as speech/silence brackets"
```

---

### Task 9: gap_context SSIM static prefilter (implement LAST)

Real-run evidence: 23/54 vision calls returned STATIC and were discarded downstream (~40% of stage cost). Frames already exist; one ffmpeg SSIM comparison per gap inside the same batch container can skip pixel-static gaps before the LLM call.

**Files:**
- Modify: `src/nagare_clip/gap_context/snapshot.py` (add `ssim_relpath`, `parse_ssim_stats`)
- Modify: `src/nagare_clip/pipeline/external.py:85-123` (`build_snapshot_batch_cmd` gains `ssim_jobs`)
- Modify: `src/nagare_clip/gap_context/describe.py:44-55` (`GapFrames` gains `ssim`)
- Modify: `src/nagare_clip/pipeline/stages.py:144-208` (`_extract_gap_frames` plans + reads SSIM)
- Modify: `src/nagare_clip/gap_context/run.py` (`run_gap_context` skip branch)
- Modify: `src/nagare_clip/config.py` (`GapContextConfig.static_ssim`)
- Modify: `config.example.yml` (regenerated), `docs/stages/gap_context.md`, `AGENTS.md` (gap_context overview)
- Test: `tests/gap_context/test_snapshot.py`, `tests/pipeline/test_external.py`, `tests/gap_context/test_run.py` (append)

**Interfaces:**
- Produces:
  - `ssim_relpath(stem: str, start: float, end: float) -> str` → `"frames/{stem}/ssim_{start:.3f}-{end:.3f}.txt"`
  - `parse_ssim_stats(text: str) -> float | None` — the `All:` score from an ffmpeg `ssim=stats_file=` output line
  - `build_snapshot_batch_cmd(project_root, jobs, width, ssim_jobs: Sequence[tuple[str, str, str]] = ())` — each ssim job is `(first_frame_container_path, last_frame_container_path, stats_container_path)`
  - `GapFrames.ssim: float | None = None`
  - config `gap_context.static_ssim` (float, default 0.99; 0 disables)

- [x] **Step 1: Write the failing pure-helper tests** — append to `tests/gap_context/test_snapshot.py`:

```python
class TestSsimHelpers:
    def test_ssim_relpath(self):
        from nagare_clip.gap_context.snapshot import ssim_relpath

        assert ssim_relpath("v", 108.4, 119.25) == "frames/v/ssim_108.400-119.250.txt"

    def test_parse_ssim_stats_reads_all_score(self):
        from nagare_clip.gap_context.snapshot import parse_ssim_stats

        line = "n:1 Y:0.994828 U:0.998750 V:0.998691 All:0.996132 (24.123456)\n"
        assert parse_ssim_stats(line) == 0.996132

    def test_parse_ssim_stats_garbage_is_none(self):
        from nagare_clip.gap_context.snapshot import parse_ssim_stats

        assert parse_ssim_stats("") is None
        assert parse_ssim_stats("no scores here") is None
        assert parse_ssim_stats("All:notanumber") is None
```

And append to `tests/pipeline/test_external.py` (mirror the style of `test_build_snapshot_batch_cmd_*` at line 162):

```python
def test_build_snapshot_batch_cmd_appends_ssim_jobs_after_extraction():
    from nagare_clip.pipeline.external import build_snapshot_batch_cmd

    jobs = [("in.mp4", 1.0, "/output/gap_context/frames/v/1.000.jpg")]
    ssim_jobs = [
        (
            "/output/gap_context/frames/v/1.000.jpg",
            "/output/gap_context/frames/v/9.000.jpg",
            "/output/gap_context/frames/v/ssim_0.800-9.200.txt",
        )
    ]
    cmd = build_snapshot_batch_cmd(Path("/proj"), jobs, 960, ssim_jobs=ssim_jobs)
    script = cmd[-1]
    lines = script.split("\n")
    assert len(lines) == 2
    assert "-frames:v 1" in lines[0]  # extraction first
    assert "ssim=stats_file=" in lines[1]  # comparison after
    assert lines[1].endswith("|| true")
    assert "-f null" in lines[1]


def test_build_snapshot_batch_cmd_no_ssim_jobs_is_byte_identical():
    from nagare_clip.pipeline.external import build_snapshot_batch_cmd

    jobs = [("in.mp4", 1.0, "/out/f.jpg")]
    assert build_snapshot_batch_cmd(Path("/p"), jobs, 960) == build_snapshot_batch_cmd(
        Path("/p"), jobs, 960, ssim_jobs=()
    )
```

- [x] **Step 2: Run to verify failure** — `uv run pytest tests/gap_context/test_snapshot.py tests/pipeline/test_external.py -v -k ssim` — Expected: FAIL (ImportError / unexpected keyword).

- [x] **Step 3: Implement the pure helpers** — in `snapshot.py` (add `import re` at top):

```python
_SSIM_ALL_RE = re.compile(r"\bAll:([0-9.]+)")


def ssim_relpath(stem: str, start: float, end: float) -> str:
    """SSIM stats-file path (relative to the gap_context stage dir) for a gap."""
    return f"frames/{stem}/ssim_{start:.3f}-{end:.3f}.txt"


def parse_ssim_stats(text: str) -> float | None:
    """The ``All:`` score from an ffmpeg ``ssim=stats_file=`` output line."""
    m = _SSIM_ALL_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None
```

In `external.py`, change the signature to `def build_snapshot_batch_cmd(project_root: Path, jobs: Sequence[tuple[str, float, str]], width: int, ssim_jobs: Sequence[tuple[str, str, str]] = ()) -> list[str]:`, extend the docstring ("*ssim_jobs* appends one first-vs-last frame SSIM comparison per gap after all extraction lines — same container, ~10ms each; the stats file is parsed host-side to prefilter pixel-static gaps. A failed comparison (`|| true`, no stats file) simply disables the prefilter for that gap."), and before `script = "\n".join(lines)` add:

```python
    lines += [
        "ffmpeg -hide_banner -nostats -loglevel error -nostdin "
        f"-i {shlex.quote(first)} -i {shlex.quote(last)} "
        f"-filter_complex {shlex.quote(f'ssim=stats_file={stats_out}')} -f null - || true"
        for first, last, stats_out in ssim_jobs
    ]
```

- [x] **Step 4: Run helper tests** — `uv run pytest tests/gap_context/test_snapshot.py tests/pipeline/test_external.py -v` — Expected: PASS.

- [x] **Step 5: Write the failing run-level test** — append to `tests/gap_context/test_run.py` (follow that file's existing enabled-stage fixture style):

```python
def test_static_ssim_prefilter_skips_vision_call(tmp_path):
    """A gap whose frames scored >= static_ssim is written as static:true
    without any LLM call; a below-threshold gap still calls the LLM."""
    from nagare_clip.describe_stub_or_real import ...  # use the file's existing fake-LLM pattern
```

Concretely (adapting to the file's real fixtures): build two `GapFrames` with real dummy frame files, set `ssim=0.999` on one and `ssim=0.5` on the other, run `run_gap_context` with `{"gap_context": {"enabled": True, "static_ssim": 0.99, ...}}` and a counting fake `_call_llm` (monkeypatched as the file already does). Assert: the fake was called exactly once; the written `_gaps.json` contains BOTH gaps; the prefiltered one has `"static": true` and a description containing `"prefilter"`.

- [x] **Step 6: Run to verify failure** — `uv run pytest tests/gap_context/test_run.py -v -k prefilter` — Expected: FAIL (both gaps hit the LLM; `static_ssim` unknown is tolerated by `.get`, so the failure mode is call-count 2 ≠ 1).

- [x] **Step 7: Implement:**
  - `describe.py` `GapFrames`: add `ssim: float | None = None  # first-vs-last frame SSIM (pixel-static prefilter)` after `relpaths`.
  - `run.py` `run_gap_context`: inside the loop, before the `describe_gap` call:

```python
        threshold = float(gc_cfg.get("static_ssim", 0.0))
        # (hoist this line above the loop)
```

```python
            if threshold > 0.0 and gf.ssim is not None and gf.ssim >= threshold:
                unit = f"{stem}_gap{i + 1:02d}"
                recorder.begin(unit)
                recorder.flush_unit(
                    unit, outcome=OK, reason=f"static prefilter (ssim {gf.ssim:.4f})"
                )
                gaps.append(
                    Gap(
                        start=gf.start,
                        end=gf.end,
                        frames=gf.relpaths,
                        description=(
                            f"static (prefilter: frames nearly identical, ssim {gf.ssim:.3f})"
                        ),
                        static=True,
                    )
                )
                continue
```

    (import `OK` from `nagare_clip.llm_report` and `Gap` is already imported).
  - `config.py` `GapContextConfig`: after `frame_width` add:

```python
    static_ssim: float = Field(
        0.99,
        ge=0.0,
        le=1.0,
        description="Skip the vision call when the gap's first/last frame SSIM is at least this (pixel-static prefilter; the gap is recorded as static). 0 disables",
    )
```

  - `pipeline/stages.py` `_extract_gap_frames`: extend the per-gap planning — change the `entries` tuple to `(start, end, frame_entries, ssim_host)` where:

```python
            ssim_host: Path | None = None
            if len(frame_entries) >= 2:
                ssim_rel = ssim_relpath(src.stem, start, end)
                ssim_host = d / ssim_rel
                ssim_jobs.append(
                    (
                        f"/output/gap_context/{frame_entries[0][1]}",
                        f"/output/gap_context/{frame_entries[-1][1]}",
                        f"/output/gap_context/{ssim_rel}",
                    )
                )
            entries.append((start, end, frame_entries, ssim_host))
```

    (initialize `ssim_jobs: list[tuple[str, str, str]] = []` next to `jobs`; import `ssim_relpath`, `parse_ssim_stats` from `nagare_clip.gap_context.snapshot`; pass `ssim_jobs=ssim_jobs` to `build_snapshot_batch_cmd`). In the result loop, read it back:

```python
            ssim: float | None = None
            if ssim_host is not None and ssim_host.is_file():
                ssim = parse_ssim_stats(ssim_host.read_text(encoding="utf-8"))
            out.append(
                GapFrames(start=start, end=end, frames=frames, relpaths=relpaths, ssim=ssim)
            )
```

- [x] **Step 8: Regenerate + full suite** — `uv run python -m nagare_clip.config --write-example && uv run pytest tests/gap_context tests/pipeline tests/test_config.py -v` — Expected: PASS.

- [x] **Step 9: Calibrate the default threshold against real data (best-effort).** The water_pump_3 run has real frames for 23 known-static and 31 known-action gaps (`/mnt/work/YouTube/water_pump_3/video-editor-ai/gap_context/frames/`, verdicts in the `_gaps.json` files' `static` flags). Compute SSIM for a handful of each via the whisperx image, e.g.:

```bash
cd /home/diginah/ghq/github.com/diginatu/nagare-clip
OUT=/mnt/work/YouTube/water_pump_3/video-editor-ai
docker compose run --rm --user 0:0 -v "$OUT:/output" --entrypoint sh whisperx -c \
  'ffmpeg -hide_banner -loglevel error -i /output/gap_context/frames/<stem>/<first>.jpg \
   -i /output/gap_context/frames/<stem>/<last>.jpg -filter_complex ssim -f null - 2>&1'
```

(Adjust the volume flag to however docker-compose.yml actually mounts `/output` — check it first; the pipeline sets this via env vars, see `_docker_env` in `pipeline/stages.py`.) If most true-static gaps score below 0.99, lower the default (e.g. 0.97) **only if** no true-action gap scores above the new value; record the measured numbers in the commit message. If Docker/the frames are unavailable, keep 0.99 and note it as uncalibrated.

- [x] **Step 10: Update docs** — `docs/stages/gap_context.md`: new subsection "Pixel-static prefilter (`static_ssim`)": frames-first-vs-last SSIM computed inside the same batch container (`ssim=stats_file=`, parsed by `snapshot.parse_ssim_stats`); a score ≥ threshold writes the gap as `static: true` with a `(prefilter…)` description and **no vision call** (report records `static prefilter` with 0 attempts); failed comparison → no stats file → prefilter disabled for that gap; pixel-static is a subset of the LLM's semantic STATIC, so below-threshold gaps still rely on the ACTION/STATIC marker; hand-flip `"static": false` to force a gap back in, exactly as before. `AGENTS.md` gap_context overview: one sentence about the prefilter + the `static_ssim` key.

- [x] **Step 11: Commit**

```bash
git add src/nagare_clip/gap_context/ src/nagare_clip/pipeline/ src/nagare_clip/config.py config.example.yml tests/gap_context tests/pipeline docs/stages/gap_context.md AGENTS.md
git commit -m "feat(gap_context): SSIM pixel-static prefilter skips vision calls on unchanged frames"
```

---

### Task 10: docs sync + full verification

**Files:**
- Modify: `README.md` (user-facing: `intervals.min_cut`, `gap_context.static_ssim`, speech/silence brackets — wherever the affected stages/config are described)
- Modify: `plan.md` (implementation/status notes for this batch)
- Verify: everything

- [x] **Step 1: README/plan.md sync** — read both, add the user-visible changes from Tasks 1–9 where the corresponding features are documented (config keys with defaults; the new bracket notation in the director/plan descriptions if mentioned).

- [x] **Step 2: Full check** — `make check` — Expected: lint, format-check, validate, and the whole pytest suite PASS.

- [x] **Step 3: Real-data spot check (optional but recommended)** — rerun the pipeline tail on the review project:

```bash
cd /mnt/work/YouTube/water_pump_3 && ./run_nagare_clip.sh --from-stage intervals --to-stage intervals
```

then confirm in `video-editor-ai/intervals/PXL_20260324_092107933_intervals.json` that no inter-keep gap is below 0.25s anymore (the Task 1 acceptance criterion on real data; the review found 42). Do NOT rerun LLM stages (costs money, and summary.json is hand-edited).

- [x] **Step 4: Commit**

```bash
git add README.md plan.md
git commit -m "docs: sync README/plan for the water_pump_3 review improvement batch"
```
