# sentence_split — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#sentence_split--split-long-lines).

## What it does

Since transcription breaks lines only where `audio_silence` cuts, a transcript line
(`{stem}.json` segment / `.txt` line) is exactly one speech span between two cuts. Those
VAD-made boundaries are kept: sentence_split **never merges lines and never touches a short
one**. It only splits a line that runs longer than `sentence_split.max_line_seconds`
(default `8.0`) because the speaker never paused for `audio_silence.min_silence`.

- **Which lines.** `run.is_long_line()`: a segment with numeric `start`/`end` and
  `end - start > max_line_seconds` (strictly greater — a line of exactly the threshold
  stays). A segment without numeric times (or with a boolean) is never long. Every other
  segment is copied as **the same dict** (not rebuilt) and is never sent to the LLM.
- **One LLM call per long line**, in order, with no neighbouring-line context
  (`run.split_long_lines()` → `llm.split_line()`). The user content is that line's GiNZA
  bunsetsu units numbered from 0 (`llm.build_messages`, `0:surface 1:surface …`); the reply
  is `{"sentences": [[a, b], ...]}`, validated by `llm.parse_ranges` (contiguous, in order,
  covering every unit once). A line with fewer than two bunsetsu is not sent. Retries follow
  `max_retries`/`retry_temp_step`/`retry_temp_cap` like every LLM stage.
- **LLM report.** One unit per long line, `"{stem} line N"` (N is the 1-based line number in
  the transcription `.txt`).
- GiNZA is loaded only at the first long line (`split_long_lines` takes a `get_nlp` loader),
  so a run where no line is long makes no LLM call and loads no model.
- `max_line_seconds` is validated `> 0` (a zero or negative value would send every timed
  line).

## Rebuild (verbatim by construction)

- The LLM returns **bunsetsu-index ranges**, not text. `segment.rebuild_segments()` slices the
  line's own word list at bunsetsu char boundaries, snapped to whole-word boundaries via
  `char_to_word_index` (never splitting a word), and builds each piece from the existing
  word objects with `segment_from_words` — so word timings are preserved and the text is
  verbatim.
- A `concat_word_text` before/after check for that line guards against any boundary-rounding
  discrepancy.
- **Every failure keeps the original line** (the same dict): LLM error after all retries or
  invalid ranges (recorded in the LLM report), a verbatim mismatch (logged), or a single range
  / a rebuild that yields one piece (the line is one sentence — a valid answer).
- When some line was split, the output's `word_segments` is rebuilt from the new segments.
  When none was (`split_long_lines` returns the input dict itself), `run_sentence_split`
  copies the transcription `{stem}.json`/`.txt` through **byte-identically**, like the
  disabled path — re-serialising would turn a speechless source's empty `.txt` into `"\n"`,
  which guided_edit/intervals then read as one empty speech line.

## Removed

Windowing (`window_segments`), the trailing-sentence carry over window seams, the
single-sentence-window rule, and the force split at long silences (`force_split`,
`force_split_min_silence`) are gone: every cut is already a line boundary, so the forced split
could no longer do anything, and the merge-then-resegment approach was replaced by
splitting long lines only. The three keys are in `config.REMOVED_KEYS`, so a config that sets
any of them fails loading naming each path. sentence_split no longer reads
`{stem}_cuts.txt` (`run_sentence_split` has no `cuts_txt` parameter).

Out of scope: choosing split points from word-gap timing (Haas token timings are too coarse),
recursively splitting a piece still over the threshold.

## Disabled

Disabled (`sentence_split.enabled: false`, the default) → byte-identical copy-through of
`output/transcription/{stem}.{json,txt}`.
