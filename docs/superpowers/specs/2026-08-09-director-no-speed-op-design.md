# Take `speed` off the director's menu

Date: 2026-08-09
Stage: `director` (prompt + one parser post-pass)

## Problem

The director may choose `speed` (factor 1.3-2.0, short span) or `timelapse` (4.0 or
more). `DIRECTOR_PROMPT` calls speed "an ACCENT, not a dial for shaving time off
speech", bounds its band, bounds its span, and names timelapse and cut as the
alternatives for anything longer.

It has been tightened twice and the middle band keeps coming back.

### Evidence — four runs of the same footage (`water_pump_3`)

Share of the finished video played at a mild 1.3-2.0x:

| run | change that preceded it | mild band |
|---|---|---|
| 07-30 | — | 57.4% |
| 08-01 11:15 | improvement 9: speed becomes a two-mode choice | **1.4%** |
| 08-01 18:03 | improvement 11: factor rules added to timelapse | 49.0% |
| 08-09 | improvement 14: number put on the speed span | 29.9% |

Two things stand out.

The one run that nearly eliminated the band is the run where `timelapse` was at
its simplest — a mode with a caption and a fast factor. Improvement 11 then gave
timelapse a target length, three worked examples, and motion and repetition to
weigh, and the band came straight back. **Making the right option harder to choose
pushed the director to the easier one.**

On the 08-09 run the two options split cleanly by source. The first source got it
right — three timelapses over 24-44 line spans, speed reduced to 2-8 line accents.
The second and third emitted **no timelapse at all**, only speed, over spans of 14
and 20 lines. Six of the fifteen speed ranges break the 30-second rule the prompt
states, one running 120.8 seconds, and one running 61.3 seconds from a span of
just two lines — "a handful of lines" and "under 30 seconds" can disagree, and the
line count won. The second source is not short of timelapse material: its parts
are 6.7, 4.1 and 6.7 minutes of hands-on trial and error. The footage was there
and the op was not used.

The viewer's verdict matches the numbers: the first source plays well, the second
and third are visibly weaker.

## Design

Remove `speed` from the operations the director can emit, and make `timelapse`
cheaper to choose again. `speed` survives as an internal marker — `timelapse`
still desugars into `<keep><speed …>` — so this is about the director's menu, not
the mechanism.

That leaves `cut`, `timelapse`, `overlay`, `keep`, `edit`, and the compression
decision becomes the two-mode one the prompt has been describing all along, with
no third option to retreat into. If a stretch drags, it is either work worth
watching fast or material worth cutting.

### 1. Parser: two paths, mirroring `max_keep_lines`

`director/director_llm.py`:

```python
VALID_TYPES = {"cut", "speed", "overlay", "keep", "edit", "timelapse"}
MENU_TYPES  = VALID_TYPES - {"speed"}   # what the director LLM may emit
```

A `_drop_off_menu_ops()` post-pass in `try_parse_director_response()` drops any op
whose type is outside `MENU_TYPES`, appending a message to `drops`. It sits beside
`_apply_keep_cap()` and follows its conventions exactly: a post-pass rather than a
check inside `_parse_op()`, so the drop message and its logging stay in one place,
and the drop surfaces as `dropped-items` in the LLM report — which is the metric
for whether the model still reaches for the op.

`ops_from_dict()` is **not** routed through the post-pass. `_director.json` is a
hand-editable intermediate; a human who writes a `speed` op into it means it, the
same reasoning that already exempts a hand-written wide `keep` from the keep cap.

`VALID_TYPES` keeps `speed`, because `guided_edit.timelapse.expand_timelapse_ops()`
still constructs `DirectorOp(type="speed", …)`. That op is built after parsing and
never re-enters the parser, so the menu restriction cannot reach it.

Nothing else changes: `guided_edit/apply.py`, `guided_edit/reconcile.py`,
`intervals/`, `blender/` and `publish/` all keep handling `speed` as they do today.

### 2. Prompt: the `speed` bullet goes, the `timelapse` bullet shrinks

- The `- speed:` bullet is deleted, with its `{"type": "speed", …}` JSON-shape
  example and its name in the cut-overlap rule's enumeration.
- The prompt does **not** state that there is no speed op. Naming an option you do
  not want chosen is how it gets chosen; a stray op is caught by the post-pass and
  degrades to 1x, which is the intended fallback anyway.
- The `- timelapse:` bullet keeps what earns its place: the 4.0 floor (matching
  `TIMELAPSE_MIN_FACTOR`), the "the words become unintelligible; that sacrifice is
  the point" price, the LISTENING/TIMELAPSE two-mode split, and the
  self-containment rule (now `do not add a separate "keep" or "overlay"`). It
  loses the three worked examples (4x/8x/16x) and the motion/repetition paragraph,
  and gains one sentence in their place: pick the factor so the result runs about
  a minute on screen — a longer span needs a bigger number, and a span where
  little is happening can go faster still.
- The prefer-timelapse-over-cut paragraph stops routing repeated speech through
  "speed is not the tool" — an op that no longer exists — and routes it to 1x or
  cutting the weakest passes directly.
- The Timing paragraph is unchanged: "a long speech duration is a candidate for
  cutting, not speeding up — a long stretch of manual work is a timelapse
  candidate instead" already says the right thing.

#### Accepted trade-off

The three worked examples were added by improvement 11 precisely because a single
anchor number becomes the director's uniform output (keep width stuck at
`[N, N+1]`, overlay durations clustering on the example value, the bare speed
factor clustering on 4.0). Removing them risks factors clustering at the 4.0
floor.

That is a far cheaper failure than the mild band: 4.0 is a real timelapse, the
floor is parser-enforced, and the run where the band nearly vanished is the run
where this bullet was at its simplest. If factors do flatten at 4.0 in the next
run, the fix is a separate, later change — deriving the factor in code from the
span's real length against a target on-screen runtime, which is arithmetic the
pipeline can do exactly and the LLM cannot be trusted to. That option was
considered here and deliberately not taken: this change stays prompt-shaped, like
the three before it, so the next run measures one variable.

### 3. Tests

**Delete** (the prompt text they pin is gone):

- `test_director_prompt_limits_a_bare_speed_op_to_a_mild_accent`
- `test_director_prompt_speed_span_has_a_duration_limit`
- `test_director_prompt_speed_span_limit_names_the_alternative`
- `test_director_prompt_speed_example_is_a_mild_accent`
- `test_director_prompt_documents_speed_does_not_keep_silence`
- `test_director_prompt_timelapse_worked_examples_are_arithmetically_correct`
- `test_director_prompt_timelapse_weighs_visible_motion_and_repetition`
- the `_speed_bullet()` helper

**Rewrite:**

- `test_director_prompt_timelapse_ties_factor_to_target_runtime` — assert the
  surviving target-runtime sentence, no worked examples.
- `test_director_prompt_timelapse_is_self_contained` — new string.
- `test_director_prompt_does_not_offer_speed_as_a_way_to_tighten_speech` — assert
  repeated speech routes to 1x / cutting the weakest passes, without the removed
  "speed is not the tool" phrase.
- `test_a_bare_speed_op_is_untouched_by_the_floor` — moves to the `ops_from_dict`
  path, which is where a bare speed op now legitimately arrives.
- Any director test whose fixture response carries a `speed` op alongside the op
  under test (the keep-cap tests) — the speed op is now dropped, so their
  assertions move onto the op they are actually about.

**Add:**

- The prompt offers no speed op: no `- speed:` bullet, no `"type": "speed"` in the
  JSON shape.
- Every `"type"` appearing in the prompt's JSON shape parses through the **LLM**
  path (`try_parse_director_response`) and survives — so a future example for an
  off-menu type fails loudly instead of teaching the director a dropped op.
- An LLM-emitted `speed` op is dropped, with a message naming the type.
- A hand-edited `_director.json` carrying a `speed` op keeps it
  (`ops_from_dict`).

### 4. Documentation

- `AGENTS.md` — the director stage paragraph (op list, the speed/timelapse
  register discussion) and the guided_edit paragraph's op enumeration.
- `README.md:33` — the `cut/speed/overlay/keep/edit/timelapse` list.
- `config.example.yml` — regenerated via `make config-example`.
- This spec; the improvement-roadmap memory entry.

`README.md`'s `<speed factor="N.N">` sections and `docs/stages/intervals.md` /
`blender.md` are untouched: the human-facing marker and its Blender behaviour are
unchanged.

## Verification

- `make check`.
- Every new test mutation-verified: break the implementation (leave `speed` in
  `MENU_TYPES`, route `ops_from_dict` through the post-pass, restore a prompt
  fragment) and confirm the test fails before reverting.
- Next real run: mild-band share of the finished video, timelapse count per
  source, and the `dropped-items` count in `output/llm_report/index.md` (a
  non-zero speed-drop count means the model is still reaching for the op and the
  prompt, not the parser, needs the next look).
