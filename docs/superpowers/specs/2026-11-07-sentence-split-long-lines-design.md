# sentence_split splits long lines only — design

Since `ed7ac82` a transcript line (`{stem}.json` segment / `.txt` line) is exactly a speech span
between two audio_silence cuts, and the user judged those VAD-made boundaries better than the
LLM's merge-and-resegment guess. But a span with no pause of `audio_silence.min_silence` can run
long (test video: 13 of 57 lines over 8 s, longest 22 s / 127 chars). So sentence_split changes
from "merge everything, then re-segment" to "split only the long lines; never merge, never touch a
short line".

## Decisions

1. **Threshold.** `sentence_split.max_line_seconds` (float, default `8.0`). A segment with
   `end - start <= max_line_seconds` (or without numeric `start`/`end`) is copied unchanged — the
   same dict, not rebuilt — and never sent to the LLM.
2. **One LLM call per long line**, no neighbouring-line context. The user content is that line's
   GiNZA bunsetsu units, numbered as today; the reply is `{"sentences": [[a, b], ...]}` bunsetsu
   index ranges that cover every unit once, in order (today's `parse_ranges` validation). A single
   range (no natural boundary) is a valid answer and keeps the line. A line with fewer than 2
   bunsetsu is not sent.
3. **Rebuild** reuses today's word-preserving path (`rebuild_window_segments` on the line's own
   words, snapped to whole words, original word objects/timings), guarded by the
   `concat_word_text` before/after check for that line. Any failure (LLM error after retries,
   invalid ranges, verbatim mismatch) keeps the original line and is logged / recorded in the LLM
   report.
4. **Removed:** windowing (`window_segments`), carry-over over window seams, the single-sentence
   window rule, and force-split at long silences (`force_split`, `force_split_min_silence`) —
   every cut is already a line boundary, so it can no longer do anything. The three keys go into
   `config.REMOVED_KEYS` (loading a config that sets them fails, naming each path).
   sentence_split no longer reads `{stem}_cuts.txt`.
5. **Kept:** `enabled` (default `false`), the provider/model/sampling/retry keys, `prompt` (its
   default is rewritten for "split this one line"), and disabled = byte-identical copy-through of
   `output/transcription/{stem}.{json,txt}`. An enabled run where no line is long writes output
   equal in content to the input (no LLM call).
6. **Output:** the rewritten `{stem}.json` (segments in order, each line either untouched or
   replaced by its pieces) and `{stem}.txt` (one segment per line), as today.

## Out of scope

Choosing split points from word-gap timing (Haas token timings are too coarse); recursive splitting
of a piece still over the threshold; changing `audio_silence.min_silence`.
