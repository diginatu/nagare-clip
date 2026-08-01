# Give the speed span a number, the way the factor band already has one

Date: 2026-08-01
Stage: `director` (prompt only)

## Problem

The `speed` bullet in `DIRECTOR_PROMPT` constrains the factor with a number (1.3
to 2.0) but constrains the span only with a word:

> `stay in the 1.3 to 2.0 band, use it on a short span, and never let it become
> the register the video runs in.`

### Evidence — 2026-08-01 18:03 run, `water_pump_3`

The numeric constraint was obeyed exactly — every speed op landed in the
1.3-2.0 band. The wordless one was not:

| speed op | lines | source span | on screen |
|---|---|---|---|
| `[58, 96]` x1.5 | 39 | 13.2 min | 3.54 min |
| `[145, 178]` x1.5 | 34 | 11.5 min | 1.84 min |
| `[33, 50]` x1.5 | 18 | 4.6 min | 1.68 min |

1.3-1.5x now covers 49% of the finished video, while `timelapse` dropped to two
ops covering 10.2% (previously five timelapses and no mild speed at all). This
is the same register problem
[2026-08-01's speed-two-mode-choice design](2026-08-01-speed-two-mode-choice-design.md)
already fixed once, reopened by the one dimension that design left unbounded.

The `timelapse` bullet has gained a factor rule, worked examples, and
motion/repetition guidance across two rounds of tightening; `speed` asks for
none of that. Given a long stretch to compress, the cheaper op wins whenever
nothing closes that door.

## Design

Prompt-only, same as the factor band it mirrors. `director_llm.py` already
treats `speed`'s factor as an unconstrained primitive
(`test_a_bare_speed_op_is_untouched_by_the_floor` pins this deliberately) and
this change does not touch that: the evidence above shows a plain numeric
instruction in prompt text is enough to constrain the model's output without a
parser-level check, exactly as the factor band already demonstrates. No new
config field, no drop/clip logic.

### The `speed` bullet gains a span limit and names the alternative

Current close of the bullet:

> `use it on a short span, and never let it become the register the video runs
> in. [...] When a span is manual work worth going genuinely fast over, emit a
> "timelapse" instead.`

New close:

> `keep the span itself short — a handful of lines, well under 30 seconds of
> finished video once the speed is applied — and never let it become the
> register the video runs in. [...] A longer stretch that needs compressing is
> not a job for speed at all: emit a "timelapse" if it is manual work worth
> going genuinely fast over, or a "cut" if it is not.`

Two changes from the current wording:

1. A number replaces "short" — "a handful of lines, well under 30 seconds of
   finished video" — so the span constrains the way the factor band does.
2. The old redirect only fired for manual work ("emit a timelapse instead").
   The new one fires on length regardless of content: past the limit, `speed`
   is off the table and the choice is between `timelapse` (worth going fast
   over) and `cut` (not). This is the missing door the evidence shows the
   director walking through by default.

The bullet stays one prompt line, like every other op bullet — the existing
`test_director_prompt_documents_speed_does_not_keep_silence` reads the single
line starting `- speed:`, so the new clauses are appended in place, not put on
a new line.

30 seconds is picked from the issue's own framing ("roughly half a minute of
finished video") and sits comfortably under every real speed op observed so far
(1.68-3.54 min on screen) — a real accent-length span (a few seconds of a
sentence) clears it easily, while every span in the evidence table would now be
over the line and have to become a `timelapse` or a `cut`.

### Out of scope

No change to the 1.3-2.0 factor band, the timelapse bullet, or any parser code
in `director_llm.py`. No global/cross-op budget (summing `speed` coverage
across the whole op list) — the issue itself notes the director can't evaluate
that while writing one op at a time; a per-op limit is checkable where the
decision is made.

## Testing

New pins in `tests/test_config.py`, alongside the existing `_speed_bullet()`
helper and `test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent`:

- the bullet states the span limit ("handful of lines", "30 seconds");
- the bullet names both alternatives for a longer stretch ("not a job for
  speed at all", `"timelapse"`, `"cut"`).

Existing pins that must keep passing unchanged (already checked against the
drafted bullet text above):
`test_director_prompt_documents_speed_does_not_keep_silence`,
`test_director_prompt_keeps_the_two_mode_choice_across_both_ops`,
`test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent`.

Because the implementation is a prompt string that already exists in
`config.py`, the new tests are written first against the *current* bullet,
run to confirm they fail, then the bullet is edited and the tests re-run to
confirm they pass — the mutation-catch evidence TDD normally gets from
breaking an implementation, produced here by writing the test before the text
it's meant to guard.

## Docs to update

- `AGENTS.md` — the director section's sentence about the 1.3-2.0 accent band
  gains the span limit and the timelapse-or-cut redirect.
- `config.example.yml` — no change expected (the generator elides prompt
  bodies); run `make config-example` once to confirm
  `tests/test_config.py::test_example_file_matches_generator` stays green.
- `README.md` — describes `<speed>` marker mechanics, which don't change; no
  edit expected.

There is no `docs/stages/director.md`; the director's deep-dive text lives in
`AGENTS.md`. `plan.md` named by the documentation policy does not exist in the
repo, so nothing to update there.
