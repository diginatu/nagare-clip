# Speed is a choice between listening and timelapse, not a dial

Date: 2026-08-01
Stage: `director` (prompt only)

## Problem

`DIRECTOR_PROMPT` describes `speed` as a general-purpose compressor —

> `- speed: play a span faster; give "factor" (e.g. 2.0).`

— and the paragraph above the op list tells the director to *prefer* it over `cut`
for "repetition that builds toward a payoff". Nothing says what a factor is *for*,
so the model picks modest values and applies them broadly. The finished video then
spends most of its running time slightly fast.

### Evidence — 2026-07-30 run, `water_pump_3`

25.6 minutes finished:

| playback rate | share of finished video |
|---|---|
| 1.0x | 42.6% |
| 1.3x | 1.4% |
| 1.5x | 23.0% |
| 1.8x | 3.4% |
| 2.0x | 29.6% |

57% of the video is sped up and no span exceeds 2.0x; across 20 speed ranges the
factors only ever land between 1.3 and 2.0. The spans are long (one covers 1017
seconds of source, another 692) and 85% of the footage inside them carries
captions — it is speech, not silent work, because a `speed` op's internal silences
are dropped before the speed applies.

Audio pitch is corrected, so nothing sounds broken. The failure is editorial: a
sustained mild fast-forward over the majority of a video is a register long-form
YouTube does not use. It reads as neither "listen to this" nor "watch this go by",
and it is the video's dominant mode rather than an accent.

## Design

The discrimination lives in the prompt, where intent lives. No parser, config
field, or enforcement path changes — `parse_director_response()` already accepts
any positive factor and continues to.

### 1. The `speed` bullet becomes a mode selector

Replace the current one-line bullet with a two-mode rule (still **one prompt
line**, like every other bullet — the existing
`test_director_prompt_documents_speed_does_not_keep_silence` reads the single line
starting `- speed:`, and the format stays uniform):

- **Speed is not a dial** for shaving time off speech; it is a choice between two
  modes, and one must be picked.
- **Listening** — the speech carries something the viewer needs: no speed op, play
  it at 1x. If it drags, `cut` the weakest parts instead.
- **Timelapse** — the span is manual work whose speech is inessential: go
  genuinely fast (factor 4.0 or more) and accept that the words become
  unintelligible. That sacrifice is stated explicitly in the prompt, because it is
  what forces the choice to be made honestly instead of split down the middle.
  Pair it with a `keep` over the same lines so the work runs continuously rather
  than becoming sped-up jump cuts (internal silences are otherwise dropped), and
  usually with an `overlay` saying what is happening, since the narration no
  longer does that job.
- Factors between 1.3 and 2.0 are the **exception, not the default**: most of the
  video plays at 1x and speed is an accent, not the register the video runs in.

The "accent, not the dominant mode" limit is stated qualitatively. No numeric
share ceiling is given: the evidence pins 57% as clearly wrong but does not pin
where the line actually sits, and an invented threshold would be a number the
model optimises against rather than a judgement it makes.

### 2. The prefer-speed-over-cut paragraph is routed by what is on screen

The paragraph introducing the op list currently licenses mild speed over talking.
It gains the same distinction:

- repetition that **is visible work** (failed attempts, assembly, waiting for a
  result) → timelapse it rather than deleting it — the buildup is part of the
  story;
- repetition that **is speech** → speed is not the tool: leave it at 1x, or cut
  the weakest passes;
- `cut` stays reserved for spans that leave the throughline entirely
  (digressions, dead ends, redundant retakes with no payoff).

The tighten-rather-than-delete instinct from
[2026-07-28](2026-07-28-director-prompt-stage-not-trim-design.md) survives intact,
including its `payoff` / `buildup` / `throughline` vocabulary. Only the reading
that produced 85%-captioned speed spans is removed.

### 3. Out of scope

`PLAN_PROMPT` still offers "speed up" as a coarse direction and still calls long
parts candidates "for shortening or speeding up". A plan direction is editorial
intent with no mechanical effect; the director now owns how speed is spent. The
[2026-07-29](2026-07-29-keep-semantics-disambiguation-design.md) precedent rewrote
plan vocabulary only because `keep` named a downstream *op*, which "speed up" does
not.

## Testing

New pins in `tests/test_config.py`:

- the speed bullet names both modes, states the fast floor and the loss of
  intelligible audio, and pairs a timelapse with `keep` and `overlay`;
- the speed bullet marks 1.3–2.0 as the exception rather than the default;
- the prefer-speed paragraph routes repeated *speech* to 1x/cut and repeated
  *visible work* to speed.

Updated in place (not deleted):

- `test_director_prompt_documents_speed_does_not_keep_silence` — must still pass
  unchanged, which is what keeps the bullet on one line;
- `test_director_prompt_prefers_speed_for_buildup_reserves_cut_for_digressions` —
  reframed to the visible-work routing while still asserting the `payoff` /
  `buildup` / `throughline` vocabulary.

Because the implementation (a prompt string) exists before the tests can be
written against it, every new assertion is verified against a **mutated** prompt —
delete the clause it guards, watch the test fail, restore — and that evidence is
reported alongside the green run.

## Docs to update

- `config.example.yml` — no change expected: the generator elides prompt bodies
  (`# prompt: "..."   # System prompt (has a sensible default)`), so a prompt edit
  does not move the file. `make config-example` is still run once to confirm
  `tests/test_config.py::test_example_file_matches_generator` stays green.
- `AGENTS.md` — the director section's closing sentence states the prefer-speed
  rule in its old form.
- `README.md` — the `<speed>` passages describe marker *mechanics*, which do not
  change; no edit expected.

There is no `docs/stages/director.md`; the director's deep-dive text lives in
`AGENTS.md`. `plan.md` named by the documentation policy does not exist in the
repo, so nothing to update there.
