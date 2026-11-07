# Whisper large-v3 hints for text_filter — design

Haas-ja-v1.0 is the transcript. A second recognizer, Whisper large-v3, transcribes the
same chunks; where its reading of a line differs, text_filter's LLM is shown it as a
reference so it can fix Haas misrecognitions (measured on a 120 s clip: Haas 「散乱感」/
「水が」 where Whisper and the old WhisperX had 「産卵管」/「水位が」). Haas stays the base:
the LLM still only emits `{{old->new}}` patches on the Haas line.

## Measured (probe, RTX 3060, 120 s, 4 packed chunks)

- `ghcr.io/jim60105/whisperx:large-v3-ja` contains faster-whisper 1.2.0
  (`WhisperModel`), Python `/venv/bin/python` 3.11, and the model
  baked at `/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots/*/`.
  Entrypoint is `dumb-init -- sh -c "whisperx ..."` → override with `--entrypoint python`.
  `soundfile` is absent; `faster_whisper.decode_audio` works.
- Per-chunk `model.transcribe(audio_slice, vad_filter=False, language="ja", beam_size=5,
  condition_on_previous_text=False, without_timestamps=True)`, one call per chunk, texts
  joined per call. (A single batched `clip_timestamps` call was tried first and rejected:
  faster-whisper 1.2.0 merges neighbouring clips of at most 30 s total into one window and
  returns one segment, so attribution to chunks fails. Final-review ruling: per-chunk.)
- Similarity: Whisper↔old WhisperX 0.972, Whisper↔Haas 0.963. Whisper drops fillers,
  normalises colloquial forms (流してる→流している), inserts stray spaces, and hallucinated
  once (えっと→越冬). Hence: hints only, never truth.

## Decisions

1. **New stage `whisper`**, right after `transcription`:
   `audio_silence → transcription → whisper → sentence_split → …`. Disabled by default
   (`whisper.enabled: false`); disabled = no Docker call, no output, and text_filter uses
   no hints even if old `{stem}_whisper.json` files exist.
2. **Runs in the existing `whisperx` compose service** (same image), ONE
   `docker compose run -T --entrypoint python whisperx - <request>` for all sources; the
   worker script goes on **stdin** (same reason as gap_context's `ShellBatch`: no argv
   limit, nothing left on disk). The script is a standalone file in the repo that never
   imports `nagare_clip`; faster-whisper is imported inside its `main()` so its pure
   helpers are testable in the main environment.
3. **Remove the `./cache:/.cache` volume from the compose service.** It only served
   WhisperX's model downloads (no longer run) and it hides the model baked into the image.
   gap_context/publish only run ffmpeg there and do not need it.
4. **Chunks = the chunks Haas used**, read from `output/transcription/{stem}_haas.json`
   (`"chunks"`, after sub-splitting), so both transcripts cover identical audio. A chunk
   over 30 s (Whisper decodes 30 s windows) triggers a stderr warning. Each chunk is
   decoded by its own `transcribe` call, so its text is attributed by construction.
5. **Worker protocol.** Request (written by the pipeline to
   `output/whisper/whisper_request.json`, container paths):
   `{"model": "large-v3", "beam_size": int, "sources": [{"path":
   "/app/<relative>", "out": "/output/whisper/{stem}_whisper.json", "chunks": [[s, e],
   ...]}]}`. Output per source: `{"chunks": [[s, e], ...], "texts": [str, ...]}` (one text
   per chunk, whitespace removed). The model is resolved from the baked snapshot glob for
   `faster-whisper-<model>`; offline (`HF_HUB_OFFLINE=1`); a source with no chunks writes
   `{"chunks": [], "texts": []}` and the model is loaded only if some source has chunks.
6. **Line hints (pure, main package).** Inputs: the line segments text_filter will see
   (`output/sentence_split/{stem}.json` — one segment per `.txt` line, per-character
   `words` with times) and the whisper output. Each Haas character belongs to the chunk
   containing its `start` (nearest chunk if in none). Per chunk, align that chunk's Haas
   characters with its Whisper text (difflib, `autojunk=False`); each line's hint is the
   Whisper span covering its characters' aligned positions (an insertion between two lines
   goes to the earlier line). A hint equal to the line text ignoring punctuation and whitespace (Unicode category `P*`) → `None`; a differing hint is shown with punctuation and whitespace removed (in a real run the LLM copied Whisper's punctuation into a patch). Result:
   `list[str | None]`, one per line.
7. **text_filter.** Only when hints are given and at least one is not `None`: a fixed
   instruction block is appended to the system prompt (after the summary context), and
   each batch's user message gets, after the numbered lines, a reference block listing
   `N: <hint>` for that batch's lines with a hint. Retries (halved batches) carry the
   hints of their own lines. Output format unchanged. No hints → messages byte-identical
   to today (regression-tested). A missing/unreadable whisper file for an enabled stage →
   warning, no hints for that source.
8. **Config `whisper:`**: `enabled: false`, `model: "large-v3"`,
   `beam_size: 5`. Language is fixed to `ja` (Haas is Japanese-only).

## Out of scope

Tuning `text_filter.batch_size` for ~2 s lines (a user-config change); retuning
`director.max_keep_lines` / `gap_context.context_lines`; Whisper word timestamps.
