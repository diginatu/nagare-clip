# Whisper Hints for text_filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new, disabled-by-default `whisper` stage transcribes Haas's chunks with Whisper large-v3 (faster-whisper inside the existing whisperx image), and text_filter shows its differing readings to the LLM as hints.

**Architecture:** A standalone worker script runs in ONE `docker compose run` (script on stdin) and writes chunk texts; a pure main-package module turns them into one hint per transcript line; text_filter adds a hint block to each batch only when hints exist.

**Tech Stack:** Python, faster-whisper 1.2.0 (in `ghcr.io/jim60105/whisperx:large-v3-ja`), docker compose, difflib, pydantic config, pytest.

**Spec:** `docs/superpowers/specs/2026-11-07-whisper-hints-design.md` — read it first; decision numbers below refer to it.

## Global Constraints

- Stage order: `audio_silence, transcription, whisper, sentence_split, gap_context, summary, text_filter, director, guided_edit, intervals, blender, publish, render`.
- `whisper` config defaults: `enabled: false`, `model: "large-v3"`, `batch_size: 8`, `beam_size: 5`; language fixed `ja`.
- Transcribe call: `BatchedInferencePipeline(model).transcribe(audio, clip_timestamps=[{"start": s, "end": e}, ...], batch_size=batch_size, vad_filter=False, language="ja", beam_size=beam_size, condition_on_previous_text=False, without_timestamps=True)`; `WhisperModel(<path>, device="cuda", compute_type="float16")`; model path = first match of `/.cache/huggingface/hub/models--Systran--faster-whisper-{model}/snapshots/*/`; `HF_HUB_OFFLINE=1`.
- The worker script never imports `nagare_clip`; the main package never imports `faster_whisper`.
- No hints → text_filter's LLM messages are byte-identical to before this plan.
- No GPU in the agent sandbox: never run the real worker; the user runs Task 5.
- Follow AGENTS.md: TDD with RED shown (or a mutation check), `uv run`, `make check` green before each commit except the known sandbox failure `tests/test_apm_skill.py::test_nothing_is_deployed_at_the_repo_root`; commit on the current branch; never push; stage files by path (untracked root dotfiles are sandbox artefacts). Commit messages end with `Claude-Session: https://claude.ai/code/session_01G3G3d7aiEou84t9HzQafN8`.

## Review Focus

- **Whisper text with spaces / punctuation differences** → spaces removed in the worker; a hint that equals the line after that is `None`. Tests in Tasks 1, 2.
- **A line whose characters span two chunks** (sentence_split merged across a chunk seam) → its hint concatenates both chunks' spans in order. Test in Task 2.
- **Whisper returns nothing for a chunk** (empty text) or a source with no speech → no crash; lines get `None` or a deletion-shaped hint (`""` is a valid hint meaning "Whisper heard nothing" — render it as `(none)`). Tests in Tasks 1, 2, 3.
- **Halving retry in text_filter** → a retried sub-batch shows exactly its own lines' hints. Test in Task 3.
- **`whisper.enabled: false` with stale `{stem}_whisper.json` on disk** → no hints. Test in Task 4.

---

### Task 1: Container worker + command builder + compose

**Files:**
- Create: `src/nagare_clip/whisper/__init__.py`, `src/nagare_clip/whisper/worker.py` (standalone; stdlib + faster_whisper only, faster_whisper imported inside `main()`)
- Modify: `src/nagare_clip/pipeline/external.py`, `docker-compose.yml` (drop `./cache:/.cache`)
- Test: `tests/whisper/__init__.py`, `tests/whisper/test_worker.py`, `tests/pipeline/test_external.py`

**Interfaces:**
- `worker.group_by_chunk(chunks: list[tuple[float, float]], segments: list[tuple[float, str]]) -> list[str]` — segments are `(start, text)`; each goes to the chunk containing its start (else the nearest chunk); texts concatenated in order with all whitespace removed; one string per chunk (`""` if none).
- `worker.find_model(model: str, root: str = "/.cache/huggingface/hub") -> str` — first sorted match of the snapshot glob; `FileNotFoundError` naming the glob when absent.
- `worker.main(argv: list[str]) -> None` — `argv[0]` is the request path (spec decision 5); writes each source's output; loads the model only if some source has chunks.
- `external.build_whisper_cmd(project_root: Path, request_container_path: str) -> tuple[list[str], str]` → `([*_compose_prefix(project_root), "-T", "--entrypoint", "python", "whisperx", "-", request_container_path], <worker.py source text>)`; the caller passes the text as `run_command(..., stdin_text=...)`.

- [ ] **Step 1: Failing tests:** `test_group_by_chunk_strips_spaces` (`[(0,30),(30,60)]`, segments `[(0.5,"はい いつも"),(31.0,"水位 が")]` → `["はいいつも","水位が"]`); `test_group_by_chunk_split_long_chunk` (two segments starting in one chunk → concatenated); `test_group_by_chunk_empty_chunk` (→ `""`); `test_find_model_globs` (tmp tree) and `test_find_model_missing_raises`; `test_main_no_chunks_skips_model` (monkeypatch the model loader to raise; output `{"chunks": [], "texts": []}`); `test_worker_never_imports_nagare_clip` (AST scan of worker.py); `test_build_whisper_cmd` (argv exact; stdin text equals worker.py's source); `test_compose_has_no_cache_volume`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. In `main`, decode each source once with `faster_whisper.decode_audio(path)`, call the Global-Constraints transcribe, collect `(segment.start, segment.text)`, `group_by_chunk`, write JSON (`ensure_ascii=False`). Name the loader `load_model(model: str)` so tests can monkeypatch it.
- [ ] **Step 4:** PASS; mutation: drop whitespace removal → a test fails; revert. `make check`.
- [ ] **Step 5:** Commit `feat(whisper): faster-whisper worker for the whisperx image`.

### Task 2: Line hints (pure)

**Files:**
- Create: `src/nagare_clip/whisper/hints.py`
- Test: `tests/whisper/test_hints.py`

**Interfaces:**
- `hints.line_hints(segments: list[dict], chunks: list[tuple[float, float]], texts: list[str]) -> list[str | None]` — spec decision 6; `segments` are WhisperX-shaped (`text`, per-character `words` with `start`).
- `hints.load_line_hints(segments_json: Path, whisper_json: Path) -> list[str | None] | None` — `None` (and a logged warning) when either file is missing/unreadable or the counts don't line up.

- [ ] **Step 1: Failing tests:** `test_identical_lines_have_no_hint`; `test_substitution` (Haas line 「散乱感出してる」, Whisper 「産卵管出してる」 → that line's hint is 「産卵管出してる」, others `None`); `test_line_spanning_two_chunks`; `test_insertion_goes_to_earlier_line`; `test_whisper_empty_chunk_gives_empty_hint` (→ `""`); `test_char_outside_all_chunks_uses_nearest`; `test_load_missing_file_returns_none`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement with `difflib.SequenceMatcher(None, haas_chunk_text, whisper_text, autojunk=False).get_opcodes()` per chunk.
- [ ] **Step 4:** PASS; mutation: send insertions to the later line → `test_insertion_goes_to_earlier_line` fails; revert. `make check`.
- [ ] **Step 5:** Commit `feat(whisper): one Whisper hint per transcript line`.

### Task 3: text_filter shows the hints

**Files:**
- Modify: `src/nagare_clip/text_filter/llm_filter.py`, `src/nagare_clip/text_filter/run.py`
- Test: `tests/text_filter/test_hints.py`

**Interfaces:**
- `filter_transcript(lines, cfg, *, call_llm=None, recorder=..., stem="", hints: list[str | None] | None = None)`; `run_text_filter(..., hints: list[str | None] | None = None)`.
- `llm_filter.HINT_INSTRUCTIONS: str` — appended to the system prompt (after everything else) only when some hint is not `None`. Text: says a second recognizer's reading follows each batch for lines where it differs, that it can be wrong, that it may only be used as evidence to fix a misrecognition with `{{old->new}}` on the original line, and that its punctuation, fillers and phrasing must not be copied.
- User message with hints = `_format_batch(batch)` + `"\n\nReference (second recognizer, may be wrong):\n"` + one `f"{idx + 1}: {hint or '(none)'}"` per batch line whose hint is not `None`; no block when the batch has none.

- [ ] **Step 1: Failing tests:** `test_no_hints_messages_unchanged` (capture messages with `hints=None` and with all-`None` hints; both equal the messages captured without the parameter); `test_hint_block_lists_only_differing_lines`; `test_empty_hint_renders_none`; `test_retry_subbatch_carries_own_hints` (fake LLM omits a line on the first call to trigger the halving retry; assert the retry message's reference block names only its lines); `test_system_prompt_gets_instructions_once`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement (hints indexed by line index, carried through `_process_batch`).
- [ ] **Step 4:** PASS; mutation: always append the block header → `test_no_hints_messages_unchanged` fails; revert. `make check`.
- [ ] **Step 5:** Commit `feat(text_filter): show Whisper hints to the LLM`.

### Task 4: Stage wiring, config, docs

**Files:**
- Modify: `src/nagare_clip/pipeline/stages.py` (`STAGE_NAMES`, `STAGES`, `_whisper_run`, `_whisper_required`, `_text_filter_run`), `src/nagare_clip/config.py` (`WhisperConfig`), `config.example.yml` (`make config-example`), `README.md`, `AGENTS.md`, `docs/stages/pipeline.md`, `docs/stages/text_filter.md`, create `docs/stages/whisper.md`
- Test: `tests/pipeline/test_stages.py`, `tests/test_config.py`

**Interfaces:**
- `_whisper_run(ctx)`: disabled → print a skip line, nothing else. Enabled → write `output/whisper/whisper_request.json` (chunks from each `{stem}_haas.json`; container paths `/app/{src.relative}` and `/output/whisper/{stem}_whisper.json`), then `argv, script = build_whisper_cmd(ctx.project_root, "/output/whisper/whisper_request.json")`; `run_command(argv, env_extra=_docker_env(ctx), stdin_text=script)`.
- `_whisper_required(ctx)`: `{stem}_whisper.json` for each stem when enabled and `ctx.from_index <= STAGE_NAMES.index("text_filter")`, else `[]`.
- `_text_filter_run`: when `whisper.enabled`, `hints = load_line_hints(sentence_split/{stem}.json, whisper/{stem}_whisper.json)`; pass `hints=` (else `None`).

- [ ] **Step 1: Failing tests:** `test_stage_order_has_whisper_after_transcription`; `test_whisper_disabled_no_docker_call`; `test_whisper_stage_one_docker_call_for_all_sources` (request contents, argv, stdin script, env); `test_whisper_required_only_when_enabled`; `test_text_filter_ignores_stale_whisper_when_disabled`; `test_text_filter_passes_hints_when_enabled`; `test_whisper_config_defaults`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; regenerate `config.example.yml`; fix tests that index stages by position; update the docs (AGENTS.md overview + project structure + Runtime Quirks entry; README stage list, `--from-stage` list and a short "Whisper hints" section incl. the measured caveats; pipeline.md; text_filter.md; new whisper.md with spec decisions 2–7 and the measured facts).
- [ ] **Step 4:** `make check` PASS; mutation: ignore `whisper.enabled` in `_text_filter_run` → the stale-file test fails; revert.
- [ ] **Step 5:** Commit `feat(pipeline): whisper stage feeds text_filter hints`.

### Task 5: Real run (the user — GPU)

- [ ] Hand the user a run of `--from-stage whisper --to-stage text_filter` on the scratch project with `whisper.enabled: true` and `text_filter.use_llm: true`; then read `output/whisper/`, the text_filter LLM report and `_edits.txt` and report whether the hints fixed known errors (散乱感→産卵管) without importing Whisper's fillers/normalisation.
