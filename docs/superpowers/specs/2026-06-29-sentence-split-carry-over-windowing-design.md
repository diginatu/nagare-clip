# sentence_split: carry-over windowing design

**Date:** 2026-06-29
**Stage:** `sentence_split`
**Status:** approved, pending implementation

## Problem

`sentence_split` re-segments a WhisperX transcript into one-sentence-per-line
units by processing it in fixed windows of `window_segments` WhisperX segments
(default 20), one LLM call per window.

The windows are cut at arbitrary WhisperX-segment boundaries with no regard for
sentence structure:

- `iter_windows()` (`segment.py`) chunks segments into non-overlapping windows.
- `parse_ranges()` (`llm.py`) requires the LLM ranges to cover `0..N-1`
  exactly, so the first bunsetsu of a window always starts a sentence and the
  last always ends one.
- `rebuild_window_segments()` (`segment.py`) forces the first slice to start at
  word 0 and the last to end at the final word.

Consequently any sentence that straddles a window boundary is permanently split
into two segments — the windows are processed independently and can never merge
across the seam.

## Decision summary

- **Approach: carry-over (sliding) windowing.** Emit all sentences of a window
  except the last; carry the last (possibly-incomplete) sentence's words into
  the next window so they are re-grouped with the continuation. No extra LLM
  calls — the same number of windows, just overlapping content. The last
  window's last sentence is final.
- **Run-on guard: only carry if the window split into >1 sentence.** If a window
  comes back as a single sentence, emit it as-is and reset carry (the window
  boundary is accepted as a real break). This bounds context growth; a genuine
  run-on longer than a window gets split, which is rare and acceptable.
- **Batch size is already configurable.** `sentence_split.window_segments`
  (default 20) *is* the batch size (segments per LLM call). No new config key.

## Scope

All changes are confined to `resegment_json()` in
`src/nagare_clip/sentence_split/cli.py`. `iter_windows`, the rest of
`segment.py`, `llm.py`, and `parse_ranges` are unchanged.

## Core change

New per-window loop in `resegment_json()`, threading a `carry_words` list (the
held-back trailing sentence's words) across iterations:

```
carry_words = []
windows = list(iter_windows(segments, window))
for idx, (base, win) in enumerate(windows):
    is_last = idx == len(windows) - 1
    _, win_words = window_text_and_words(win)
    carried_in = carry_words            # remember for degrade flush
    words = carry_words + win_words      # prepend carry
    text  = "".join(str(w.get("word", "")) for w in words)
    carry_words = []
    if not text:                         # both carry and window empty
        new_segments.extend(win)
        continue
    bunsetsu = bunsetsu_units(text, nlp)
    ranges = split_window(bunsetsu, sp_cfg, recorder=recorder,
                          unit=f"{stem}.w{base + 1}") if bunsetsu else None
    if not bunsetsu or ranges is None:   # degrade
        if carried_in:
            new_segments.append(segment_from_words(carried_in))
        new_segments.extend(win)
        continue
    char2word = char_to_word_index(words)
    rebuilt = rebuild_window_segments(words, bunsetsu, ranges, char2word)
    if not is_last and len(rebuilt) > 1: # run-on guard
        carry_words = rebuilt[-1].get("words", [])
        new_segments.extend(rebuilt[:-1])
    else:
        new_segments.extend(rebuilt)     # single-sentence window: accept break
# last window always has is_last True, so no trailing carry remains
```

`segment_from_words` is already exported from `segment.py`; import it into
`cli.py` alongside the existing helpers.

### Invariants & safety

- **Verbatim invariant preserved.** Words are only reassigned, never dropped or
  duplicated; carried words are emitted exactly once — either finalized on
  degrade (`segment_from_words(carried_in)`) or re-grouped in the next window.
  The existing global `concat_word_text(new_segments) != concat_word_text(...)`
  check still guards the whole result and copies through on violation.
- **Degrade safety.** On no-bunsetsu / LLM-fail, any carried-in sentence is
  flushed as its own finalized segment *before* falling back to the window's
  original segments, so nothing is lost and the fallback stays local.
- **Run-on guard.** Carry only when `len(rebuilt) > 1`. A single-sentence window
  emits as-is and resets carry.
- **Unchanged behavior.** Recorder `unit` naming (`{stem}.w{base+1}`),
  copy-through-when-disabled, and degrade-to-original semantics are unchanged.
  Single-window transcripts (one window, `is_last`) behave exactly as before.

## Testing (TDD)

In `tests/sentence_split/test_cli.py`. The `split_window` stub branches on the
bunsetsu surface it receives so it returns window-specific ranges (carry changes
window 2's content); `bunsetsu_units` is stubbed per-char. No GiNZA/model needed.

1. **Cross-boundary merge** — `window_segments=2`, 3 segments: win1=`["あい",
   "うえ"]`, win2=`["お"]`. Stub splits win1 into `["あい","うえ"]` (>1 → carries
   `うえ`); win2 (last) sees `うえお` → one sentence. Assert output
   `["あい","うえお"]`. *Mutation: disable carry → `["あい","うえ","お"]`, fails.*
2. **Run-on guard** — `window_segments=1`, two segments; stub returns the whole
   window as one sentence each time. Assert output stays `["あい","うえ"]` (no
   carry) and verbatim holds. *Mutation: drop the `len(rebuilt) > 1` guard → it
   carries and merges, fails.*
3. **Degrade flushes carry-in** — win1 carries `うえ`, win2 returns `None`.
   Assert output `["あい","うえ","お"]` and `concat_word_text` preserved.
   *Mutation: degrade `continue` without flushing carry → `うえ` lost, fails.*
4. **Backward compat** — existing `test_resegment_rebuilds_and_preserves_text`
   and `test_resegment_degraded_window_keeps_original` stay green unchanged.

Per the repo TDD rule: write tests first, confirm red, implement, then re-run
each per-test mutation check and report the red→green evidence.

## Documentation

Update the `sentence_split` description in `AGENTS.md` (and `README.md` /
`plan.md` if they describe the windowing) to note that windowing carries the
trailing sentence across window boundaries so cross-seam sentences are merged,
except a single-sentence window which accepts the boundary as a break.
