# Sentence-Split Stage — Design

Date: 2026-06-29
Status: Approved design, pre-implementation

## Goal

Add a new pipeline stage, **`sentence_split`**, that re-segments the WhisperX
transcript into natural one-sentence units using an LLM. WhisperX segments often
pack several sentences into one line or cut a sentence mid-way, which hurts both
caption readability and every downstream LLM stage that reasons by line ranges
(`summary`, `plan`, `director`, `guided_edit`).

The stage rewrites `{stem}.json` (re-segmented) **and** `{stem}.txt` (one new
sentence per line). It is **disabled by default**; when disabled it copies the
transcription `.json`/`.txt` through byte-identically, so landing it changes
nothing until a project opts in.

## Non-goals

- Changing the transcript **text**. The stage only moves segment boundaries;
  every character (and its WhisperX word timing) is preserved verbatim.
- Caption re-chunking. Captions are already re-chunked at bunsetsu level in the
  `intervals` stage; this stage improves the *segment/line* granularity that the
  LLM stages consume, which captions also benefit from but is not driven here.
- Punctuation insertion or mis-dictation cleanup (the latter is `text_filter`'s
  job).
- Replacing GiNZA usage elsewhere; `intervals/bunsetu.py` is reused, not changed.

## Background: why this approach (measured)

Three boundary-detection approaches were prototyped against a real 122-segment
source (`PXL_20260324_092107933`, 7,331 char-tokens) on a local model
(`gemma4:31b`, Ollama `/api/generate`, temperature 0, 20-segment chunks):

| Approach | Verbatim-safe | Speed | Quality |
| --- | --- | --- | --- |
| A — char-index ranges | 1/7 | timeouts | unusable (truncated JSON) |
| B — delimiter `│` in plain text | 5/7 | fast | over-splits ~3.5×, deletes source `。` |
| C — merge/split ops | 5/7\* (unvalidated) | slow (timeouts) | — |
| **A′ — bunsetsu-index ranges** | **7/7** | OK, no timeouts | natural sentences |

Decisive finding: **WhisperX tokenizes Japanese per character**, so char-level
numbering (Approach A) is too token-heavy — the model cannot emit the long range
list and times out. Numbering at **bunsetsu** granularity (GiNZA
`ginza.bunsetu_spans()`) shrinks both the numbered input and the output range
list ~3–6×, and — because a sentence is always a whole run of bunsetsu — the
LLM only does the easy grouping while GiNZA does the hard tokenization. Every
chunk came back valid and verbatim.

This is the locked approach.

## Identity & placement

- Config section: `sentence_split:`
- Package: `src/nagare_clip/sentence_split/`
- Output dir: `output/sentence_split/`
- `STAGE_ORDER`: `transcription → audio_silence → sentence_split → text_filter →
  summary → plan → director → guided_edit → intervals → blender`

The stage depends only on transcription's `{stem}.json`/`{stem}.txt`.
`audio_silence` is independent of segmentation (it emits absolute-timestamp
`{stem}_cuts.txt`), so placing `sentence_split` after it is correct and keeps the
existing audio path untouched.

## Contract

The whole pipeline rests on one invariant: **each line of `_edits.txt` maps 1:1
to a `segment` in the WhisperX JSON**, and each segment carries word-level
timings (`intervals/check_edits.py`, `intervals/sync_json.py`). Re-segmentation
must therefore rewrite **both** artifacts together:

- **Inputs:** `output/transcription/{stem}.json`, `output/transcription/{stem}.txt`
- **Outputs:** `output/sentence_split/{stem}.json` (re-segmented),
  `output/sentence_split/{stem}.txt` (one new segment per line)

All downstream `.json`/`.txt` consumers repoint from `TRANSCRIPTION_DIR` to
`SENTENCE_SPLIT_DIR`. In `scripts/run_pipeline.sh` today these are:

- `text_filter`: `--txt …/{stem}.txt`
- `intervals`: `--json …/{stem}.json`
- `director`: `--json …/{stem}.json`
- `summary`: `--json …/{stem}.json`
- `guided_edit`: `--json …/{stem}.json`

Because words are only **reassigned** to new segments (never edited), the
concatenation of all words is unchanged and timing is lossless.

## Algorithm

Per source, processed in **windows of W whole segments** (`window_segments`,
default 20):

1. **Window text + index map.** Concatenate the window's char-word `word` fields
   into `window_text`. Build a cumulative char→word-index map; do **not** assume
   1 char = 1 word (handle multi-char or space word entries).
2. **Bunsetsu.** `ginza.bunsetu_spans(window_text)` → numbered bunsetsu list with
   `start_char`/`end_char`. (GiNZA loaded lazily, mirroring `bunsetu.py`.)
3. **LLM call.** Through `nagare_clip.llm_client.call_llm` with `response_format:
   "json"`, recorded via `llm_report.Recorder` (stage `sentence_split`, unit
   `stem`/window). Prompt presents the numbered bunsetsu and requests
   `{"sentences": [[a, b], …]}` of **bunsetsu-index** ranges.
4. **Validate.** Ranges must be contiguous and cover `0..M-1`. On invalid output
   or LLM/connection failure, retry per `llm_retry` (`max_retries`, default 2,
   nudging temperature via `cfg_for_attempt()`); after retries fail, **fall back
   to the window's original segmentation** (graceful degrade, matching the other
   LLM stages). A valid `{"sentences": [...]}` covering everything as a single
   range (no split) is accepted without retry.
5. **Rebuild segments.** For each sentence, take the char span
   `[bunsetsu[first].start_char, nextSentence.first.start_char)` (last sentence
   runs to end of `window_text`) — a **partition**, so inter-bunsetsu spaces are
   assigned to the preceding sentence and every char is covered. Map the char
   span to a word-index slice via the cumulative map; the new segment's `words`
   is that slice (each word unchanged), `text` is the joined word fields, and
   `start`/`end` come from the first/last word's timing.
6. **Rebuild `word_segments`.** Concatenate all new segments' words into the
   top-level `word_segments` array (mirrors `sync_json.sync_text_to_json`).

### Verbatim invariant (safety net)

After rebuild, assert that the concatenation of all new segments' word fields
equals the concatenation of the original words. By construction this always
holds; if it ever fails (e.g. a future GiNZA offset edge case), fall back to the
original segmentation for that source and log it. This makes the stage incapable
of corrupting the transcript.

### Window boundaries

Windows are aligned to whole original segments, so a window edge forces a
sentence boundary. This can occasionally split one spoken sentence across two
windows — a rare, harmless extra break (never a text change). W=20 keeps the
model stable (per the measurements) while making such splits infrequent.

## Modules (isolated, testable)

- `sentence_split/segment.py` — **pure**, no I/O or LLM: window iteration,
  cumulative char↔word-index map, `rebuild_segments(words, ranges, bunsetsu)`,
  the verbatim check. The core that tests pin down.
- `sentence_split/llm.py` — prompt construction, response parse + range
  validation, retry/degrade. GiNZA/spaCy imported lazily.
- `sentence_split/cli.py` — config loading (`--config`), `llm_report` recorder
  wiring, per-window orchestration, **copy-through when disabled**, writing
  `{stem}.json` + `{stem}.txt`. Mirrors existing stage CLIs (e.g. `summary/cli.py`).

CLI shape:
`python -m nagare_clip.sentence_split.cli --json <in.json> --output-json <out.json>
--output-txt <out.txt> [--config <path>] [--stem <name>]`.

## Configuration

New `sentence_split:` section in `config.py` `DEFAULTS` and documented in
`config.example.yml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | When false, copy transcription `.json`/`.txt` through byte-identically |
| `provider` | `ollama_chat` | LiteLLM backend (as other LLM stages) |
| `model` | (example value) | Model id |
| `api_base` | `""` | Empty → Ollama localhost / omitted for cloud |
| `api_key` | `""` | Forwarded when set |
| `thinking` | `false` | Maps to LiteLLM `reasoning_effort` |
| `window_segments` | `20` | Segments per LLM window |
| `max_retries` | `2` | Bounded retries via `llm_retry` |

Priority order unchanged: CLI flags > YAML > `DEFAULTS`.

## `scripts/run_pipeline.sh`

- Add `sentence_split` to `STAGE_ORDER` between `audio_silence` and
  `text_filter`; add `ORD_SENTENCE_SPLIT` and `SENTENCE_SPLIT_DIR=
  "${OUTPUT_DIR}/sentence_split"` (and to `mkdir -p`).
- Add the stage block: always runs when `in_window`; the CLI no-ops (copy-through)
  when disabled, so `output/sentence_split/{stem}.{json,txt}` always exists for
  downstream — consistent with how `summary`/`plan`/`director`/`guided_edit`
  always run.
- Repoint the five downstream `--txt`/`--json` arguments from `TRANSCRIPTION_DIR`
  to `SENTENCE_SPLIT_DIR`.
- Skip-validation (`past_window`): when starting past `sentence_split`, require
  `SENTENCE_SPLIT_DIR/{stem}.json` and `.txt` to exist with friendly errors.
- Pass `--config` through when provided; `--json` is the transcription JSON,
  outputs go to `SENTENCE_SPLIT_DIR`.

## Testing (TDD, with mutation-catch evidence)

Per repo policy, write tests first; where implementation already exists, mutate
it to confirm each test fails, then revert, and report the evidence.

- **`rebuild_segments` (pure):** known words + ranges → correct word-slice
  boundaries, `text`/`start`/`end` rebuilt, `word_segments` concatenated.
  Mutation: off-by-one the slice boundary; confirm failure.
- **Verbatim invariant:** property — concatenation of new words == original
  words for arbitrary valid ranges. Mutation: drop a word in rebuild; confirm
  failure.
- **char↔word map:** multi-char and space word entries map to correct word
  indices. Mutation: assume 1 char = 1 word; confirm failure on a multi-char
  fixture.
- **Validation → degrade:** non-contiguous / out-of-range / partial-coverage
  ranges fall back to original segmentation. Malformed JSON / LLM error → degrade.
- **Disabled copy-through:** output `.json`/`.txt` are byte-identical to the
  transcription inputs.
- **CLI integration:** end-to-end on a small fixture JSON (stub the LLM call to
  return fixed ranges), asserting `.txt` line count == new segment count and the
  1:1 `_edits.txt` contract would hold (`check_edits` clean).

GiNZA-dependent tests load `ja_ginza` (already a runtime dep) or stub
`bunsetu_spans` where the LLM/segment logic is what's under test.

## Documentation

Update on implementation: `AGENTS.md` (new stage section, Project Structure
package entry, naming-convention list, `STAGE_ORDER`), `README.md` (user-facing
usage + the new output dir), `plan.md` (status), `config.example.yml` (the
`sentence_split:` block).

## Rollout / risk

Default-off with byte-identical copy-through means **zero behavior change** until
a project sets `sentence_split.enabled: true`; this is regression-guarded by the
copy-through test. When enabled, the verbatim invariant + graceful degrade make
the worst case "no re-segmentation for this source," never a corrupted
transcript.
