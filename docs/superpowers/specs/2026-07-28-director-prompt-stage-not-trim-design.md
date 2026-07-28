# Director prompt: tighten AND stage (not just trim)

## Problem

`DIRECTOR_PROMPT` (`src/nagare_clip/config.py`) frames the director's job purely as
removal ("Decide high-level edits to tighten the video"). Two consequences, evidenced
by a real run (`water_pump_3`, 90.8min source):

- **`overlay` has no guidance on when/how often to use it.** Op mix on the 2026-07-16
  run: 15 cut, 8 speed, 4 overlay, 3 keep — four overlays across 25.8 minutes of
  finished video for a brief asking for a fast, punchy vlog. A `cut [60,77]` in the
  same run deleted repeated failed priming attempts wholesale ("78〜で新方法に切り替わるため不要"),
  even though the payoff at line 96 only lands because of those failures — that
  wanted `speed`, not `cut`.
- **A long gap is defined as dead air with no exception.** The strongest moment in the
  project — water flooding the floor, then cleanup — sits in a 48.8s gap between two
  lines of speech that announce it:

  ```
  131: ...これはこぼれないこれやばい水浸し  [10.2s speech, 11.5s silence, gap 48.8s]
  132: 一旦掃除します  [1.8s speech, 0.8s silence]
  ```

  The director dropped it. This is a regression with a date: on 2026-07-16 (before the
  silence-aware bracket landed) the director emitted `keep` over this span; after the
  bracket became more accurate (2026-07-28), the old signal that accidentally made the
  span look interesting (an inflated duration) disappeared, and no real rule ever
  replaced it. The gap length reaches the director regardless of `gap_context` (this
  gap has no `[silent gap ...]` annotation in the evidence — no upstream change
  needed, the judgment belongs in the prompt paragraph that already renders plain
  timing brackets).

## Goal

Rewrite `DIRECTOR_PROMPT` so the stated goal is "tighten **and** stage": still cut
what drags, but also mark the moments that make the video worth watching, and give
the director a real rule for reading a long gap instead of "dead air, no exceptions."

## Non-goals

- No change to the op schema, `director_llm.py`, `context.py`, or `run.py` — this is
  prompt text only.
- No new config field for overlay density. The spec text explicitly asks for the
  density guidance to read as an adjustable soft target, not a hardcoded constant, so
  a later editorial-brief-injection feature (tracked separately, not part of this
  work) can append stronger/weaker wording after it without a code change here.
- No change to README.md. Nothing in the observable contract (op types, JSON shape,
  application mechanics) changes — only internal LLM judgment guidance.

## Design

Four edits to `DIRECTOR_PROMPT`, all prose:

1. **Opening framing.** "Decide high-level edits to tighten the video." becomes
   "Decide high-level edits to tighten AND STAGE the video: cut what drags, but also
   mark the moments that make it worth watching — turning points, payoffs, failures,
   mishaps."

2. **Timing paragraph's gap sentence.** Currently: `long gaps are dead air (already
   dropped by default unless you "keep" them)`. Becomes a fallback-not-a-rule: a long
   gap is dead air by default, but check the speech immediately before/after it — if
   it announces something happening (an accident, a cleanup, a wait for a result), the
   gap itself may be the most watchable moment in the shot, and a `"keep"` spanning
   that line and the next (`[N, N+1]`) preserves it. This mirrors the existing
   "Visual context" paragraph's `keep [N, N+1]` pattern for gap_context-described
   gaps, applied here to the plain timing bracket that every line carries regardless
   of gap_context.

3. **New preamble before the Operations bullet list.** Prefer `speed` over `cut` for
   repetition that builds toward a payoff (failed attempts, retries, warm-up) — the
   buildup is part of the story, so tighten it rather than deleting it. Reserve `cut`
   for spans that leave the throughline entirely (digressions, dead ends, redundant
   retakes with no payoff).

4. **Overlay bullet.** Append: reach for it at turning points, conclusions, failures,
   and mishaps — moments worth labeling on screen. Aim for roughly one overlay per 3-5
   minutes of finished video as a loose default target; if an editorial brief states
   otherwise, follow the brief instead.

### Exact new prompt text

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

### Compatibility with existing pinned tests

Verified against `tests/test_config.py`:

- `test_director_prompt_documents_speed_does_not_keep_silence` — the `- speed:` bullet
  line is untouched; still contains `keep` and `silences/pauses`.
- `test_prompt_documents_duration_and_gap_bracket` (parametrized `director`/`plan`) —
  all three `format_dur_gap(...)` example strings, plus `"duration"`, `"gap"`,
  `"negligible"`, are still present verbatim in the Timing paragraph.
- `test_director_prompt_documents_gap_annotations` /
  `test_director_prompt_gap_example_matches_the_real_formatter` — the Visual context
  paragraph and its exact annotation-line example are untouched.
- `test_director_prompt_overlay_example_carries_a_duration` — the JSON-shape overlay
  example line is untouched and still parses with a positive duration.

## New tests (TDD — written first, confirmed to fail against the current prompt)

Added to `tests/test_config.py`, alongside the existing pinned-prompt tests, one per
edit above:

1. `test_director_prompt_frames_goal_as_tighten_and_stage` — the opening sentence
   (case-insensitive) contains both `tighten` and `stage`.
2. `test_director_prompt_treats_long_gap_as_keep_candidate_when_speech_announces_event`
   — prompt (lowercased) contains `fallback`, `accident`, and `watchable moment`.
3. `test_director_prompt_prefers_speed_for_buildup_reserves_cut_for_digressions` —
   prompt contains `payoff`, `throughline`, and `buildup`.
4. `test_director_prompt_documents_overlay_density_target` — prompt contains
   `3-5 minutes` and `editorial brief`.

Each assertion is written to fail against the current (pre-edit) `DIRECTOR_PROMPT` and
pass once the four edits land — confirmed by running the new tests before touching
`config.py`.

## Docs to update

- `make config-example` — regenerate `config.example.yml` from the model (mechanical;
  `test_example_file_matches_generator` guards drift).
- `AGENTS.md` — one sentence added to the `director` stage section noting the prompt
  frames the goal as tighten-and-stage, with speed-over-cut-for-buildup and
  long-gap-as-keep-candidate guidance.
- `plan.md` — one new dated changelog entry (2026-07-28) summarizing the change,
  evidence, and the four prompt edits, following the existing entry style (e.g.
  "Overlay duration stated by the director").
- `README.md` — **not** touched (see Non-goals).

## Verification

`make check` (lint + format + validate + test) after the edits; manually diff-read the
new `config.example.yml` section for `director.prompt` to confirm it matches the new
text.
