# A wide keep is wrong for talking, right for a highlight

Date: 2026-07-31
Stage: `director` (prompt + `max_keep_lines` default)

## Problem

`DIRECTOR_PROMPT` rules out wide `keep` ops outright — "use the NARROWEST range
that covers the gap, normally `[N, N+1]`… a wide keep adds nothing but dead air
and inflates the runtime". That reasoning holds only while speech is the sole
thing worth keeping. During a highlight the *silence* carries the shot, and
chopping it up produces jump cuts in the one place the viewer wants continuity.

The wording came from commit 5b976f3, which stopped `keep` being used as an
"this part is important" marker (that abuse took a 22.3-minute cut to 53.9
minutes). The fix worked, but it removed the legitimate case with the abuse.

### Evidence — 2026-07-30 run, `water_pump_3`

Every `keep` op across all three sources is exactly two lines: `[112,113]`,
`[130,131]`, `[169,170]`, `[51,52]`, `[2,3]`. The `[N, N+1]` example became the
only shape the model produces.

The best moment in the project is water spilling onto the floor and the cleanup
after it, lines 128-132 of `PXL_20260324_092107933`. The director recognised it
— its note on `keep [130,131]` reads
「line130「おーなに」直後の6.7s gap：水がこぼれ始める瞬間の映像を保持」 — but the
two-line shape rescued only one gap. Two cuts survive inside the sequence:

| cut | length | lost |
|---|---|---|
| 2522.0 → 2526.6 | 4.6s | the water actually starting to spill |
| 2556.0 → 2604.5 | 48.5s | mopping the floor |

Widening that op to `[128, 132]` costs 53.8s, taking the cut from 24.7 to 25.6
minutes against a brief asking for 20-30. The narrow rule is not buying runtime
the project needs.

## Design

Width follows what is on screen. Nothing in the enforcement code path changes;
the discrimination lives in the prompt, where intent lives.

### 1. The `keep` bullet in `DIRECTOR_PROMPT`

Replace the "NARROWEST … adds nothing but dead air" sentences with a two-case
rule:

- one silent gap → the narrowest range covering it, the line before and the next
  one (`[N, N+1]`);
- a continuous event across several gaps (an accident and the cleanup after it,
  a demo running, a result arriving) → span the **whole event** in one keep, so
  the payoff is not chopped into jump cuts;
- never widen a keep to mark talking as important — speech is never dropped by
  default, so a keep over a talking span only restores its pauses.

The `feature`/`retain`/`emphasise` rule from 5b976f3 is kept in substance: a
plan direction asking for editorial emphasis is never a request for a keep op.
Only the "narrowest always" framing goes.

### 2. The visual-context paragraph

It currently ends every described gap at `[N, N+1]`. It gains one clause: if the
described action continues across several gaps, span the whole run rather than
emitting one narrow keep per gap. That paragraph is where the water-spill case
was decided.

### 3. `director.max_keep_lines`: 4 → 8

The wanted op is 5 lines wide, so the cap would drop it however the prompt is
worded. Line count stays the mechanism and stays a decent proxy for the abuse:
a genuine continuous event spans few lines *because nobody is talking through
it* (5 lines / 53.8s here), whereas "this talking is important" spans many (the
regression op was `keep [97,131]`, 35 lines). A cap of 8 admits the first and
still rejects the second.

The field description, the `config.example.yml` comment and
`director_llm.keep_limit_note()` are reframed from "keep is narrow" to "a keep
this wide is marking talking, not an event". `ops_from_dict()` stays uncapped, so
a hand-written wide keep in `_director.json` is still honoured.

## Testing

- `tests/test_config.py::test_director_prompt_narrows_keep_to_gap_rescue` becomes
  a two-sided pin: the single-gap `[N, N+1]` example still present, the
  continuous-event allowance present, the talking-is-not-a-keep rule present.
- `test_director_max_keep_lines_default` → 8.
- New parser test tied to the shipped default rather than a literal: the
  water-spill op `keep [128,132]` survives, the regression op `keep [97,131]` is
  still dropped.
- Each assertion is verified against a mutated implementation before the final
  green run.

## Docs to update

`AGENTS.md` (director section), `README.md`, `docs/stages/gap_context.md` — all
three state the narrow-only rule and the number 4.
