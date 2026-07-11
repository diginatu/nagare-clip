# sentence_split — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#sentence_split--llm-sentence-re-segmentation).

## Re-segmentation core (verbatim by construction)

- The LLM returns **bunsetsu-index ranges** (`{"sentences":[[a,b],…]}`), not text: each range is a contiguous span of GiNZA bunsetsu units forming one sentence. `segment.rebuild_window_segments()` slices the original word list at bunsetsu char boundaries, snapped to whole-word boundaries via `char2word` (never splitting a word), and assembles new segments from the existing word objects — so word timings are preserved and output text is verbatim.
- A final `concat_word_text` check (before/after) guards against any boundary-rounding discrepancy; if it fails, the original segmentation is kept for that window. This global invariant is the ultimate guard behind every code path below.

## Windowing & carry-over

- Processing is windowed (`sentence_split.window_segments`, default 20 segments per LLM call — the batch size) so long transcripts fit in context. Each window degrades independently on LLM failure, returning the original window segments unchanged.
- Windows are non-overlapping but **carry the trailing sentence over the seam**: all sentences of a window except the last are emitted, and the last (possibly incomplete) sentence's words are prepended to the next window (`resegment_json`), so a sentence straddling a window boundary is re-grouped with its continuation instead of being split at the seam.
- A **single-sentence window** is the exception: it emits as-is and resets the carry (run-on guard, bounding context growth), so a sentence longer than a full window still splits at the seam.
- On **degrade**, any carried-in sentence is flushed as a finalized segment *before* falling back to the window's original segments, so carry-over never loses words.
- Disabled → byte-identical copy-through of `output/transcription/{stem}.{json,txt}`.

## Force split at long silences

Config `force_split` (default `true`), `force_split_min_silence` (default 3.0s — deliberately higher than `audio_silence.min_silence`, which targets short jump-cut pauses).

- The audio_silence `{stem}_cuts.txt` is read (`read_cuts`); spans `>= force_split_min_silence` become hard sentence boundaries. Threshold filtering happens **once** in `run_sentence_split` (`_forced_silences`), so the pure `split_segment_at_silences` receives only already-qualifying spans.
- `resegment_json` post-splits **every** emitted segment — LLM-rebuilt, carry-flush, and degrade-fallback — via `segment.split_segment_at_silences()`.
- **Corroboration rule:** the split cuts before the first word whose start-time (inheriting the last known time for untimed words) is `>=` the silence midpoint, **only when the previous word's end-time is `<=` that midpoint** — i.e. a genuine inter-word gap brackets the midpoint. WhisperX often stretches one word across an entire pause (its end abutting the next word); the midpoint then falls *inside* that word, which sentence it belongs to is undecidable from timing, and splitting anyway would chop the sentence's first characters off. Such silences are skipped — a false negative over a wrong split. (Real-project testing: 116 of 199 long silences fell inside a stretched word; a gap-only detector without the cuts file found only 3 of 199, since the same stretching hides the gaps.)
- **Pure post-enforcement:** window contents and the LLM call count are byte-identical to before (`silences=[]` is a regression-guarded no-op), the split survives total LLM failure, and because it runs *before* the carry decision, carry-over can never drag words across a silence.
- Since the cuts file is human-editable, deleting a line (keeping that span's audio) also stops it forcing a split.
