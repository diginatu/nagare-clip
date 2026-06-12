# nagare-clip — Edit-File Context for LLM Assistance

Paste this document into a chat with a capable LLM (Claude, GPT-4, etc.) before
asking it to help author or revise a `*_edits.txt` file from this pipeline.

## What this is

`nagare-clip` is a rough-cut pipeline for Japanese video. WhisperX transcribes
the audio into numbered segments, an optional small LLM filter produces a
`{stem}_edits.txt` checkpoint, and a human reviews and annotates that file.
Downstream stages then apply the annotations: text patches are synced back into
the WhisperX timing JSON, silent regions are cut, and Blender assembles a VSE
project for final hand-editing.

The `_edits.txt` file is the single human-editable surface for shaping what
survives the cut and how it is presented. Everything below describes what you
may write inside it.

## The edit file

- Plain UTF-8 text. Each line begins with a number, a colon, and the segment
  text — for example `1: それは今日はいい天気ですね`.
- **Preserve the numbering and the surrounding text exactly.** Downstream code
  pairs each line back to a WhisperX segment by its number.
- Edits are expressed only through the four markers below. Do not rephrase,
  reorder, merge, or split lines.
- Blank lines and comment lines are tolerated but unnecessary.

## Markers

### `{{old->new}}` — text patch

Corrects transcription errors. Wraps **only** the erroneous span; everything
outside the braces must remain byte-identical to the input line. Use
`{{old->}}` (empty `new`) to delete `old` entirely.

Example:

```
1: {{えーと->}}それは{{急は->今日は}}いい天気ですね
2: {{(雑音)->}}
```

Notes:

- Multiple `{{...->...}}` patches per line are allowed.
- `old` must be a substring of the original line (do not fabricate).
- The patch operates on text only; it is later re-aligned into the word-level
  WhisperX JSON, so keep edits minimal and local.

### `<keep>...</keep>` — force-preserve audio

Forces the audio under the wrapped text to survive silence cuts (both the
acoustic silence detector and the WhisperX word-gap heuristic). Use it to
protect pauses, sound effects, or deliberate silence that the cut stages would
otherwise remove.

Example (single line):

```
5: <keep>そして……（沈黙）……こう</keep>なりました
```

Example (multi-line span — opens on one line, closes on a later line; all
inter-segment silences inside the span are preserved):

```
7: ここから<keep>静かに
8: 考えている
9: 時間です</keep>続けます
```

Notes:

- Does not extend by the configured keep margins; the span is exactly the time
  from the first wrapped word's start to the last wrapped word's end.
- Empty, unclosed (at EOF), unmatched, or nested `<keep>` tags are skipped with
  a warning.

### `<speed factor="N.N">...</speed>` — preserve and change playback speed

Behaves like `<keep>` (force-preserves audio) **and** instructs the Blender
stage to play the wrapped region at the given speed via a VSE Speed Control
effect strip. `factor` greater than 1.0 speeds up, less than 1.0 slows down.

Example:

```
12: 長い説明を<speed factor="1.5">早送りしたい部分です</speed>普通に戻ります
```

Notes:

- Multi-line spans and nesting/unmatched-tag warnings follow the `<keep>`
  rules.
- Captions inside a sped-up region are time-mapped accordingly so they still
  line up on the final timeline.
- Use `<keep>` when no speed change is wanted; do not write `<speed
  factor="1.0">`.

### `<overlay text="...">...</overlay>` — on-screen text strip

Places a Blender VSE TEXT strip on screen during the wrapped span. Useful for
captions of your own wording, chapter titles, callouts, etc.

Example:

```
20: ここで<overlay text="第1章 導入">本題に入ります</overlay>
```

Notes:

- **Does not preserve audio.** If the wrapped audio is removed by silence
  cuts, the overlay is silently skipped — it cannot display over content that
  no longer exists on the timeline. Wrap the same span in `<keep>` (outside
  the `<overlay>`) if the underlying audio must survive.
- Quotes (`"`) inside the `text="..."` attribute value are **not** supported;
  the parser uses `[^"]*`. Use Japanese quotation marks (`「」`, `『』`) or
  rewrite without quotes.
- Multi-line spans and nesting/unmatched-tag warnings follow the `<keep>`
  rules.

## Authoring rules

- All four markers may coexist on the same line, but **do not nest markers of
  the same type** and do not interleave open/close tags across types in
  confusing ways. Each `<keep>`, `<speed>`, `<overlay>` has exactly one matching
  closer.
- `<keep>`, `<speed>`, and `<overlay>` are intended to be added by the human
  *after* the small filter LLM has produced `_edits.txt`. A larger assisting
  LLM may produce them too, but it should not also re-introduce or alter
  `{{old->new}}` patches the human has already accepted unless asked.
- Any unclosed tag at end-of-file is dropped with a warning; check your spans
  close before the last line.
- Preserve the original line numbers and any non-marker text exactly. The
  downstream sync step matches lines by number and re-aligns timing from the
  corrected text.

## Validating your edits

Before resuming the pipeline you can check a hand-edited `_edits.txt` against
its original WhisperX JSON with the standalone checker. Unlike the downstream
sync step (which fails fast on the *first* error), the checker collects and
reports **every** problem at once, each tied to a line number, so you can fix
them all in one pass:

```bash
uv run python -m nagare_clip.stage3.check_edits \
  --edits-txt output/stage3/myvideo_edits.txt \
  --json output/stage1/myvideo.json
```

It prints one `line N: <message>` per problem, then a count, and exits `1` if
any problem was found (exit `0` and `no problems found` when clean). The checks
mirror what the downstream stages actually do, so a clean result means the file
will sync without error:

- **Line count** — the number of lines must match the number of JSON segments
  (lines map 1:1 to segments).
- **`{{old->new}}` patch syntax** — unbalanced/missing braces are flagged; an
  empty no-op `{{->}}` is flagged. An empty `new` (`{{old->}}`, a deletion) is
  **valid** and is not flagged.
- **Decomposition integrity** — text changed without a patch marker, an `old`
  side that does not match the original transcript at that point, or a line
  that does not cover the full segment text.
- **`<keep>` / `<speed>` / `<overlay>` tags** — balance (nested, unmatched, or
  unclosed-at-EOF tags), `<speed>` factor greater than 0, non-empty
  `<overlay>` text, and malformed tags.

A syntax error on a line suppresses the (otherwise confusing) decomposition
message for that same line, so fix the reported syntax first and re-run.
