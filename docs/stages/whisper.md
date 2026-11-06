# whisper — Whisper hints for text_filter

Haas-ja-v1.0 is the transcript. A second recognizer, faster-whisper (default `large-v3`), transcribes the same chunks; where its reading of a line differs, text_filter's LLM is shown it as a reference so it can fix Haas misrecognitions. Haas stays the base: the LLM still only emits `{{old->new}}` patches on the Haas line. Design: `docs/superpowers/specs/2026-11-07-whisper-hints-design.md`.

## Measured (probe, RTX 3060, 120 s, 4 packed chunks)

- `ghcr.io/jim60105/whisperx:large-v3-ja` contains faster-whisper 1.2.0 (`BatchedInferencePipeline`), `/venv/bin/python` 3.11 and the model baked at `/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots/*/`. Its entrypoint is `dumb-init -- sh -c "whisperx ..."`, so the stage overrides it with `--entrypoint python`. `soundfile` is absent; `faster_whisper.decode_audio` works.
- One batched `transcribe(audio, clip_timestamps=[{"start": s, "end": e}, ...], batch_size=8, vad_filter=False, language="ja", beam_size=5, condition_on_previous_text=False, without_timestamps=True)` returned one segment per chunk, boundaries equal to the chunks: 2.9 s for 117 s (load 2.7 s) vs 4.3 s for a per-chunk loop; texts 0.997 similar.
- Similarity: Whisper-old WhisperX 0.972, Whisper-Haas 0.963. Whisper drops fillers, normalises colloquial forms (流してる→流している), inserts stray spaces and hallucinated once (えっと→越冬). Hence hints only, never truth.

## Decisions

2. **Runs in the existing `whisperx` compose service**: ONE `docker compose run -T --entrypoint python whisperx - <request>` for all sources (`pipeline.external.build_whisper_cmd`). The worker script goes on **stdin** (no argv limit, nothing on disk), is a standalone file (`src/nagare_clip/whisper/worker.py`) that never imports `nagare_clip`, and imports faster-whisper inside `main()` so its pure helpers are testable in the main environment. The main package never imports `faster_whisper`.
3. **The `./cache:/.cache` volume is gone from the compose service.** It only served WhisperX's model downloads (no longer run) and hid the model baked into the image. gap_context/publish only run ffmpeg there.
4. **Chunks = the chunks Haas used**, from `output/transcription/{stem}_haas.json` (`"chunks"`, after sub-splitting), so both transcripts cover identical audio. A chunk over 30 s may come back as several segments; segments are grouped back to the chunk containing their start.
5. **Worker protocol.** Request (`output/whisper/whisper_request.json`, container paths): `{"model", "batch_size", "beam_size", "sources": [{"path": "/app/<relative>", "out": "/output/whisper/{stem}_whisper.json", "chunks": [[s, e], ...]}]}`. Output per source: `{"chunks": [[s, e], ...], "texts": [str, ...]}` (one text per chunk, whitespace removed). The model is the first match of the baked snapshot glob for `faster-whisper-<model>`, loaded offline (`HF_HUB_OFFLINE=1`, `WhisperModel(path, device="cuda", compute_type="float16")`); a source with no chunks writes `{"chunks": [], "texts": []}` and the model loads only if some source has chunks.
6. **Line hints** (`whisper/hints.py`, pure). Inputs: the line segments text_filter sees (`output/sentence_split/{stem}.json`, per-character `words` with times) and the whisper output. Each Haas character belongs to the chunk containing its `start` (nearest chunk if none). Per chunk the Haas characters are aligned with the Whisper text (difflib, `autojunk=False`); a line's hint is the Whisper span covering its characters' aligned positions (an insertion between two lines goes to the earlier line). A hint equal to the line text is `None`. Result: `list[str | None]`, one per line.
7. **text_filter hand-off.** See [`text_filter.md`](text_filter.md#whisper-hints). `_text_filter_run` loads hints only when `whisper.enabled`, and drops them (warning) when their count differs from the `.txt` line count. A missing or unreadable whisper file gives no hints for that source.

## Stage behaviour

- Config `whisper:` `enabled: false`, `model: "large-v3"`, `batch_size: 8`, `beam_size: 5`. Language is fixed to `ja`.
- Disabled: prints a skip line; no Docker call, no output. text_filter uses no hints even if `{stem}_whisper.json` files exist.
- `required_outputs`: `{stem}_whisper.json` for each stem, only when enabled and the run starts at or before `text_filter`.
- Needs a CUDA GPU (like transcription). Re-run just this stage with `--from-stage whisper --to-stage whisper`.

## Out of scope

Tuning `text_filter.batch_size` for short lines (a config change); retuning `director.max_keep_lines` / `gap_context.context_lines`; Whisper word timestamps.
