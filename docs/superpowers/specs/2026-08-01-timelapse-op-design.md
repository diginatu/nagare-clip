# A timelapse is one op, not three that must agree

Date: 2026-08-01
Stages: `director` (op vocabulary + prompt), `guided_edit` (desugaring)

## Problem

A timelapse is assembled by hand from three separate ops that must all be emitted
with the same line range:

```json
{"type": "speed",   "lines": [13, 44], "factor": 4.0}
{"type": "keep",    "lines": [13, 44]}
{"type": "overlay", "lines": [13, 13], "text": "パイプ・ホース取り付け作業", "duration": 2.0}
```

`DIRECTOR_PROMPT` explains the arrangement in prose: pair a timelapse with a `keep`
over the same lines so the work runs continuously, "and usually with an overlay
saying what is happening".

Nothing enforces the agreement. If the `keep` misses the `speed` range by a line,
the uncovered part becomes speed without keep — its internal silences are dropped,
so it plays back as sped-up jump cuts. That is the exact failure the two-mode rule
was written to eliminate, and it would reappear silently: the op list still looks
correct, and nothing in the log flags it.

### Evidence — 2026-08-01 run, `water_pump_3`

The arrangement worked. All five timelapses have `speed` and `keep` on identical
ranges (`[13,44]`, `[52,96]`, `[145,198]`, `[5,20]`, `[33,51]`), each with an
overlay at its first line. Strip count fell from 501 to 157 because the work now
runs as continuous footage instead of a chain of jump cuts.

But the invariant held by discipline alone, and one detail shows the seam. The
overlay is anchored to a single line and carries a reading-length duration, so it
labels a fraction of what it introduces:

| timelapse | on-screen length | overlay shown |
|---|---|---|
| パイプ・ホース取り付け作業 | 3.21 min | 2.0s |
| 呼び水の方法を試行錯誤… | 3.83 min | 3.0s |
| 再調整・再挑戦するも安定せず | 5.01 min | 4.0s |

Five minutes of near-silent footage where the viewer is told what they are
watching for the first four seconds.

## Design

A single `timelapse` op carries the range, the factor, and the caption:

```json
{"type": "timelapse", "lines": [13, 44], "factor": 8.0, "text": "パイプ・ホース取り付け作業"}
```

`guided_edit` desugars it into the markers that already exist, so `intervals` and
`blender` are untouched and `_edits.txt` stays hand-editable in today's syntax.
The primitives remain available for everything that is not a timelapse.

To change the caption partway through, the director emits consecutive `timelapse`
ops. That constraint is worth having: a new caption means a new phase of work,
which is a natural place to split.

### 1. Op contract (`director/director_llm.py`)

`VALID_TYPES` gains `timelapse`. `_parse_op` validates it as:

| field | rule |
|---|---|
| `lines` | as every op — 1-based, inclusive, within range |
| `factor` | **required**, must be at least `TIMELAPSE_MIN_FACTOR` (4.0) |
| `text` | **optional** string; CR/CRLF normalised to `\n` as for `overlay`; blank counts as absent |
| `duration` | not read — the caption's on-screen time is derived, never stated |

A factor below the floor **drops the op** (logged, and recorded in the LLM
report's drop list). Below 4.0 it is not a timelapse but a mild speed-up, and a
`timelapse` op's derived `keep` is uncapped by construction — accepting a low
factor would smuggle an uncapped keep over talking, which is precisely the
runtime-inflation hole `max_keep_lines` closed. Dropping degrades to the default
(speech kept, internal silence cut), the safe direction.

An op with no `text` still applies: it becomes speed+keep with no caption.

The floor lives in `_parse_op`, so it holds on both paths — the LLM response and a
hand-edited `{stem}_director.json`. Unlike `max_keep_lines`, which is a policy cap
the LLM alone is subject to, the floor is part of what the word `timelapse` means:
a 2.0x span is a `speed` op, and writing one by hand is as available as it ever
was.

`ops_to_dict` needs no change — it already emits `factor`/`text`/`note` when set —
so a `timelapse` op round-trips through the hand-editable `{stem}_director.json`.

### 2. Desugaring (`guided_edit/timelapse.py`, new)

A pure `expand_timelapse_ops(ops, seg_times) -> list[DirectorOp]`, called from
`run_guided_edit` before `apply_ops`. That function already reads the WhisperX
JSON for its closing `check_edits` pass; the read moves ahead of `apply_ops` so
`timing.segment_times` can use it.

Each `timelapse [a,b] factor F text T` expands into three ordinary ops, in this
order:

```
overlay [a,a] duration=D text=T
speed   [a,b] factor=F
keep    [a,b]
```

`apply_span_op` prepends each open tag to line `a`, so that order produces the
documented nesting:

```
line a:  <keep><speed factor="F"><overlay text="T" duration="D"/>…
line b:  …</speed></keep>
```

All three inherit the original op's `note`, so the per-op LLM report stays
readable. None of the three needs an LLM call — span ops and point ops are already
applied deterministically.

**Caption duration.** `D = round((end_b − start_a) / F, 2)`, from the segment
times. This is exact, not an estimate: the derived `<keep>` preserves the whole
span, so nothing inside is cut and the edited-timeline length *is* the quotient.

**No minimum clamp.** Every overlay is placed on Blender's single
`OVERLAY_CHANNEL` (5). A floor that let a short timelapse's caption outlive its
own span would collide on that channel with the next consecutive timelapse's
caption — exactly the arrangement this design asks for when the caption changes
partway through. A short timelapse gets a proportionally short caption.

**Degradation.** When segment times are unavailable (no `json_path`, or a
boundary segment missing `start`/`end`), the speed and keep ops are still emitted
and the caption is dropped with a warning: the continuity fix is the valuable
half. `apply_ops` also grows a guard so an unexpanded `timelapse` op is recorded
as unapplied rather than raising out of `_span_tags`.

### 3. Removing the three-op pairing

The pairing currently has dedicated support, which comes out:

- `_apply_keep_cap` loses `_timelapse_covers` and its containment exemption,
  reverting to the plain width cap. A hand-written wide `speed`+`keep` pair is
  capped again, as it was before that exemption landed.
- `keep_limit_note` loses its exception clause.
- `TIMELAPSE_MIN_FACTOR` survives as the `timelapse` op's factor floor.
- In `DIRECTOR_PROMPT`, the `speed` bullet loses the pairing paragraph — keeping
  the two-mode framing and the "1.3–2.0 is the exception" line — and a
  `timelapse` bullet replaces it. The JSON-shape block gains a `timelapse` entry.

The prompt gets shorter as a result: one op with three fields replaces a paragraph
explaining how to coordinate three ops.

### 4. Tests

New:

- Parse level: factor floor (at, above, below), missing factor, optional/blank
  `text`, ignored `duration`, `ops_from_dict` parity, `_director.json` round-trip.
- Expansion: emitted op order and types; the resulting tag nesting on both
  boundary lines; duration arithmetic; two consecutive timelapses producing
  abutting, non-overlapping captions; missing-timing degradation; a non-timelapse
  op list passing through unchanged.
- `run_guided_edit` end-to-end: the written `_edits.txt` passes `check_edits`.
- Prompt: the prompt's own `timelapse` example parses through
  `parse_director_response`, so a stale example fails loudly.

Deleted: `TestTimelapseKeepExemption` (8 tests) and
`test_note_states_the_timelapse_exemption` — both assert the exemption being
removed.

Rewritten: `test_director_prompt_timelapse_states_its_price_and_its_partners`,
which asserts the `speed` bullet names `keep` and `overlay` as partners. That
prose is what goes away; the replacement asserts the `timelapse` bullet states
its price (unintelligible audio, factor 4.0 floor) and that it needs no partners.

Per the repo's TDD rule, each new test is verified to fail against a mutated
implementation before the real one is written.

## Documentation

- `AGENTS.md` — director op vocabulary, guided_edit desugaring, and the
  `max_keep_lines` exemption paragraph.
- `README.md` — the director op list and the `max_keep_lines` paragraph.
- `config.example.yml` — regenerated (`make config-example`) for the new prompt.

No `docs/stages/` file covers `director`/`guided_edit`, and `intervals`/`blender`
behaviour is unchanged, so no stage deep-dive needs an edit.

## Out of scope

- Changing the `<overlay/>` marker contract or the `intervals`/`blender` overlay
  path. The desugaring keeps both untouched.
- A caption that changes partway through a single timelapse op. Consecutive
  `timelapse` ops express this, deliberately.
