# Haas Transcription + Silero VAD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcribe with Haas-ja-v1.0 instead of WhisperX, detect silence with Silero VAD v6 run before transcription, and keep the WhisperX JSON contract so every downstream stage is unchanged.

**Architecture:** A separate uv subproject `transcribe/` (heavy deps) is a thin JSON-in/JSON-out worker run as a subprocess (`vad`, `asr` commands). The main package turns its output into `{stem}_vad.json` → `{stem}_cuts.txt`, packs VAD regions into ≤30 s chunks, and builds WhisperX-shaped `{stem}.json`/`.txt` from Haas tokens.

**Tech Stack:** Python 3.12 (subproject) / 3.11+ (main), uv, haas-asr (NeMo 2.7.3), silero-vad 6.x, host ffmpeg, pydantic config, pytest.

**Spec:** `docs/superpowers/specs/2026-11-06-haas-transcription-design.md` — read it first; decision numbers below refer to it.

## Global Constraints

- `transcribe/` pins exactly: `torch==2.14.0`, `triton==3.8.0`, `numba==0.67.0` (as `[tool.uv] constraint-dependencies`); `haas-asr @ git+https://github.com/third-intelligence/Haas-ASR.git@acc614f68769dcbfae9dba96b48439c4a66295af`; `silero-vad>=6,<7`; `override-dependencies = ["fsspec[http]>=2026.6.0", "hydra-core>=1.3.8", "packaging>=26.3", "transformers>=5.10.0"]`; `compile-bytecode = true`; `requires-python = ">=3.12,<3.13"`.
- The main package must never import `nagare_transcribe`, torch, haas_asr or silero_vad; the subproject must never import `nagare_clip`.
- Subproject results are seconds rounded to 3 decimals.
- No GPU is visible inside the agent sandbox. Never try to work around that; anything that needs CUDA is verified by the user (Task 6). Silero VAD runs on CPU and may be exercised in the sandbox.
- Docker/docker-compose stay (gap_context, publish). Only `transcription` and `audio_silence` stop using Docker.
- Follow AGENTS.md: TDD; for each new test, show it fails first (or mutate the implementation and show the failure); `uv run` everything; `make check` green before each commit; commit on the current branch `diginatu/haas-ja-transcript`; never push.
- `rm` is aliased off — use `\rm`. Don't touch untracked dotfiles in the worktree root (`.bashrc`, `.gitconfig`, `.claude/*` …): they are sandbox artefacts.

## Review Focus

- **A source with no speech** (VAD finds nothing): `_cuts.txt` is one silence `0 - duration` (+ duration comment), `{stem}.json` has `segments: []`, `.txt` is empty, the asr request carries no chunks for it and the worker writes `{"chunks": [], "tokens": []}` without calling the model. Tests in Tasks 1, 2, 3.
- **A single VAD region longer than `max_chunk_seconds`**: packed as its own chunk in the main package, sub-split by the worker; token offsets are each sub-chunk's own start. Tests in Tasks 1, 2.
- **A token whose start lies in a gap between two regions** of one packed chunk (Haas stamps a chunk's first token up to ~0.2 s early): assigned to the nearer region, never dropped; concatenated segment texts equal the concatenated token texts. Test in Task 2.
- **Whitespace / `▁` / non-Japanese text in tokens**: stripped, so `segment.text == "".join(w["word"])` and the character count intervals relies on holds. Tests in Tasks 1, 2.
- **Old config keys** (`audio_silence.noise|enabled`, `transcription.compute_type|language|align_model`) fail config loading naming every path. Tests in Tasks 3, 4.

---

### Task 1: `transcribe/` subproject (worker)

**Files:**
- Create: `transcribe/pyproject.toml`, `transcribe/README.md` (5–10 lines: what it is, `uv sync --project transcribe`, GPU required for `asr`)
- Create: `transcribe/src/nagare_transcribe/__init__.py`, `__main__.py`, `audio.py`, `vad.py`, `asr.py`
- Test: `transcribe/tests/test_vad.py`, `transcribe/tests/test_asr.py`, `transcribe/tests/test_cli.py`
- Modify: `Makefile` (add `test-transcribe: ## Subproject tests (needs uv sync --project transcribe)` → `uv run --project transcribe pytest transcribe/tests -q`; not part of `check`)

**Interfaces (the worker protocol — Tasks 3/4 depend on it verbatim):**
- `python -m nagare_transcribe vad REQUEST` — request `{"params": {"threshold": float, "min_speech": float, "min_silence": float, "speech_pad": float}, "sources": [{"path": str, "out": str}]}`; writes per source `{"duration": float, "speech": [[start, end], ...]}`.
- `python -m nagare_transcribe asr REQUEST` — request `{"model": str, "batch_size": int, "max_chunk_seconds": float, "sources": [{"path": str, "out": str, "chunks": [[start, end], ...]}]}`; writes per source `{"chunks": [[start, end], ...] (after sub-splitting), "tokens": [{"text": str, "start": float, "end": float}, ...]}`, absolute seconds, in time order. Model loaded once per process, and only if some source has chunks.
- `audio.decode_audio(path: Path) -> np.ndarray` — float32 mono 16 kHz via `ffmpeg -nostdin -v error -i PATH -ac 1 -ar 16000 -f f32le -`; `SAMPLE_RATE = 16000`.
- `vad.speech_regions(audio: np.ndarray, params: dict) -> list[tuple[float, float]]` — `silero_vad.load_silero_vad()` + `get_speech_timestamps(..., sampling_rate=16000, threshold, min_speech_duration_ms, min_silence_duration_ms, speech_pad_ms, return_seconds=True)` (params are seconds; convert to ms).
- `asr.split_long_chunks(audio, chunks, max_seconds, splitter=haas_asr.split_at_quiet_points) -> list[tuple[float, float]]` — chunks ≤ max pass through; longer ones become `splitter(audio[a:b], chunk_seconds=max_seconds)` sample ranges offset by `a`.
- `asr.tokens_from_hypothesis(h, offset: float) -> list[dict]` — from `h.timestamp["char"]`: text = `"".join(e["char"])` with `▁` and all whitespace removed, entries left empty dropped, `start/end + offset` rounded to 3 decimals.
- `asr.transcribe_chunks(model, audio, chunks, batch_size) -> list[dict]` — `model.transcribe([audio slices], batch_size=batch_size, timestamps=True, verbose=False)`, tokens of all chunks concatenated in order.

- [ ] **Step 1:** Write `transcribe/pyproject.toml` per Global Constraints (hatchling, `packages = ["src/nagare_transcribe"]`, dev group `pytest>=8`), run `uv sync --project transcribe` (network: pypi.org, files.pythonhosted.org, github.com, codeload.github.com, api.github.com, download.pytorch.org). Verify with `uv pip list --project transcribe` that torch is 2.14.0 and triton 3.8.0. Commit `transcribe/uv.lock`.
- [ ] **Step 2: Failing tests** (fake hypotheses: `types.SimpleNamespace(timestamp={"char": [...]})`; fake model records its call args and returns them; fake splitter):
  - `test_tokens_drop_empty_and_add_offset`: entries `[{"char": [""], "start": 0.0, "end": 0.08}, {"char": ["▁は"], "start": 0.96, "end": 1.28}, {"char": ["んです"], "start": 1.28, "end": 1.36}]`, offset `92.81` → `[{"text": "は", "start": 93.77, "end": 94.09}, {"text": "んです", "start": 94.09, "end": 94.17}]`.
  - `test_tokens_strip_inner_whitespace`: `["Third ", "▁Intelligence"]` in one entry → text `"ThirdIntelligence"`.
  - `test_split_long_chunks_offsets_subchunks`: chunks `[(0.0, 10.0), (12.0, 80.0)]`, max 30, splitter returning `[(0, 480000), (480000, 1088000)]` → `[(0.0, 10.0), (12.0, 42.0), (42.0, 80.0)]`; the splitter was called once, with `chunk_seconds=30`.
  - `test_transcribe_chunks_passes_timestamps_and_offsets`: fake model sees `timestamps=True`, `batch_size=8`, slice lengths match the chunks; returned token starts carry each chunk's start.
  - `test_cli_asr_source_without_chunks_skips_model` (monkeypatch the model loader to raise): source with `"chunks": []` → out file `{"chunks": [], "tokens": []}`.
  - `test_speech_regions_silence_is_empty`: `np.zeros(16000 * 3, np.float32)` → `[]` (real Silero on CPU).
- [ ] **Step 3:** Run `make test-transcribe`; expected: FAIL (module/function missing).
- [ ] **Step 4:** Implement the interfaces above; `__main__.py` uses argparse subcommands `vad`/`asr` with one positional `request`.
- [ ] **Step 5:** `make test-transcribe` → PASS; mutation-check `tokens_from_hypothesis` (drop the offset) → a test fails; revert. `make check` → PASS (ruff also covers `transcribe/`).
- [ ] **Step 6:** Commit `feat(transcribe): Haas + Silero worker subproject`.

### Task 2: WhisperX-shaped JSON from Haas tokens (main package, pure)

**Files:**
- Create: `src/nagare_clip/transcription/__init__.py`, `chunks.py`, `whisperx_json.py`
- Test: `tests/transcription/__init__.py`, `tests/transcription/test_chunks.py`, `tests/transcription/test_whisperx_json.py`

**Interfaces:**
- Consumes: the `asr` output shape (Task 1).
- Produces:
  - `chunks.pack_regions(regions: list[tuple[float, float]], max_seconds: float) -> list[tuple[float, float]]` — greedy: extend the current chunk with the next region while `region.end - chunk.start <= max_seconds`; otherwise start a new chunk at that region's start; a region longer than the limit is its own chunk.
  - `whisperx_json.build_whisperx_json(tokens: list[dict], regions: list[tuple[float, float]]) -> dict` — spec decision 8.
  - `whisperx_json.transcript_text(data: dict) -> str` — segment texts joined with `"\n"`, trailing `"\n"` when non-empty, `""` otherwise.

- [ ] **Step 1: Failing tests:**
  - `test_pack_merges_until_limit`: `[(0,5),(6,20),(21,31),(32,40)]`, 30 → `[(0,20),(21,40)]`.
  - `test_pack_long_region_alone`: `[(0,5),(6,50),(51,52)]`, 30 → `[(0,5),(6,50),(51,52)]`.
  - `test_pack_empty`: `[]` → `[]`.
  - `test_one_segment_per_region_and_char_words`: regions `[(1.0, 2.0), (5.0, 6.0)]`, tokens `[{"text":"は","start":1.0,"end":1.2}, {"text":"いい","start":1.2,"end":1.4}, {"text":"今","start":5.1,"end":5.3}]` → 2 segments; seg0 `text == "はいい"`, words `[("は",1.0,1.2),("い",1.2,1.3),("い",1.3,1.4)]`, every word `score == 1.0`; seg0 `start == 1.0`, `end == 1.4`; `word_segments` is all words in order; `language == "ja"`.
  - `test_gap_token_goes_to_nearer_region`: regions `[(1.0, 2.0), (5.0, 6.0)]`, token start `4.8` → in the second segment; token start `2.1` → in the first; nothing dropped (`"".join(seg texts) == "".join(token texts)`).
  - `test_zero_duration_token`: `{"text":"。","start":3.0,"end":3.0}` → one word `start == end == 3.0`.
  - `test_regions_without_tokens_make_no_segment` and `test_no_tokens_gives_empty_segments` (`segments == []`, `transcript_text(...) == ""`).
- [ ] **Step 2:** `uv run pytest tests/transcription -q` → FAIL (import error).
- [ ] **Step 3:** Implement. Even split: char *i* of *n* gets `start + (end-start)*i/n` to `start + (end-start)*(i+1)/n`, rounded to 3 decimals.
- [ ] **Step 4:** Tests PASS; mutate the nearest-region rule (always the earlier region) → `test_gap_token_goes_to_nearer_region` fails; revert. `make check` PASS.
- [ ] **Step 5:** Commit `feat(transcription): WhisperX-shaped JSON from Haas tokens`.

### Task 3: audio_silence on Silero VAD

**Files:**
- Create: `src/nagare_clip/audio_silence/vad.py`
- Modify: `src/nagare_clip/audio_silence/run.py`, `src/nagare_clip/config.py` (`AudioSilenceConfig`, `REMOVED_KEYS`)
- Delete: `src/nagare_clip/audio_silence/detect.py`, `tests/audio_silence/test_detect.py`
- Test: `tests/audio_silence/test_vad.py`, `tests/audio_silence/test_run.py` (rewrite), `tests/test_config.py`

**Interfaces:**
- Consumes: the `vad` output shape (Task 1).
- Produces:
  - `vad.read_vad(path: Path) -> tuple[float, list[tuple[float, float]]]` — `(duration, speech)`.
  - `vad.silences_from_speech(speech, duration: float, min_silence: float) -> list[tuple[float, float]]` — complement within `[0, duration]`, gaps shorter than `min_silence` dropped.
  - `run_audio_silence(output: Path, cfg: dict, *, vad_path: Path) -> None` — always writes the silences and `duration` via `write_cuts(..., duration=...)` (no enabled switch).
  - `AudioSilenceConfig` (no `enabled` field): `threshold: float = 0.5`, `min_speech: float = 0.25`, `min_silence: float = 0.5`, `speech_pad: float = 0.2`, each with a description; the section comment says Silero VAD v6, run before transcription, regions also define transcription chunks.
  - `REMOVED_KEYS["audio_silence"] = (frozenset({"noise", "enabled"}), "audio_silence always runs Silero VAD (delete a _cuts.txt line to keep a silence); tune audio_silence.threshold")`.

- [ ] **Step 1: Failing tests:** `test_silences_complement` (speech `[(1.0,2.0),(2.2,5.0)]`, duration 6.0, min 0.5 → `[(0.0,1.0),(5.0,6.0)]`); `test_no_speech_is_one_silence` (`[]`, 4.0 → `[(0.0,4.0)]`); `test_run_writes_silences_and_duration` (`read_media_duration(out) == 6.0`, `read_cuts(out) == [(0.0,1.0),(5.0,6.0)]`); `test_config_rejects_removed_audio_silence_keys` (`audio_silence: {noise: -30, enabled: false}` raises naming both `audio_silence.noise` and `audio_silence.enabled`).
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement; delete `detect.py` and its test; update module docstrings that mention silencedetect/Docker.
- [ ] **Step 4:** Tests PASS (mutation: drop the `min_silence` filter → `test_silences_complement` fails; revert). Leave `make check` red only where `pipeline/stages.py` still imports the removed builders — that is Task 4; commit Tasks 3+4 together if needed to keep the tree green.
- [ ] **Step 5:** Commit `feat(audio_silence): Silero VAD instead of silencedetect`.

### Task 4: Pipeline wiring, config, stage order

**Files:**
- Modify: `src/nagare_clip/pipeline/stages.py` (`STAGE_NAMES`, `STAGES`, the two adapters + `_required`s), `src/nagare_clip/pipeline/external.py`, `src/nagare_clip/config.py` (`TranscriptionConfig`, `REMOVED_KEYS`, `SentenceSplitConfig.window_segments` 20 → 200), `config.example.yml` (regenerate: `make config-example`)
- Create: `src/nagare_clip/transcription/run.py`
- Test: `tests/pipeline/test_external.py`, `tests/pipeline/test_stages.py`, `tests/transcription/test_run.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `external.build_transcribe_cmd(project_root: Path, command: str, request: Path) -> list[str]` → `["uv", "run", "--project", str(project_root / "transcribe"), "python", "-m", "nagare_transcribe", command, str(request)]`. Remove `build_transcription_cmd`, `build_silencedetect_cmd`, `effective_align_model` and their tests.
  - `transcription.run.asr_request(sources: list[tuple[Path, Path, Path]], cfg: dict) -> dict` — per `(media, vad_json, asr_out)`: chunks = `pack_regions(read_vad(vad_json)[1], cfg["transcription"]["max_chunk_seconds"])`.
  - `transcription.run.finish_transcription(asr_out: Path, vad_json: Path, out_json: Path, out_txt: Path) -> None` — `build_whisperx_json(tokens, speech regions)` → JSON (`ensure_ascii=False`, indent 2) + `transcript_text`.
  - `STAGE_NAMES` starts `["audio_silence", "transcription", "sentence_split", ...]`.
  - `_audio_silence_run`: one `vad` request for all sources (`out` = `output/audio_silence/{stem}_vad.json`, written to `output/audio_silence/vad_request.json`), then `run_audio_silence` per source. `_transcription_run`: one `asr` request (`output/transcription/asr_request.json`, outs `{stem}_haas.json`), then `finish_transcription` per source. Both via `run_command` (no Docker env).
  - `_audio_silence_required` adds `{stem}_vad.json` only when `ctx.from_index <= STAGE_NAMES.index("transcription")`.
  - `TranscriptionConfig`: `model: str = "third-intelligence/Haas-ja-v1.0"`, `batch_size: int = 8`, `max_chunk_seconds: float = 30.0`; `REMOVED_KEYS["transcription"] = (frozenset({"compute_type", "language", "align_model"}), "transcription runs Haas-ja-v1.0 (Japanese only)")`.

- [ ] **Step 1: Failing tests:** `test_build_transcribe_cmd`; `test_stage_order_audio_silence_first`; `test_audio_silence_stage_one_vad_call_for_all_sources` and `test_transcription_stage_one_asr_call_for_all_sources` (monkeypatch `run_command` to record argv and write fake outputs; assert one call, request lists every source in `ctx.sources` order, params from cfg); `test_asr_request_packs_regions`; `test_asr_request_no_speech_has_no_chunks`; `test_finish_transcription_writes_json_and_txt`; `test_from_stage_transcription_requires_vad_json`; `test_config_rejects_old_transcription_keys` (all three paths named in one error); `test_window_segments_default_200`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; regenerate `config.example.yml`; fix every existing test that assumed the old order/commands (grep `transcription`, `silencedetect`, `whisperx` under `tests/`).
- [ ] **Step 4:** `make check` PASS; mutation: swap `STAGE_NAMES` back → order test fails; revert.
- [ ] **Step 5:** Commit `feat(pipeline): audio_silence (Silero) before transcription (Haas)`.

### Task 5: Documentation

**Files:** `README.md`, `AGENTS.md`, `docs/stages/audio_silence.md`, `docs/stages/pipeline.md`, `docs/stages/sentence_split.md`, create `docs/stages/transcription.md`, `.github/workflows/ci.yml` only if `make validate` gained a step.

- [ ] **Step 1:** Update per the AGENTS.md Documentation Policy: objective list and Pipeline Overview (order `audio_silence` → `transcription`; Haas/Silero; `{stem}_vad.json`; audio_silence is always on — README's "Set `audio_silence.enabled: false`" goes), Hard Constraints (host ffmpeg exception for `transcribe/`, alongside ImageMagick; Docker still used by gap_context/publish), Project Structure (`transcribe/`, `src/nagare_clip/transcription/`), Human Editing Workflow, setup instructions in README (`uv sync --project transcribe`, NVIDIA GPU required, first run downloads the model), the window_segments change. `docs/stages/transcription.md` holds spec decisions 6–8 and the measured facts (token entries, 80 ms, empty first token, chunk-start error bounded by `speech_pad`).
- [ ] **Step 2:** `grep -rn "WhisperX\|silencedetect\|whisperx" README.md AGENTS.md docs/stages` — every remaining hit is either about the later WhisperX-hint task, the WhisperX JSON *format*, or gap_context/publish Docker.
- [ ] **Step 3:** `make check` PASS; commit `docs: Haas transcription and Silero audio_silence`.

### Task 6: Real-run verification (the user runs it — needs the GPU)

- [ ] **Step 1:** Hand the user: `uv sync --project transcribe` then, in a video directory with their config, `<repo>/scripts/run_pipeline.sh --source <a source with a WhisperX JSON in video-editor-ai/transcription/> --to-stage sentence_split`.
- [ ] **Step 2:** After they report back, check (read-only): `output/audio_silence/{stem}_cuts.txt` has a duration line; `output/transcription/{stem}.json` segments ≈ VAD regions, every segment `text == join(words)`; then `--from-stage intervals` style downstream run works unchanged (`python -m nagare_clip.intervals.check_edits` on the produced `_edits.txt`). Report findings; fix in a new task if needed.
