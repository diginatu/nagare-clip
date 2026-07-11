# sentence_split: force split at audio silences

**Date:** 2026-07-11
**Stage:** `sentence_split` (consumes `audio_silence` output)
**Status:** approved, pending implementation

## Problem

`sentence_split` re-segments the WhisperX transcript with an LLM, but nothing
ties its sentence boundaries to the audio. A long silence — a take boundary, a
topic change, a pause while the speaker repositions — is a strong signal that
two utterances are separate, yet the LLM can freely group words from both
sides of it into one output sentence. Downstream, that sentence becomes one
`_edits.txt` line spanning the silence, which hurts director timing context
and makes human editing awkward.

The `audio_silence` stage already detects these silences (ffmpeg
`silencedetect`) and writes them to a human-editable `{stem}_cuts.txt`, and it
runs *before* `sentence_split` in the pipeline — the data is available; it is
just not used.

## Decision summary

- **Trigger: cuts file + longer threshold.** Read the (possibly human-edited)
  `output/audio_silence/{stem}_cuts.txt` via the existing `read_cuts()`, but
  only spans **≥ `sentence_split.force_split_min_silence`** (default **3.0 s**)
  force a split. `audio_silence.min_silence` is tuned for jump-cuts (short
  pauses); forcing a sentence break at every jump-cut would over-split.
  Deleting a cut line (to keep that span's audio) also stops it forcing a
  split — one consistent semantics for the human-edited file.
- **Mechanism: deterministic post-enforcement, zero extra LLM calls.**
  Windowing, LLM calls, and carry-over run exactly as today. Every emitted
  segment — LLM-rebuilt *and* degrade-fallback — is deterministically split at
  qualifying silences before emission. The LLM call count and window contents
  are byte-identical to today. (The block pre-split alternative was rejected:
  it adds up to one extra LLM call per qualifying silence. A later improvement
  may add silence context to the LLM prompt so the model itself prefers to
  split there; the post-enforcement guarantee stays either way.)
- **Scope: only when the LLM stage is enabled.** `sentence_split.enabled:
  false` keeps its documented byte-identical copy-through contract. No
  standalone silence-only splitting mode.
- **Defaults: on.** `force_split: bool = true`,
  `force_split_min_silence: float = 3.0`. Setting `force_split: false`
  restores current behavior exactly.

## Config (`config.py`, `SentenceSplitConfig`)

Two new fields:

```yaml
sentence_split:
  force_split: true              # split segments at long detected silences
  force_split_min_silence: 3.0   # seconds; only cut spans at least this long
```

Regenerate `config.example.yml` with `make config-example` (the
`test_example_file_matches_generator` sync test enforces this).

## Data flow

- `run_sentence_split()` gains a keyword arg `cuts_txt: Path | None = None`.
- `_sentence_split_run` (`pipeline/stages.py`) passes
  `cuts_txt=ctx.stage_dir("audio_silence") / f"{stem}_cuts.txt"`.
- The stage reads it with `audio_silence.cuts_file.read_cuts()` — a missing
  file or empty list degrades to "no forced boundaries", so
  audio_silence-disabled runs (header-only cuts file) are unaffected.
- Qualifying silences (`end - start >= force_split_min_silence`) are filtered
  once in `run_sentence_split()` and threaded into `resegment_json()` as a
  plain `list[tuple[float, float]]` (empty when `force_split` is false, the
  file is absent, or nothing qualifies).

## Core change

One new pure function in `segment.py`:

```
split_segment_at_silences(seg, silences) -> list[segment]
```

- For each qualifying silence, the split point is **before the first word
  whose `start` ≥ the silence's midpoint** (time-based — no global word-index
  bookkeeping; robust to WhisperX alignment jitter at silence edges). Words
  lacking `start` inherit the last known time (never split between a timed
  word and a following untimed one).
- **Revision (2026-07-11, after real-project testing):** the split only fires
  when the previous word's effective `end` is also ≤ the midpoint — a genuine
  inter-word gap must corroborate the silence. WhisperX frequently stretches
  one word across an entire pause (e.g. 'は' timed 2.06–7.40 over a 2.23–6.50
  silence, its end abutting the next word); the midpoint then falls *inside*
  that word and the original rule split one word too late, chopping the
  sentence's first character(s) off (116 of 199 long silences in the test
  video). Which sentence a stretched word belongs to is undecidable from
  timing alone — sometimes the previous ('だけ**ど** | こんな'), sometimes the
  next ('**い**きます') — so ambiguous silences are skipped (false negative
  over wrong split). Pure word-gap detection without the cuts file was
  evaluated and rejected: the same stretching hides the gaps, leaving only 3
  of 199 pauses detectable.
- Pieces are rebuilt with the existing `segment_from_words()`; empty slices
  (split point at position 0 or `len(words)`) are dropped, so a silence
  outside the segment's word range is a no-op.
- Words are only reassigned, never edited — the global `concat_word_text`
  verbatim invariant is preserved by construction and stays as the final
  guard, unchanged.

`resegment_json()` gains a `silences` parameter and applies
`split_segment_at_silences` at **every emission point**:

1. rebuilt sentences from a successful LLM window,
2. the degrade path (carried-in flush + original window segments),
3. the empty-text passthrough (`new_segments.extend(win)`).

The forced split therefore survives total LLM failure.

**Carry-over safety falls out automatically:** the split pass runs on the
rebuilt window *before* the carry decision, so no candidate segment spans a
qualifying silence; the held-back trailing sentence is always entirely on one
side of the window's last silence and carry can never drag words across it.

## Trade-off accepted

The LLM may group a sentence across a qualifying silence and have its output
chopped at the boundary, yielding two fragments rather than two well-formed
sentences. With a 3.0 s threshold this is rare (3 s of mid-sentence silence
almost always separates two utterances), and the split lands at the same word
position the block alternative would have chosen. Future work can add silence
markers to the prompt to make the LLM prefer those boundaries itself.

## Testing (TDD — red first)

- `split_segment_at_silences` (pure): midpoint snapping; sub-threshold spans
  filtered upstream (function receives only qualifying silences); silence
  outside the segment is a no-op; multiple silences in one segment; untimed
  words inherit the last known time; word objects preserved by identity;
  timing (`start`/`end`) of pieces derived from their words.
- `resegment_json` with a stub LLM: no output segment spans a qualifying
  silence; degrade-fallback segments are also split; carry never crosses a
  silence; `silences=[]` output is byte-identical to today (regression
  guard).
- `run_sentence_split`: `force_split: false` → identical to today; missing
  `cuts_txt` / header-only cuts file → identical to today; disabled stage →
  copy-through unchanged; qualifying cut span → output `.json`/`.txt` split
  there.
- `pipeline/stages.py`: adapter passes the audio_silence cuts path.

Per repo TDD policy, each test must be shown to fail against a broken
implementation (mutation-catch evidence) before being trusted.

## Documentation

- `AGENTS.md` — sentence_split stage section (new inputs + behavior).
- `README.md` — user-facing config description.
- `plan.md` — implementation/status.
- `config.example.yml` — regenerated.
