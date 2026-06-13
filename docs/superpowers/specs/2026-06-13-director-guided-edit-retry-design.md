# Retry for director & guided_edit LLM stages

## Problem

Both LLM stages currently get a single shot:

- **director** (`director_llm.generate_director_ops`) calls the LLM once. On a
  connection error or unparseable JSON it silently yields `[]` — the whole
  high-level edit pass is lost on one transient failure.
- **guided_edit** (`apply.apply_ops`) calls the LLM once per op. On an exception
  **or** a failed `verify_op` (verbatim-safety / reflection check), the op is
  reverted into `_unapplied.txt` with no second chance.

Local LLM output at low temperature is noisy: a single bad sample can drop an
op or the entire op list. A bounded retry recovers most of these cheaply.

## Goal

Add bounded, configurable retry to both stages, nudging temperature upward on
each successive attempt so a deterministic low-temperature failure is not
reproduced identically.

## Design

### director — `generate_director_ops`

- Add `try_parse_director_response(response, num_lines) -> Optional[List[DirectorOp]]`
  returning `None` on a **hard parse failure** (invalid JSON / no `ops` array),
  else the (possibly empty) validated op list. The existing
  `parse_director_response` becomes a thin wrapper: `return try_parse_director_response(...) or []`
  (keeps current callers and tests unchanged).
- Loop up to `max_retries + 1` attempts. An attempt **fails** when:
  - `call_llm` raises, **or**
  - `try_parse_director_response` returns `None` (hard parse failure).
- A valid-but-empty `{"ops": []}` is a legitimate "no edits" result → success,
  **no retry**.
- After exhausting attempts, fall back to `[]` (preserves the current graceful
  no-op contract).

### guided_edit — `apply_ops` / `_apply_one_op`

- Per op, loop up to `max_retries + 1` attempts. An attempt **fails** when:
  - `_apply_one_op` raises, **or**
  - `verify_op` returns a non-`None` reason.
- First success (verify `None`) is accepted; processing continues to the next op.
- After exhausting attempts, the op is reverted into `unapplied` with the **last**
  failure reason (same destination as today, just delayed).

### Temperature nudge (both stages)

- Attempt 0 uses the configured `temperature`.
- Each retry adds `retry_temp_step`, capped at `retry_temp_cap`:
  `temp(attempt) = min(base + step * attempt, cap)`.
- Implemented by passing a shallow-copied cfg with an overridden `temperature`
  to `call_llm` (which already reads `cfg["temperature"]`). The base cfg is not
  mutated.

### Config

Add to both `director:` and `guided_edit:` sections (DEFAULTS + `config.example.yml`):

| key | default | meaning |
|-----|---------|---------|
| `max_retries` | `2` | extra attempts after the first (`0` = single attempt = today's behaviour) |
| `retry_temp_step` | `0.2` | temperature increment per retry |
| `retry_temp_cap` | `0.8` | maximum temperature any retry uses |

Step/cap are configurable because the best temperature varies by model.

## Testing (TDD)

A fake `call_llm` records each call's `cfg` (for temperature assertions) and is
scripted to fail/succeed per attempt.

**director** (`tests/director/`):
- LLM raises on attempt 1, succeeds on attempt 2 → ops returned, 2 calls.
- Unparseable JSON on attempts 1–2, valid on 3 → ops returned, 3 calls.
- All attempts fail → returns `[]`, exactly `max_retries + 1` calls.
- Valid `{"ops": []}` → returns `[]`, exactly **1** call (no retry).
- `max_retries=0` → exactly 1 call even on failure.
- Temperature strictly increases per attempt and is capped at `retry_temp_cap`.

**guided_edit** (`tests/guided_edit/`):
- op LLM raises then succeeds → op applied, `unapplied` empty.
- verify fails then succeeds → op applied.
- all attempts fail → op in `unapplied` with last reason, `max_retries + 1` calls.
- `max_retries=0` → 1 call, op straight to `unapplied` on failure.
- Temperature increments per attempt, capped.

Each new test is mutation-checked against a deliberately broken implementation
(remove the retry loop / the temperature bump / the `None`-vs-empty distinction)
to confirm it fails red before the real implementation makes it green.

## Out of scope

- Backoff/sleep between attempts (local LLM, no rate limit).
- Corrective-hint prompting on retry (rejected during brainstorming in favour of
  the simpler temperature nudge).
- All-ops-dropped (valid JSON, every individual op invalid) is **not** a retry
  trigger — only a hard parse failure (`None`) is.

## Documentation

On completion update `README.md`, `plan.md`, and `CLAUDE.md` (the per-stage
config tables and the director/guided_edit stage descriptions).
