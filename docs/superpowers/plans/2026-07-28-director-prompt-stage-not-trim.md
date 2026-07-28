# Director prompt: tighten AND stage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `DIRECTOR_PROMPT` in `src/nagare_clip/config.py` so the director's
stated goal is "tighten and stage" instead of pure removal — giving it real guidance
on overlay usage, cut-vs-speed judgment for repetition that builds to a payoff, and a
rule for reading a long silent gap as a possible keep-worthy moment instead of
default dead air.

**Architecture:** Single prompt-text change (4 edits inside one Python string
constant), guarded by 4 new pinned tests in `tests/test_config.py` written and
confirmed red first, plus a regenerated `config.example.yml` and two doc updates
(`AGENTS.md`, `plan.md`). No code/logic/schema changes anywhere else.

**Tech Stack:** Python, pytest, pydantic-settings (`config.py`'s `DEFAULTS` dict is
derived from the models — regenerating `config.example.yml` is a `make` target, not a
manual edit).

## Global Constraints

- Full design + exact prompt text already approved: see
  `docs/superpowers/specs/2026-07-28-director-prompt-stage-not-trim-design.md`. Copy
  the prompt text from there verbatim — do not improvise wording, since Task 1's
  tests are pinned to it.
- Do not touch `director_llm.py`, `context.py`, `run.py`, or the op schema.
- Do not add a new config field (spec's Non-goals: overlay density is prose only).
- Do not touch `README.md` (spec's Non-goals).
- Every existing pinned prompt test in `tests/test_config.py` must keep passing
  unmodified (verified compatible in the design doc) — do not edit them.
- Run `uv run pytest` / `uv run python -m ...` — never bare `python`/`pytest` (repo
  convention, `AGENTS.md` "Python Execution").

---

### Task 1: Write and red-confirm the four new pinned prompt tests

**Files:**
- Modify: `tests/test_config.py` (add after
  `test_director_prompt_overlay_example_carries_a_duration`, around line 526)

**Interfaces:**
- Consumes: `get_effective_config(None, {})` (already imported/used throughout this
  test file) — `cfg["director"]["prompt"]` is the string under test.
- Produces: nothing consumed by later tasks; these tests are the acceptance gate for
  Task 2's prompt rewrite.

- [ ] **Step 1: Add the four test functions**

Append this block to `tests/test_config.py`, directly after
`test_director_prompt_overlay_example_carries_a_duration` (currently ends at line 526
with the closing `assert ops[0].duration is not None and ops[0].duration > 0`):

```python
def test_director_prompt_frames_goal_as_tighten_and_stage():
    """The prompt's opening framing must ask for staging, not just removal --
    otherwise the director has no license to add overlays or keep a moment
    (see docs/superpowers/specs/2026-07-28-director-prompt-stage-not-trim-design.md)."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    opening = prompt.split("\n\n")[0].lower()
    assert "tighten" in opening
    assert "stage" in opening


def test_director_prompt_treats_long_gap_as_keep_candidate_when_speech_announces_event():
    """A long gap must not be an unconditional 'dead air' rule -- the prompt
    must tell the director to read the surrounding speech and keep a gap that
    the speech says is meaningful (an accident, a cleanup, a wait for a result)."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"].lower()
    assert "fallback" in prompt
    assert "accident" in prompt
    assert "watchable moment" in prompt


def test_director_prompt_prefers_speed_for_buildup_reserves_cut_for_digressions():
    """Repetition that builds toward a payoff should be sped up, not cut --
    the prompt must say so explicitly, reserving cut for spans that leave the
    throughline entirely."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"].lower()
    assert "payoff" in prompt
    assert "throughline" in prompt
    assert "buildup" in prompt


def test_director_prompt_documents_overlay_density_target():
    """Overlay needs a sense of when/how often -- a loose numeric target that
    an editorial brief (future feature) can override, not a hard rule."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    assert "3-5 minutes" in prompt
    assert "editorial brief" in prompt.lower()
```

- [ ] **Step 2: Run the new tests and confirm all four fail**

Run: `uv run pytest tests/test_config.py -k "frames_goal_as_tighten_and_stage or treats_long_gap_as_keep_candidate or prefers_speed_for_buildup or documents_overlay_density_target" -v`

Expected: 4 failures (the current `DIRECTOR_PROMPT` has none of `"stage"` in its
opening, `"fallback"`/`"accident"`/`"watchable moment"`, `"payoff"`/`"throughline"`/
`"buildup"`, or `"3-5 minutes"`/`"editorial brief"`). If any test unexpectedly passes,
stop and re-check the assertion against the current prompt text in `config.py` before
proceeding — a passing red-phase test is a bug in the test, not a shortcut.

- [ ] **Step 3: Commit**

```bash
git add tests/test_config.py
git commit -m "$(cat <<'EOF'
test(director): pin new tighten-and-stage prompt guidance (red)

Four new tests assert the director prompt frames its goal as staging (not
just trimming), treats a long gap as a keep candidate when the surrounding
speech announces something happened, prefers speed over cut for buildup,
and gives overlay a loose density target. All four currently fail against
DIRECTOR_PROMPT -- Task 2 makes them pass.

Claude-Session: https://claude.ai/code/session_01TXsieqPYRnKajurTqfL3Lh
EOF
)"
```

---

### Task 2: Rewrite `DIRECTOR_PROMPT` and regenerate `config.example.yml`

**Files:**
- Modify: `src/nagare_clip/config.py:153-213` (the `DIRECTOR_PROMPT` assignment)
- Modify: `config.example.yml` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: Task 1's four tests (`tests/test_config.py`) as the acceptance gate.
- Produces: the new `DIRECTOR_PROMPT` string, consumed by Task 3's doc updates (which
  describe its content in prose, not by import) and by every existing pinned prompt
  test (must still pass, see Global Constraints).

- [ ] **Step 1: Replace `DIRECTOR_PROMPT` in `src/nagare_clip/config.py`**

Replace the entire `DIRECTOR_PROMPT = (...)` assignment at `src/nagare_clip/config.py:153-213`
with:

```python
DIRECTOR_PROMPT = (
    "You are a video editor. You receive a Japanese transcript as "
    "numbered lines (one line per subtitle segment). Decide high-level "
    "edits to tighten AND STAGE the video: cut what drags, but also mark "
    "the moments that make it worth watching — turning points, payoffs, "
    "failures, mishaps. Do NOT rewrite or output the "
    "transcript text. Output ONLY a JSON object.\n"
    "\n"
    "Timing: a line may carry a bracket after its text — [4.2s, gap 0.8s] "
    "means the line lasts 4.2 seconds and is followed by a 0.8-second silent "
    "gap before the next line. A negligible gap, and the last line, show no "
    "gap ([4.2s]); a line with "
    "unknown timing has no bracket. "
    "A line containing long internal silences splits its duration — "
    "[13.0s speech, 62.9s silence] means only 13.0 seconds are spoken; the "
    '62.9 silent seconds are dropped by default (a "keep" over the line '
    "preserves them). Judge pacing from the speech figure, never from "
    "speech+silence. "
    "Use these numbers to judge pacing: long "
    "durations are candidates for cutting or speeding up. A long gap is "
    "dead air by default, already dropped — but that is the fallback "
    "reading, not the only one. Check the speech just before and after it: "
    "if it announces something happening (an accident, a cleanup, a wait "
    "for a result), the gap itself may be the most watchable moment in the "
    'shot, and a "keep" spanning that line and the next (lines [N, N+1]) '
    "preserves it.\n"
    "\n"
    "Visual context: an indented line like\n"
    "    [silent gap 12.4s: a build runs and logs scroll past]\n"
    "may follow a numbered line. It describes what is VISIBLE on screen "
    "during the silence after that line (nobody is speaking). Such gaps are "
    "dropped by default. If the gap shows something worth watching, emit a "
    '"keep" op spanning that line and the next one — a keep over lines '
    "[N, N+1] preserves the silence between them. Annotation lines are not "
    "numbered; never reference them as op lines.\n"
    "\n"
    "Operations (reference lines by their 1-based numbers, inclusive). "
    "Prefer speed over cut for repetition that builds toward a payoff "
    "(failed attempts, retries, warm-up) — the buildup is part of the "
    "story, so tighten it rather than deleting it; reserve cut for spans "
    "that leave the throughline entirely (digressions, dead ends, "
    "redundant retakes with no payoff):\n"
    "- cut: remove a boring/redundant span entirely (deletes audio+video).\n"
    '- speed: play a span faster; give "factor" (e.g. 2.0). Internal silences/pauses are still dropped — add a "keep" over the same lines to preserve them while sped up.\n'
    '- overlay: show an on-screen caption; give "text" and "duration" '
    "(how many seconds it stays on screen). Pick the duration from reading "
    "length — a short label needs about 2 seconds, a full sentence 4 to 6; "
    'never a fixed value. Its "lines" say WHERE it appears (the caption '
    "starts at the first line of the range), not how long it shows. Reach "
    "for it at turning points, conclusions, failures, and mishaps — "
    "moments worth labeling on screen. Aim for roughly one overlay per "
    "3-5 minutes of finished video as a loose default target; if an "
    "editorial brief states otherwise, follow the brief instead.\n"
    "- keep: protect a span from cutting, INCLUDING its silences/"
    "non-speech gaps (which are dropped by default).\n"
    '- edit: request a fine within-line text deletion/fix; describe it in "note".\n'
    "\n"
    "JSON shape:\n"
    '{"ops": [\n'
    '  {"type": "cut", "lines": [12, 18], "note": "why / where precisely"},\n'
    '  {"type": "speed", "lines": [30, 34], "factor": 2.0, "note": "..."},\n'
    '  {"type": "overlay", "lines": [5, 5], "text": "ポイント", "duration": 2.0, "note": ""},\n'
    '  {"type": "keep", "lines": [40, 42], "note": "..."},\n'
    '  {"type": "edit", "lines": [7, 7], "note": "delete the redundant restatement"}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    '- "lines" must be within the transcript range.\n'
    '- A "cut" range must not overlap any other op\'s range: cutting deletes '
    "the span, so never include a line you also keep/speed/overlay in a cut "
    "(e.g. to cut lines 12-18 but keep line 18, emit cut [12, 17]). "
    "Overlapping ops are clipped and the cut loses the shared lines.\n"
    '- Use "note" to describe in natural language precisely WHERE in the '
    "line(s) the edit starts and ends, so a downstream editor can place "
    "it exactly.\n"
    "- Output only the JSON object, no other text."
)
```

- [ ] **Step 2: Run Task 1's four tests and confirm they now pass**

Run: `uv run pytest tests/test_config.py -k "frames_goal_as_tighten_and_stage or treats_long_gap_as_keep_candidate or prefers_speed_for_buildup or documents_overlay_density_target" -v`

Expected: 4 passed.

- [ ] **Step 3: Run the full `test_config.py` file to confirm no existing pinned test broke**

Run: `uv run pytest tests/test_config.py -v`

Expected: all pass, including (by name, since these are the ones touching
`DIRECTOR_PROMPT`): `test_director_defaults_present`,
`test_director_prompt_documents_speed_does_not_keep_silence`,
`test_prompt_documents_duration_and_gap_bracket[director]`,
`test_director_prompt_documents_gap_annotations`,
`test_director_prompt_gap_example_matches_the_real_formatter`,
`test_director_prompt_overlay_example_carries_a_duration`.

If any of these fail, the new prompt text was transcribed incorrectly relative to
Step 1 (a stray edit) — diff the pasted block against this plan's Step 1 text
character-for-character rather than patching the failing test.

- [ ] **Step 4: Regenerate `config.example.yml`**

Run: `make config-example`

This is a generated file — do not hand-edit it. It re-derives `config.example.yml`
from the pydantic models, embedding the new `DIRECTOR_PROMPT` text as the commented
sample for `director.prompt`.

- [ ] **Step 5: Confirm the generator/example sync test passes**

Run: `uv run pytest tests/test_config.py::test_example_file_matches_generator -v`

Expected: PASS. (This test exists already; it fails if `config.example.yml` drifts
from what `make config-example` produces — Step 4 should already have fixed any
drift, so this step is a confirmation, not expected to require further changes.)

- [ ] **Step 6: Run the full test suite and repo validation**

Run: `make check`

Expected: PASS (lint + format-check + validate + full pytest suite). If `ruff format`
flags the new `config.py` block, run `make format` and re-run `make check`.

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml
git commit -m "$(cat <<'EOF'
feat(director): rewrite DIRECTOR_PROMPT to tighten AND stage the video

Real-run evidence (water_pump_3): overlays were scarce (4 across 25.8min
of finished video) with no usage guidance, and a director run dropped the
project's strongest moment -- a 48.8s gap holding a flooding accident and
its cleanup -- because the prompt classified every long gap as dead air
with no exception.

Four prose edits: the opening goal now says "tighten AND STAGE"; a long
gap is read as a keep candidate when the surrounding speech announces an
event, with "dead air" demoted to the fallback reading; a new preamble
prefers speed over cut for repetition that builds toward a payoff,
reserving cut for spans that leave the throughline entirely; and the
overlay bullet gains a when-to-use rule plus a loose ~1-per-3-5-minutes
density target that an editorial brief can override.

See docs/superpowers/specs/2026-07-28-director-prompt-stage-not-trim-design.md.

Claude-Session: https://claude.ai/code/session_01TXsieqPYRnKajurTqfL3Lh
EOF
)"
```

---

### Task 3: Update `AGENTS.md` and `plan.md`

**Files:**
- Modify: `AGENTS.md:101` (end of the `director` stage paragraph)
- Modify: `plan.md` (append a new dated section at the end of the file, after the
  existing "Overlay duration stated by the director (2026-07-28)" section which ends
  at line 122)

**Interfaces:**
- Consumes: the shipped `DIRECTOR_PROMPT` text from Task 2 (described in prose here,
  not imported).
- Produces: nothing consumed by later tasks — this is the terminal documentation task.

- [ ] **Step 1: Append a sentence to `AGENTS.md`'s director paragraph**

In `AGENTS.md`, the `### director — LLM High-Level Edit Operations (Pass A)` section's
single paragraph currently ends (line 101) with:

```
...Only applies when `seg_times` is also present (the annotation needs anchor times); an absent/empty `gaps` file leaves the prompt byte-identical to before this feature.
```

Append this sentence directly after it (same paragraph, no blank line):

```
 The default `director.prompt` frames its goal as tighten-AND-stage rather than pure trimming: it prefers `speed` over `cut` for repetition that builds toward a payoff (reserving `cut` for spans that leave the throughline entirely), gives `overlay` a when-to-use rule (turning points, conclusions, failures, mishaps) plus a loose ~1-per-3-5-minutes density target, and reads an ordinary long timing-gap as a possible keep candidate — not just a `gap_context`-described one — when the speech immediately before/after it announces an accident, cleanup, or other event, with "dead air" as the fallback reading rather than the only one.
```

- [ ] **Step 2: Append a new dated section to `plan.md`**

Append to the end of `plan.md` (after the existing final paragraph, which currently
ends with `Breaking change by design: an existing _edits.txt carrying <overlay
text="...">…</overlay> is rejected rather than migrated, so no overlay's duration can
come from a tag position any more.`):

```markdown

## Director prompt: tighten AND stage, not just trim (2026-07-28)

**Status: complete.** `DIRECTOR_PROMPT` framed its goal purely as removal ("tighten
the video"), with two consequences visible in a real run (`water_pump_3`, 90.8min
source, `output/llm_report/director/`): overlays were scarce and unguided (4 ops
across 25.8min of finished video for a brief asking for a fast, punchy vlog), and a
long silent gap holding the project's strongest moment — water flooding the floor,
then cleanup, announced by the speech on both sides of a 48.8s gap between lines 131
and 132 — was dropped, because the prompt classified every long gap as dead air with
no exception. This was a regression with a date: before the silence-aware duration
bracket landed, the director kept this span (the old bracket rendered the whole span
as one long duration, an accidental signal that the more accurate bracket correctly
removed, with no real rule ever put in its place).

Four prose edits to `DIRECTOR_PROMPT` (no schema/code changes): the opening goal now
reads "tighten AND STAGE"; the Timing paragraph's gap sentence demotes "dead air" to
the fallback reading and adds a rule — check the speech immediately before/after a
long gap, and if it announces something happening (an accident, a cleanup, a wait for
a result), `keep [N, N+1]` may be worth it; a new preamble before the Operations list
prefers `speed` over `cut` for repetition that builds toward a payoff, reserving `cut`
for spans that leave the throughline entirely; and the `overlay` bullet gains a
when-to-use rule (turning points, conclusions, failures, mishaps) plus a loose ~1-per-
3-5-minutes density target, phrased so a future editorial-brief-injection feature can
override it without a code change. See
`docs/superpowers/specs/2026-07-28-director-prompt-stage-not-trim-design.md` for the
full design and exact prompt text, and `tests/test_config.py`'s four new
`test_director_prompt_*` tests for the pinned guarantees.
```

- [ ] **Step 3: Run `make check` once more to confirm nothing broke from the doc edits**

Run: `make check`

Expected: PASS. (Doc-only changes shouldn't affect this, but it's the cheapest way to
catch an accidental syntax slip, e.g. in a shell-quoted markdown block.)

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md plan.md
git commit -m "$(cat <<'EOF'
docs(director): document the tighten-and-stage prompt rewrite

Claude-Session: https://claude.ai/code/session_01TXsieqPYRnKajurTqfL3Lh
EOF
)"
```
