# "keep" means two things one stage apart (2026-07-29)

## Problem

The word `keep` carried two unrelated meanings in adjacent stages:

- **plan (editorial sense).** `PLAN_PROMPT` asked for "a ROUGH editorial
  direction — what to do with it (e.g. remove, shorten, speed up, **keep**,
  emphasise)". Here `keep` means *this part earns its place; don't drop it*.
- **director (mechanical sense).** `keep` is an op that "protects a span from
  cutting, INCLUDING its silences/non-speech gaps (which are dropped by
  default)".

`plan.json` is fed to the director as context, so a direction reading
`keep — …` sits in the prompt right next to an op list where `keep` is a valid
op name. The director copies the word across — and the second meaning is far
more expensive than the first: "don't drop this part" concerns the *spoken*
content (which is never dropped by default anyway), while the op additionally
restores **every silent second** in the range.

### Evidence (water_pump_3, 2026-07-30 run)

`plan.json` returned `keep` for 8 of 12 parts. `director` then emitted keep ops
over `[77,96]` (20 lines), `[97,131]` (35 lines), `[132,138]` (7 lines) — the
notes (「試行錯誤の核心部分」) make the *plan* sense obvious. Result: 62-71% of
each source kept, finished runtime 22.3min → **53.9min** against a brief asking
for 20-30, with `cut` ops falling from 21 to 4.

## Decision

Both halves of the wanted behaviour, because the rename alone leaves the op's
blast radius mismatched to its purpose:

### 1. The plan vocabulary no longer contains the word

`PLAN_PROMPT` offers `feature` instead of `keep` in its verb list and its JSON
example, drops the two `"keep" preserves …` mechanical asides (the plan stage
has no business describing an op), and gains an explicit rule:

> Never use the word "keep" in a direction: a later stage reads it as a
> mechanical instruction to restore every silent second of the part. Say
> "feature", "retain" or "emphasise" instead.

Silent moments that genuinely matter are still expressible — the prompt says to
state it in the direction and leave the mechanics to a later stage.

`DIRECTOR_PROMPT`'s keep bullet closes the loop from the other side: a
project-context direction saying a part should be featured/retained/emphasised
is editorial emphasis, **not** a request for a keep op.

### 2. The keep op is narrowed to what it is for

Prompt: use the NARROWEST range that covers the gap, normally `[N, N+1]`; keep
is "not a way to mark a span as important: speech is never dropped by default,
so a wide keep adds nothing but dead air".

Enforcement: new `director.max_keep_lines` (default **4**, `0` = no limit).
A `keep` op wider than that is **dropped** at parse time, logged, and recorded
in the LLM report as a dropped item.

Design points worth keeping in mind:

- **Dropped, not clipped.** Clipping to the first N lines would restore an
  arbitrary silence the director never chose. Dropping degrades to the default
  behaviour — speech kept, internal silence cut — which is exactly the
  non-runaway outcome. A `keep` never protects *speech*; only silence.
- **Enforced on the LLM response only.** `try_parse_director_response` applies
  the cap; `ops_from_dict` deliberately does not, so a human hand-editing the
  `_director.json` intermediate (a documented workflow, like `_cuts.txt`) can
  still write a wide keep and mean it.
- **The limit is stated in the prompt at runtime**
  (`keep_limit_note()`, appended by `generate_director_ops` when the limit is
  > 0) rather than hard-coded into `DIRECTOR_PROMPT`, so the number the LLM is
  told is always the number the parser enforces.
- **Residual risk, accepted:** `keep` also protects a span from an overlapping
  `cut` op in `guided_edit` (cut ops clip around existing tags). Dropping a wide
  keep removes that protection — but the prompt already forbids a `cut` range
  from overlapping any other op's range, and the pre-existing cut-last ordering
  handles the narrow case.

## Tests

- `tests/test_config.py::test_plan_prompt_never_offers_keep_as_a_direction_word`
  — every occurrence of the word in `PLAN_PROMPT` must be the rule forbidding it.
- `…::test_plan_prompt_example_directions_are_not_director_op_names` — the JSON
  example's verbs are checked against `director_llm.VALID_TYPES`, so any future
  op name leaking into the plan vocabulary fails here.
- `…::test_director_prompt_narrows_keep_to_gap_rescue`, `…::test_director_max_keep_lines_default`.
- `tests/director/test_director_llm.py::TestKeepWidthLimit` — at/over the limit,
  the drop message, `0` = unlimited, only keep ops capped, `ops_from_dict`
  uncapped, cfg plumbing, and the prompt note matching the enforced number.
