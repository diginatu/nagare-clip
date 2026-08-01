# Speed Span Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a concrete span/duration limit to the director's `speed` bullet in `DIRECTOR_PROMPT`, and name what to use instead once a stretch exceeds it, so the span constrains as reliably as the existing 1.3-2.0 factor band does.

**Architecture:** Single prompt-text edit in `src/nagare_clip/config.py` (the `- speed:` bullet, currently `config.py:206`). No parser/validation code changes — `director_llm.py`'s handling of a `speed` op's `factor` is untouched, and stays an unconstrained positive float (see `tests/director/test_director_llm.py::test_a_bare_speed_op_is_untouched_by_the_floor`, which must keep passing).

**Tech Stack:** Python 3, pytest, the repo's `NagareClipConfig` pydantic-settings models (`src/nagare_clip/config.py`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-01-speed-span-limit-design.md`.
- The `- speed:` bullet must stay a single physical line in the `DIRECTOR_PROMPT` string (one `\n`-terminated Python string literal) — `test_director_prompt_documents_speed_does_not_keep_silence` reads it via `next(ln for ln in prompt.splitlines() if ln.startswith("- speed:"))`, which would break if the bullet were split across lines.
- No code enforcement of the span limit (no new drop/clip logic in `director_llm.py`) — prompt-only, per the design's "Design" section.
- Run `uv run pytest` (not bare `pytest`) and `uv run python -m ...` for any Python invocation, per `AGENTS.md`'s Python Execution section.
- Per the global TDD guidance: write each new test against the **current, unmodified** bullet first, run it, confirm it **fails**, only then edit the bullet, then confirm the new tests (and all pre-existing pinned tests) pass. Report the fail→pass evidence, not just the final green run.

---

### Task 1: Span-limit prompt bullet + test coverage + docs

**Files:**
- Modify: `src/nagare_clip/config.py:206` (the `- speed:` bullet inside `DIRECTOR_PROMPT`)
- Modify: `tests/test_config.py` (add two new tests after `test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent`, currently ending at line 450, before `test_director_prompt_speed_example_is_a_mild_accent` at line 453)
- Modify: `AGENTS.md` (the director section's sentence about the "1.3–2.0 accent band", inside the single long paragraph starting at line 101)
- Read-only reference: `docs/superpowers/specs/2026-08-01-speed-span-limit-design.md`

**Interfaces:**
- Consumes: `get_effective_config(None, {})["director"]["prompt"]` (existing config accessor, unchanged signature) and the existing `_speed_bullet()` test helper in `tests/test_config.py:364-367` (unchanged, reused as-is).
- Produces: nothing new is consumed by later tasks — this is the only task in this plan.

- [ ] **Step 1: Write the two new failing tests in `tests/test_config.py`**

Insert immediately after `test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent` (which ends at line 450) and before `test_director_prompt_speed_example_is_a_mild_accent` (line 453):

```python
def test_director_prompt_speed_span_has_a_duration_limit():
    """Improvement 14: the factor band alone constrained faithfully in a real
    run, but nothing bounded the span's length -- two long stretches (11-13
    minutes of source each) got covered by a mild speed instead of a
    timelapse or cut, reproducing the register problem the two-mode split
    was meant to prevent. The bullet needs a number for the span the way it
    already has one for the factor."""
    bullet = _speed_bullet().lower()
    assert "handful of lines" in bullet
    assert "30 seconds" in bullet


def test_director_prompt_speed_span_limit_names_the_alternative():
    """A length limit with no alternative just gets ignored once a stretch
    exceeds it: the bullet must say what to reach for instead, for both the
    worth-going-fast-over case (timelapse) and the case where it isn't
    (cut)."""
    bullet = _speed_bullet().lower()
    assert "not a job for speed at all" in bullet
    assert '"timelapse"' in bullet
    assert '"cut"' in bullet
```

- [ ] **Step 2: Run the two new tests to verify they fail against the current bullet**

Run: `uv run pytest tests/test_config.py::test_director_prompt_speed_span_has_a_duration_limit tests/test_config.py::test_director_prompt_speed_span_limit_names_the_alternative -v`

Expected: both `FAIL` — the current bullet (`config.py:206`) contains neither "handful of lines"/"30 seconds" nor "not a job for speed at all". This is the mutation-catch evidence: it proves the assertions are load-bearing before the prompt text exists to satisfy them.

- [ ] **Step 3: Edit the `- speed:` bullet in `src/nagare_clip/config.py`**

The current line 206 reads (verify with `grep -n '^    .- speed:' src/nagare_clip/config.py` first, since line numbers may have shifted):

```python
    '- speed: play a span slightly faster; give "factor". It is an ACCENT, not a dial for shaving time off speech: stay in the 1.3 to 2.0 band, use it on a short span, and never let it become the register the video runs in. It protects nothing — unlike a "keep", the silences and pauses inside its span are still dropped — so a fast factor here plays back as sped-up jump cuts. When a span is manual work worth going genuinely fast over, emit a "timelapse" instead.\n'
```

Replace it with:

```python
    '- speed: play a span slightly faster; give "factor". It is an ACCENT, not a dial for shaving time off speech: stay in the 1.3 to 2.0 band, keep the span itself short — a handful of lines, well under 30 seconds of finished video once the speed is applied — and never let it become the register the video runs in. It protects nothing — unlike a "keep", the silences and pauses inside its span are still dropped — so a fast factor here plays back as sped-up jump cuts. A longer stretch that needs compressing is not a job for speed at all: emit a "timelapse" if it is manual work worth going genuinely fast over, or a "cut" if it is not.\n'
```

Use the Edit tool with the exact old/new strings above (the whole line, so the match is unambiguous) rather than a partial substring replace.

- [ ] **Step 4: Run the two new tests again to verify they pass**

Run: `uv run pytest tests/test_config.py::test_director_prompt_speed_span_has_a_duration_limit tests/test_config.py::test_director_prompt_speed_span_limit_names_the_alternative -v`

Expected: both `PASS`.

- [ ] **Step 5: Run the full existing prompt-pinning suite to confirm no regressions**

Run: `uv run pytest tests/test_config.py -v -k "director_prompt or director_defaults"`

Expected: all `PASS`, including (unchanged by this edit, but must still hold against the new bullet text):
- `test_director_prompt_documents_speed_does_not_keep_silence`
- `test_director_prompt_keeps_the_two_mode_choice_across_both_ops`
- `test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent`
- `test_director_prompt_speed_example_is_a_mild_accent`

If any of these fail, the bullet edit broke an existing invariant it wasn't supposed to touch — fix the wording (not the test) and re-run.

- [ ] **Step 6: Confirm the parser-side "speed stays unconstrained" test still passes**

Run: `uv run pytest tests/director/test_director_llm.py -k test_a_bare_speed_op_is_untouched_by_the_floor -v`

Expected: `PASS`, unchanged — this plan makes no code changes to `director_llm.py`, so this is a confirmation, not a new assertion.

- [ ] **Step 7: Regenerate and check `config.example.yml`**

Run: `make config-example && git status --porcelain config.example.yml`

Expected: no output from `git status --porcelain` (the generator elides prompt bodies, so the file should be byte-identical). If it does show a diff, inspect it — it should not happen per the design doc's "Docs to update" section, and if it does, stage the regenerated file as part of this task's commit.

- [ ] **Step 8: Update `AGENTS.md`'s director section**

In the single long paragraph starting at `AGENTS.md:101`, find this substring (part of the sentence about `speed`/`timelapse`):

```
with a bare `speed` op limited to the 1.3–2.0 accent band and never the tool for shaving time off speech (a real run put 57% of the finished video under speed, no span above 2.0x, 85% of it over captioned speech)
```

Replace it with:

```
with a bare `speed` op limited to the 1.3–2.0 accent band and a span of a handful of lines / well under 30s of finished video, never the tool for shaving time off speech (a real run put 57% of the finished video under speed, no span above 2.0x, 85% of it over captioned speech; a later run reproduced the same register problem through span length alone once the factor band held but the span didn't, which is why a longer stretch is now explicitly routed to `timelapse` or `cut` instead)
```

Use the Edit tool with `old_string` set to the first snippet above and `new_string` set to the second — both are substrings of the one-line paragraph, so an exact match is required (copy them verbatim, including the em-dash and backtick characters).

- [ ] **Step 9: Run the full test suite and lint/format checks**

Run: `make check`

Expected: all of lint, format-check, validate, and test pass. If `make check` is unavailable in this environment (e.g. sandboxed without Docker for `docker compose config`), instead run `uv run ruff check . && uv run ruff format --check . && uv run pytest`.

- [ ] **Step 10: Commit**

```bash
git add src/nagare_clip/config.py tests/test_config.py AGENTS.md
git commit -m "feat(director): put a number on the speed op's span limit"
```

(Include `config.example.yml` in the `git add` only if Step 7 showed a diff.)

---

## Self-Review Notes

- **Spec coverage:** the spec's two required changes (span number + named alternative) are both in Step 3; the spec's testing section maps to Steps 1-2 (new tests, verified failing first) and Step 4-6 (new + existing tests passing); the spec's "Docs to update" section maps to Steps 7-8.
- **Placeholder scan:** none — every step has literal old/new text, exact commands, and exact expected output.
- **Type/name consistency:** `_speed_bullet()` (existing helper, `tests/test_config.py:364-367`) is reused as-is by both new tests, matching its existing call signature (`() -> str`) and existing callers' usage pattern (`.lower()` before substring checks, same as `test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent`).
