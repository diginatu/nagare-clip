# transcription — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#transcription--haas-ja-transcription).

Haas-ja-v1.0 (third-intelligence/Haas-ASR, NeMo TDT) replaces WhisperX. The stage
runs **after** `audio_silence`: the VAD speech regions it writes
(`{stem}_vad.json`, see [`audio_silence.md`](audio_silence.md)) define what is
transcribed. WhisperX is no longer run; its JSON *shape* is kept so that no later
stage changes.

## Split of work

- The `transcribe/` uv subproject is a thin worker (`python -m nagare_transcribe asr
  REQUEST.json`, started by `external.build_transcribe_cmd()`): decode audio with the
  host `ffmpeg`, run Haas on the chunks it is given, return raw tokens. One process
  for all sources (Haas warm-up takes several seconds). It needs a CUDA GPU. Its heavy
  pins live only in `transcribe/pyproject.toml` (newer torch/triton break Haas's KDA
  decoder); the main package never imports them.
- All shaping lives in the main package so CI tests it without the heavy
  environment: `transcription/chunks.py`, `transcription/whisperx_json.py`,
  `transcription/run.py` (`asr_request()` builds `asr_request.json`,
  `finish_transcription()` turns the worker's `{stem}_haas.json` into
  `{stem}.json`/`{stem}.txt`).
- Resuming at `transcription` (`--from-stage transcription`) needs `{stem}_vad.json`
  on disk (the runner's skip-validation via `_audio_silence_required` lists it when
  starting at `transcription` or earlier); `_transcription_required` asks for
  `{stem}.json`/`.txt` only when resuming at `sentence_split`.

## Chunking: "packed"

`pack_regions()` merges consecutive VAD regions greedily into one chunk while the
chunk spans at most `transcription.max_chunk_seconds` (default 30 s); every chunk
starts at a region start. A single region longer than the limit is sub-split inside
the worker with `haas_asr.split_at_quiet_points`. Measured on real footage: packed
chunks keep fillers (あの / ま / ね), add almost no punctuation, and remove the
multi-second chunk-start timestamp error seen with plain 30 s chunks; one chunk per
region lost fillers and misread short chunks.

## Timestamps

`model.transcribe(chunks, timestamps=True)`. `h.timestamp["char"]` is a list of
**token** entries `{"char": [str, ...], "start", "end"}` (despite the key name, a
token is not a character), in seconds relative to the chunk, at 80 ms resolution,
with durations of 0 to 0.32 s. The worker adds the chunk offset and strips whitespace
and `▁` from the token text; a token left empty is dropped (one appears at every
chunk start). **Byte-fallback:** a character outside the SentencePiece vocab comes
back as one `"�"` entry per UTF-8 byte; the worker takes
`model.tokenizer.ids_to_tokens(h.y_sequence)` (aligned 1:1 with the entries) and joins
each maximal run of `<0xNN>` pieces into one token (UTF-8 decode, `errors="replace"`,
first entry's start to last entry's end). Without pieces, or when their count differs
from the entries (a warning goes to stderr), the entries are used as they are. The transcribed text with and without `timestamps=True` is identical.
The remaining timing error at a chunk start is bounded by `audio_silence.speech_pad`
(the chunk starts that far before the speech).

## The WhisperX-shaped JSON

`build_whisperx_json(tokens, regions)` returns
`{"segments": [...], "word_segments": [...], "language": "ja"}`:

- **One segment per VAD region.** A token belongs to the region holding its start; a
  token in a gap goes to the nearer region (a tie goes to the earlier one). A region
  with no tokens yields no segment.
- **`words` has one entry per non-whitespace character**, because the intervals stage
  indexes `words` by character position (`intervals/bunsetu.py`, `intervals/sync_json.py`
  patches and markers). A token's time is split evenly over its characters.
- `score` is `1.0` (Haas has no confidence; only `sync_json` averages it).
- Segment `text` is `"".join(word)` with no whitespace; `{stem}.txt` is one segment
  text per line.

Because segments are VAD regions (about 2 s on average, against WhisperX's ~25 s),
`sentence_split.window_segments` defaults to 200 (see
[`sentence_split.md`](sentence_split.md)).

## Config

`transcription.model` (`third-intelligence/Haas-ja-v1.0`), `.batch_size` (8),
`.max_chunk_seconds` (30.0). `compute_type`, `language` and `align_model` (and the CLI
flags `--language` / `--align-model`) are removed and fail config loading
(`config.REMOVED_KEYS`). The first run downloads the model.
