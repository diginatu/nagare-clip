# Speed as a Listening/Timelapse Choice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the director from parking `speed` at 1.5x over most of a video by making the prompt present speed as a choice between playing at 1x and doing a real timelapse.

**Architecture:** Prompt-only change to `DIRECTOR_PROMPT` in `src/nagare_clip/config.py`. Two edits: the `- speed:` bullet becomes a two-mode selector, and the paragraph introducing the op list routes "prefer speed over cut" by whether the repetition is visible work or speech. No parser, config field, or enforcement path changes — `parse_director_response()` already accepts any positive factor and keeps doing so. Guarded by string-level pins in `tests/test_config.py`, the same technique every other director-prompt rule uses.

**Tech Stack:** Python 3.11+, pydantic-settings config models, pytest, ruff, uv.

**Spec:** [`docs/superpowers/specs/2026-08-01-speed-two-mode-choice-design.md`](../specs/2026-08-01-speed-two-mode-choice-design.md)

## Global Constraints

- Run every Python tool through `uv run` (e.g. `uv run pytest`).
- `DIRECTOR_PROMPT` is built from adjacent Python string literals. **One prompt line per bullet**: the `- speed:` bullet must stay a single logical line (one `"..."` literal ending in `\n`, no embedded `\n`), because `tests/test_config.py::test_director_prompt_documents_speed_does_not_keep_silence` selects it with `next(ln for ln in prompt.splitlines() if ln.startswith("- speed:"))`.
- Ruff `line-length = 100` but `E501` is ignored, so a long single-line string literal is fine and is what the existing `- speed:` and `- overlay:` bullets already do.
- Prompts do not appear in `config.example.yml` (the generator elides them as `# prompt: "..."`), so no regeneration is expected — but `make config-example` is run once at the end to confirm nothing drifted.
- Per `CLAUDE.md`: the implementation (a prompt string) is edited before its tests can assert on it, so **every new assertion must be verified against a mutated prompt** — delete/alter the clause it guards, run the test, see it fail, restore. Report that evidence alongside the final green run. A passing test proves nothing unless you have seen it fail.
- Do not touch `PLAN_PROMPT` — the spec puts it explicitly out of scope.

---

### Task 1: The `speed` bullet becomes a two-mode selector

**Files:**
- Modify: `src/nagare_clip/config.py` (the `- speed:` line inside `DIRECTOR_PROMPT`, currently line 203)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `get_effective_config(None, {})["director"]["prompt"]` — the merged default prompt string.
- Produces: a `DIRECTOR_PROMPT` whose single `- speed:` line contains the two-mode rule. Task 2 edits a different region of the same string and must not reflow this line.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, immediately after the existing
`test_director_prompt_documents_speed_does_not_keep_silence`:

```python
def _speed_bullet() -> str:
    """The single DIRECTOR_PROMPT line describing the `speed` op."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    return next(ln for ln in prompt.splitlines() if ln.startswith("- speed:"))


def test_director_prompt_makes_speed_a_two_mode_choice():
    """speed must read as a choice between playing at 1x and a real timelapse,
    not as a dial -- a mild sustained fast-forward is the failure this guards
    (see docs/superpowers/specs/2026-08-01-speed-two-mode-choice-design.md)."""
    bullet = _speed_bullet().lower()
    assert "not a dial" in bullet
    assert "listening" in bullet
    assert "timelapse" in bullet
    assert "1x" in bullet
    # The listening mode's alternative to a mild speed-up is a cut, not a
    # slower speed.
    assert "cut" in bullet


def test_director_prompt_timelapse_states_its_price_and_its_partners():
    """A timelapse loses intelligible audio; saying so is what forces an honest
    choice instead of a mild speed-up that splits the difference.  It also has
    to be paired with keep (so it is continuous) and usually overlay (so the
    viewer still knows what is happening)."""
    bullet = _speed_bullet().lower()
    assert "4.0" in bullet  # the fast floor
    assert "unintelligible" in bullet
    assert "keep" in bullet
    assert "overlay" in bullet


def test_director_prompt_marks_mild_speed_factors_as_the_exception():
    """1.3-2.0 was the entire observed range of a real run (57% of the finished
    video).  The prompt must name that band as the exception, not the default."""
    bullet = _speed_bullet().lower()
    assert "1.3" in bullet and "2.0" in bullet
    assert "exception" in bullet
    assert "accent" in bullet
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py -k "two_mode_choice or timelapse_states_its_price or mild_speed_factors" -v
```

Expected: all three FAIL against the current prompt (`assert "not a dial" in bullet` and friends), because today's bullet is only
`- speed: play a span faster; give "factor" (e.g. 2.0). Internal silences/pauses are still dropped — add a "keep" over the same lines to preserve them while sped up.`

- [ ] **Step 3: Rewrite the bullet**

In `src/nagare_clip/config.py`, replace this single line:

```python
    '- speed: play a span faster; give "factor" (e.g. 2.0). Internal silences/pauses are still dropped — add a "keep" over the same lines to preserve them while sped up.\n'
```

with this single line (still one literal, no embedded `\n`):

```python
    '- speed: play a span faster; give "factor". Speed is NOT a dial for shaving time off speech — it is a choice between two modes, and you must pick one. LISTENING: the speech carries something the viewer needs — emit no speed op, play it at 1x; if it drags, cut the weakest parts instead. TIMELAPSE: the span is manual work whose speech is inessential — go genuinely fast (factor 4.0 or more) and accept that the words become unintelligible; that sacrifice is the point of the mode and is why you must be sure the speech is not needed. Pair a timelapse with a "keep" over the same lines so the work runs continuously instead of becoming sped-up jump cuts (internal silences/pauses are dropped otherwise), and usually with an "overlay" saying what is happening, since the narration is no longer doing that job. Factors between 1.3 and 2.0 are the exception, not the default: most of the video plays at 1x, and speed is an accent, not the register the whole video runs in.\n'
```

- [ ] **Step 4: Run the new tests plus the pre-existing speed pin**

Run:

```bash
uv run pytest tests/test_config.py -k "speed" -v
```

Expected: PASS, including the untouched
`test_director_prompt_documents_speed_does_not_keep_silence` (the new bullet still
contains `keep` and `silences` on that one line).

- [ ] **Step 5: Verify each new assertion catches a broken prompt (mutation check)**

For each mutation below: apply it to the `- speed:` line, run the command, confirm the
named test FAILS, then restore the line exactly.

| # | Mutation to the bullet | Command | Must fail |
|---|---|---|---|
| 1 | change `NOT a dial` → `a dial` | `uv run pytest tests/test_config.py -k two_mode_choice -v` | `test_director_prompt_makes_speed_a_two_mode_choice` |
| 2 | delete the sentence starting `TIMELAPSE:` up to `not needed.` | `uv run pytest tests/test_config.py -k timelapse_states_its_price -v` | `test_director_prompt_timelapse_states_its_price_and_its_partners` |
| 3 | delete the final sentence starting `Factors between 1.3` | `uv run pytest tests/test_config.py -k mild_speed_factors -v` | `test_director_prompt_marks_mild_speed_factors_as_the_exception` |
| 4 | delete the sentence starting `Pair a timelapse` | `uv run pytest tests/test_config.py -k timelapse_states_its_price -v` | `test_director_prompt_timelapse_states_its_price_and_its_partners` |

After the last restore, re-run `uv run pytest tests/test_config.py -k speed -v` and confirm it is green again.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/config.py tests/test_config.py
git commit -m "feat(director): make speed a listening/timelapse choice, not a dial"
```

---

### Task 2: Route "prefer speed over cut" by what is on screen

**Files:**
- Modify: `src/nagare_clip/config.py` (the paragraph introducing the op list inside `DIRECTOR_PROMPT`, currently lines 196-201)
- Test: `tests/test_config.py` (rewrite `test_director_prompt_prefers_speed_for_buildup_reserves_cut_for_digressions`, currently at line 551, and add one new test)

**Interfaces:**
- Consumes: the `DIRECTOR_PROMPT` from Task 1 (its `- speed:` bullet is not touched here).
- Produces: the final `DIRECTOR_PROMPT`. Task 3 documents it; no code depends on the wording beyond `tests/test_config.py`.

**Context:** this paragraph is what licensed the mild speed-up over talking. Its
tighten-rather-than-delete instinct comes from
`docs/superpowers/specs/2026-07-28-director-prompt-stage-not-trim-design.md` and must
survive — the existing test asserts the words `payoff`, `throughline` and `buildup`
are present, and those assertions stay.

- [ ] **Step 1: Write the failing tests**

Replace the existing test at `tests/test_config.py:551` with the version below, and
add the new test after it:

```python
def test_director_prompt_prefers_speed_for_visible_work_reserves_cut_for_digressions():
    """Repetition that is VISIBLE WORK should be timelapsed rather than deleted --
    the tighten-rather-than-delete instinct from the 2026-07-28 rewrite -- while
    cut stays reserved for spans that leave the throughline entirely."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"].lower()
    assert "payoff" in prompt
    assert "throughline" in prompt
    assert "buildup" in prompt
    assert "visible work" in prompt


def test_director_prompt_does_not_offer_speed_as_a_way_to_tighten_speech():
    """The prefer-speed-over-cut paragraph must route repeated SPEECH to 1x or a
    cut.  Left ungated it reads as a licence to shave talking with a mild
    speed-up, which is how a real run put 57% of the finished video under speed
    with 85% of that footage carrying captions."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    # The paragraph is one prompt line ending in ":" above the bullet list --
    # select it alone, so the `- speed:` bullet's own wording cannot satisfy
    # these assertions for it.
    para = next(ln for ln in prompt.splitlines() if "Prefer speed over cut" in ln).lower()
    assert "speech" in para
    assert "speed is not the tool" in para
    assert "1x" in para
    assert "cut the weakest passes" in para
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py -k "visible_work or way_to_tighten_speech" -v
```

Expected: both FAIL — `"visible work"` is absent from the prompt, and the paragraph
containing `Prefer speed over cut` says nothing about speech.

- [ ] **Step 3: Rewrite the paragraph**

In `src/nagare_clip/config.py`, replace these lines:

```python
    "Operations (reference lines by their 1-based numbers, inclusive). "
    "Prefer speed over cut for repetition that builds toward a payoff "
    "(failed attempts, retries, warm-up) — the buildup is part of the "
    "story, so tighten it rather than deleting it; reserve cut for spans "
    "that leave the throughline entirely (digressions, dead ends, "
    "redundant retakes with no payoff):\n"
```

with:

```python
    "Operations (reference lines by their 1-based numbers, inclusive). "
    "Prefer speed over cut where the repetition is VISIBLE WORK building "
    "toward a payoff (failed attempts, assembly, waiting for a result) — "
    "the buildup is part of the story, so timelapse it rather than "
    "deleting it. Where the repetition is SPEECH, speed is not the tool: "
    "leave it at 1x, or cut the weakest passes. Reserve cut for spans "
    "that leave the throughline entirely (digressions, dead ends, "
    "redundant retakes with no payoff):\n"
```

- [ ] **Step 4: Run the whole config test module**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: PASS, including every other director-prompt pin (`tighten_and_stage`,
`overlay_density_target`, `keep_width`, the gap/bracket example tests) and
`test_example_file_matches_generator`.

- [ ] **Step 5: Verify each new assertion catches a broken prompt (mutation check)**

For each mutation: apply, run, confirm FAIL, restore.

| # | Mutation to the paragraph | Command | Must fail |
|---|---|---|---|
| 1 | change `VISIBLE WORK building` → `repetition building` | `uv run pytest tests/test_config.py -k visible_work -v` | `test_director_prompt_prefers_speed_for_visible_work_reserves_cut_for_digressions` |
| 2 | delete the sentence `Where the repetition is SPEECH, speed is not the tool: leave it at 1x, or cut the weakest passes.` | `uv run pytest tests/test_config.py -k way_to_tighten_speech -v` | `test_director_prompt_does_not_offer_speed_as_a_way_to_tighten_speech` |
| 3 | change `leave it at 1x, or cut the weakest passes` → `use a gentle factor` | `uv run pytest tests/test_config.py -k way_to_tighten_speech -v` | `test_director_prompt_does_not_offer_speed_as_a_way_to_tighten_speech` |

After the last restore, re-run `uv run pytest tests/test_config.py -v` and confirm green.

- [ ] **Step 6: Confirm the prompt still parses as an instruction the director stage can use**

Run the director stage's own test suite, which exercises `generate_director_ops`
against the default config:

```bash
uv run pytest tests/director -v
```

Expected: PASS (no test asserts on the old wording; this catches an accidental
broken literal, e.g. a lost `\n` merging two bullets).

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/config.py tests/test_config.py
git commit -m "feat(director): stop offering speed as a way to tighten speech"
```

---

### Task 3: Documentation and full validation

**Files:**
- Modify: `AGENTS.md` (director section, the closing sentence of the long paragraph at line 101 that states the prefer-speed rule)
- Verify only: `config.example.yml`, `README.md`

**Interfaces:**
- Consumes: the final `DIRECTOR_PROMPT` from Tasks 1 and 2.
- Produces: nothing code-facing.

**Context:** there is no `docs/stages/director.md` — the director's deep-dive text
lives in `AGENTS.md`. `README.md`'s `<speed>` passages describe marker *mechanics*
(how the tag behaves in intervals/blender), which this change does not touch, so no
README edit is expected. The repo has no `plan.md` despite the documentation policy
naming one.

- [ ] **Step 1: Update the `AGENTS.md` director paragraph**

Find this clause in the final sentence of the director section's long paragraph
(`AGENTS.md` line 101):

```
it prefers `speed` over `cut` for repetition that builds toward a payoff (reserving `cut` for spans that leave the throughline entirely),
```

Replace it with:

```
it treats `speed` as a two-mode choice rather than a dial — either the speech is worth listening to (no speed op; `cut` the weakest parts if it drags) or the span is manual work worth a real timelapse (factor 4.0+, audio deliberately sacrificed, paired with a `keep` so it runs continuously and usually an `overlay` since the narration no longer explains it), with factors of 1.3–2.0 called out as the exception rather than the default (a real run put 57% of the finished video under speed, no span above 2.0x, 85% of it over captioned speech) — and it prefers `speed` over `cut` only where the repetition is VISIBLE WORK building toward a payoff, routing repeated SPEECH to 1x or a cut and reserving `cut` for spans that leave the throughline entirely,
```

- [ ] **Step 2: Confirm the example config did not drift**

Run:

```bash
make config-example && git diff --stat config.example.yml
```

Expected: empty diff — the generator elides prompt bodies, so a prompt edit does
not move the file. If it *did* change, commit the regenerated file with the rest.

- [ ] **Step 3: Run the full validation suite**

Run:

```bash
make check
```

Expected: PASS — `lint`, `format-check`, `validate`, and the whole `pytest` suite.

- [ ] **Step 4: Read the final prompt end to end**

Run:

```bash
uv run python -c "from nagare_clip.config import get_effective_config; print(get_effective_config(None, {})['director']['prompt'])"
```

Read the output as the LLM would. Confirm: the speed bullet is one line, the
two-mode rule and the prefer-speed paragraph agree rather than contradict, and no
sentence still offers a mild speed-up over speech.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md config.example.yml
git commit -m "docs(director): describe speed as a listening/timelapse choice"
```
