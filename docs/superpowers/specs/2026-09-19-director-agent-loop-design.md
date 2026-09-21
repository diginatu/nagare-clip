# Director: one conversation over the whole video, with silence as lines

Status: approved in conversation 2026-09-20. Supersedes the 2026-09-19 draft of
this file (a tool-calling loop). Rollback point before any of this: `e1006f2`
(whole-video context), `639f0ab` (playback preview).

## Problem

The director writes edit ops over line ranges and never sees them played back.
Across three real runs on the same footage:

1. Timelapses opened on the line announcing the work (fixed by prompt + effort
   `high` + "plan ranges are section boundaries").
2. **Range-end misunderstanding, every run.** A keep/timelapse span ends at the
   last word of its last line (`intervals/sync_json.py:392-412`), so the gap
   after it is dropped. Notes like "compress the 29.9 s wait after line 53" on
   `timelapse [53,53]`, which plays 0.9 s of speech in 0.2 s. It is also
   *unexpressible*: `[53,54]` destroys line 54's explanation, and nothing
   addresses the wait alone.
3. Meaningful speech swallowed into a timelapse with no visible cost
   (`[6,15] x12`, line 9, 13.7 s).
4. Neighbouring ops always drop the gap between them (the 37.0 s of fitting
   work between `[10,45]` and `[46,85]` in PXL_20260328_082352713).

Also structural: segments range from 0.6 to 8.0 minutes, so one call per
segment gives the model wildly different amounts to hold at once.

## Design

### 1. Silence is a line

A gap of **5 s or more between two speech lines** is its own line in the
director's transcript, in square brackets (angle brackets are the edit
markers in `_edits.txt`):

```
53: その状態で  [0.9s]
54: [silent 29.9s: a hand enters from the right holding a clear tube over the tank]
55: この状態で今予備水持ってきたんで…  [16.4s speech, 3.1s silence]
```

- Whether a silence plays is whether its line is inside an op. `timelapse [54]`
  fast-forwards the wait alone; `[53,55]` includes it with both sides.
- The silence is the no-speech gap between words, i.e. exactly what the
  intervals stage drops by default (`intervals/run.py:83-123`), not an
  audio_silence cut range (one 29.9 s wait is six cut ranges).
- 5 s matches `gap_context.min_gap`: 58 of the 59 visible-action descriptions
  fall inside a silence line, and become its text (several are joined).
- **Silence inside a line** (49 more at 5 s, mostly WhisperX stretching one
  character across a pause; ~40 % cannot be split at a defensible word) stays
  in the bracket as `Ys silence` for now.
- Made redundant for gaps ≥ 5 s: the bracket's `gap` part, the
  `[silent gap: …]` annotation lines and their anchoring, the prompt's
  `[N, N+1]` rescue rule and visual-context legend, and the preview's "gap
  outside this op" fact. Removed as part of this work.

### 2. One conversation, one numbering

- The whole video in playback order with **one global line numbering**
  (speech and silence lines together). It is a view: every display line maps
  back to (segment, source, source line) or (…, "silence after source line n").
- Plain multi-turn JSON — no tool calling, so no thinking-block replay, no
  `tool_choice` constraints, no clash with `response_format: json`.
- Each turn the code asks for **an approximate range** — "around lines 41 to
  80" — never a hard boundary (hard boundaries are how plan ranges got copied
  into op boundaries). The model replies with the ops for the range it
  actually reviewed and the line it reviewed through; the next request starts
  after that line.
- The model may rewrite any earlier range at any time. A reply's ops
  **replace everything inside the range it names**.
- The model ends with `done` once every line has been reviewed.
- A range crossing a segment join: `cut` and `keep` are split at the join
  automatically; `timelapse` and `overlay` are refused in the preview with the
  join's line number, for the model to split.

### 3. The playback preview answers every turn

`director/preview.py` (committed), extended to global numbering and silence
lines: per op, footage seconds → on-screen seconds, caption seconds, speech
made unintelligible (2x or faster) with what the span shows alongside, caption
overlaps, parser drops, and the running runtime of the whole video. Facts,
no thresholds except the 2x rule.

### 4. Limits and failure

- Turn cap ≈ `ceil(display lines / x) × 2`, x ≈ 40 (`director.chunk_lines`).
  On the current project: ~456 display lines → 12 chunks → 24 turns.
- **Reaching the cap is an error** — the stage fails, uniformly, so it never
  continues silently. **The ops accepted so far are still written**, so the
  user can inspect them and resume from the next stage by hand. The error
  names the last reviewed line.
- A turn whose reply cannot be parsed goes through the existing retry ladder;
  exhausting it is also an error, with the same write-what-you-have rule.
- `--source` no longer narrows the director (accepted 2026-09-20): one
  conversation owns the whole video and rewrites every `_director.json`.

### 5. Output contract

`_director.json` stays per source in source coordinates, so hand-editing and
everything from `guided_edit` on keep working. `lines` gains one form:
`"n~"`, the silence after source line n, e.g. `{"type": "timelapse",
"lines": ["53~", "53~"], "factor": 5}`. Old files parse unchanged, and a
`"n~"` reference survives a change of the 5 s threshold.

### 6. Downstream: ops become time ranges in code

guided_edit places `cut`/`keep`/`speed` deterministically already (only `edit`
ops use its LLM, `guided_edit/apply.py:479-542`), but no marker position in
`_edits.txt` can address a gap. So line-granular keep/speed/overlay resolve to
**time ranges in code** — a speech line to its words' span, `"n~"` to the gap
before line n+1 — and `run_intervals` takes them as extra lists (proven: adding
keep + 5x over 501.897–531.779 s of PXL_20260426_090431216 plays the wait at
5x and line 54 at 1x, nothing else changes; Blender already splits at speed
boundaries, `blender/frames.py:55`). Kept in guided_edit: LLM-placed `edit`
ops and whole-line `<cut>` (keeps grow back over caption words,
`intervals/run.py:184-188`). The cut-is-clipped-around-kept-lines rule stays.

## Increments

1. **`"n~"` end to end.** `_coerce_lines` accepts it; a resolver turns keep /
   timelapse / overlay ops into time ranges; `run_intervals` consumes them.
   Evidence: hand-edit PXL_20260426_090431216's `[53,53]` to `["53~","53~"]`
   x5, run guided_edit → blender for that source (its `_edits.txt` is stale
   anyway), and watch the wait play at 5x with line 54 at 1x.
2. **Silence lines in the director's view.** Render them, retire the
   redundant gap machinery above, extend the preview. Evidence: prompt dump,
   no LLM.
3. **The conversation.** Global numbering, approximate ranges, rewrite-any-
   range, `done`, cap-as-error-with-output, the preview every turn; the
   per-segment path is deleted. Evidence: one director run compared with the
   three earlier runs on range-end claims, unintelligible speech seconds,
   gaps dropped between neighbouring ops, timelapse count and runtime share,
   turns and rewrites used.

## Testing

TDD with mutation evidence throughout. LLM stages use scripted fake
`call_llm`s. Key cases: `"n~"` parsing and resolution (incl. the raw vs
capped word-end mismatch found on 5 of 49 real gaps); join-crossing splits and
refusals; a reply replacing only its named range; `done` refused while lines
remain unreviewed; the cap raising while the files are still written; the
cached prefix byte-identical across turns.
