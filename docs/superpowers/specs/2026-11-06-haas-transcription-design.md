# Haas-ja-v1.0 transcription + Silero VAD — design

Replace the WhisperX transcription stage with Haas-ja-v1.0 (third-intelligence/Haas-ASR,
NeMo TDT) and the ffmpeg-silencedetect `audio_silence` stage with Silero VAD v6, which now
runs **first** and defines the transcription chunks. Downstream stages are unchanged: the
transcription output keeps the WhisperX JSON shape.

## Decisions

1. **Separate uv subproject `transcribe/`** holds every heavy dependency (haas-asr, NeMo
   2.7, torch, Triton, silero-vad). The main package never imports it; the pipeline runs it
   as a subprocess: `uv run --project <repo>/transcribe python -m nagare_transcribe <cmd>
   <request.json>`. One process per stage for all sources (Haas warm-up is several seconds).
   The subproject is a thin worker: decode audio, run VAD, run Haas, return raw results as
   JSON. All shaping (cuts, chunk packing, WhisperX JSON) lives in the main package, so CI
   tests it without the heavy environment.
2. **Pins** (newer versions break Haas's KDA decoder, measured: torch 2.15 / triton 3.9 →
   `ValueError('key contains names that are not kernel arguments: BT ...')`):
   `torch==2.14.0`, `triton==3.8.0`, `numba==0.67.0`; haas-asr pinned to git commit
   `acc614f68769dcbfae9dba96b48439c4a66295af`; Haas's own `override-dependencies`
   (`fsspec[http]>=2026.6.0`, `hydra-core>=1.3.8`, `packaging>=26.3`,
   `transformers>=5.10.0`) copied, since they apply only to its own repo; `silero-vad>=6,<7`;
   `compile-bytecode = true`; Python 3.12.
3. **Stage order:** `audio_silence` → `transcription` → `sentence_split` → …
4. **audio_silence = Silero VAD v6.** Always runs VAD (writes `{stem}_vad.json`: media
   duration + speech regions). `{stem}_cuts.txt` keeps its format: the silences are the
   complement of the speech regions within `[0, duration]`, gaps shorter than `min_silence`
   dropped; `# media duration:` always recorded. The stage is always on:
   `audio_silence.enabled` is removed (fails config load) — transcription needs the regions,
   and a human who wants to keep a silence deletes its line from `_cuts.txt`. Params (measured on a real 120 s clip: 34 regions, 2/377 WhisperX
   chars outside, both harmless): `threshold 0.5`, `min_speech 0.25` s, `min_silence 0.5` s,
   `speech_pad 0.2` s. `audio_silence.noise` and `audio_silence.enabled` are removed (fail
   config load).
5. **Audio decode:** host ffmpeg in the subproject
   (`ffmpeg -nostdin -v error -i SRC -ac 1 -ar 16000 -f f32le -`). This is an exception to the
   "ffmpeg via the whisperx Docker image" rule, limited to `transcribe/`. gap_context and
   publish keep their Docker ffmpeg (untouched in this change); docker-compose stays.
6. **Chunking = "packed":** consecutive VAD regions are merged greedily into one chunk while
   the chunk spans ≤ `max_chunk_seconds` (30 s); every chunk starts at a region start. A
   single region longer than the limit is sub-split inside the subproject with
   `haas_asr.split_at_quiet_points`. Measured: packed keeps fillers (あの/ま/ね), adds almost
   no punctuation, and removes the multi-second chunk-start timestamp error seen with plain
   30 s chunks (「今」 92.81 → 96.92 s, WhisperX 97.215); one-chunk-per-region lost fillers
   and misread short chunks.
7. **Timestamps:** `model.transcribe(chunks, timestamps=True)`; `h.timestamp["char"]` is a
   list of **token** entries `{"char": [str, ...], "start", "end"}`, seconds relative to the
   chunk, 80 ms resolution, durations 0–0.32 s. Add the chunk offset; strip whitespace and
   `▁` from token text; drop tokens left empty (one appears at every chunk start). Text with
   and without `timestamps=True` is identical (measured).
8. **WhisperX-compatible JSON:** `{"segments": [...], "word_segments": [...],
   "language": "ja"}`. **One segment per VAD region** (tokens assigned to the region holding
   their start; a token in a gap goes to the nearer region, tie → earlier); regions with no
   tokens yield no segment. `words` are **one entry per character** — intervals indexes
   `words` by character position (`intervals/bunsetu.py`, `intervals/sync_json.py`
   patches and markers) — with a token's time split evenly over its characters;
   `score` is `1.0` (Haas has no confidence; only `sync_json` averages it). Segment
   `text == "".join(word)` with no whitespace. `{stem}.txt` = one segment text per line.
9. **sentence_split.window_segments default 20 → 200**, so a window spans about the same
   time as with WhisperX's ~25 s segments (VAD regions average ~2 s).
10. **WhisperX is no longer run.** Feeding a WhisperX transcript to text_filter as a diff hint
    is a separate, later task (Docker is kept for it).
11. **Config:** `transcription:` becomes `model` (`third-intelligence/Haas-ja-v1.0`),
    `batch_size` (8), `max_chunk_seconds` (30.0); `compute_type`, `language`, `align_model`
    are removed (fail config load).

## Out of scope

WhisperX diff hints for text_filter; moving gap_context/publish ffmpeg off Docker;
Haas `Vocabulary` boosting; time-based sentence_split windows.
