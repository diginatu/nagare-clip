# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a multi-stage rough-cut pipeline:

1. WhisperX transcription in Docker
2. Audio-silence (jump-cut) detection — ffmpeg `silencedetect`, editable cut list
3. LLM sentence re-segmentation — rewrites `{stem}.json`/`{stem}.txt` into one-sentence-per-line units (disabled by default)
4. gap_context — a vision LLM snapshots+describes long silent gaps (audio_silence spans) so summary/director can see what the transcript can't (disabled by default)
5. summary — a larger LLM segments every video into line-range parts + summaries (and lists per-video misspelling-prone keywords) and writes one all-videos summary (project-wide)
6. Text editing checkpoint — copies `.txt` or runs LLM filter with `{{old->new}}` markers, optionally primed with the summary stage's summaries/keywords
7. plan — a larger LLM gives coarse, cross-video rough directions per part (project-wide); the director no longer reads them — only the starting order and `publish` do
8. plan_revise — a larger LLM applies the human's conversation to those directions as delete/add/update operations (project-wide, no call unless a turn is unanswered)
9. director — a larger LLM writes its own plan in its first turn, then proposes high-level edits (cut/timelapse/overlay/keep/edit) as a reviewable JSON op list and decides the playback order (fed the summaries; the plan's order is only its starting point)
10. guided_edit — a small LLM applies the director's ops into `_edits.txt`, deterministically verified
11. Patch application + keep-interval computation in Python (audio cuts unioned in)
12. Blender VSE auto-layout in headless mode
13. publish — title candidates, a description with chapter timestamps taken from the finished timeline, thumbnail copy and candidate stills (disabled by default)
14. render — composites one thumbnail per copy set with ImageMagick; the last stage, and the only one that never makes an LLM call

Plus three deterministic, non-stage artifacts (no LLM call):

- the **order note** (`order_note.py` → `llm_report/notes/order.md`), which states plainly whenever the finished video is not in shooting order — or that an order was rejected — because a reorder changes the shape of the finished video more than any other single decision and must never arrive unannounced (see [`docs/stages/order.md`](docs/stages/order.md));
- **`cut_report`**, which measures the finished cut after `intervals`/`blender` and writes a section into `llm_report/index.md` (see below);
- **`output/index.md`** (`index_page.py`), written by a `finally` in `pipeline/cli.py` after **every** invocation, failed ones included: one page naming the `.blend` and linking the files written for a human, each row carrying an mtime or `—` and **never explaining an absence** (an explanation is a guess by something that cannot see the run). Deliberately not a stage and no `Stage.when`/`ALWAYS` concept in the runner. Zero LLM calls, enforced the way `render`'s are (an AST import check plus a fresh-interpreter run); it honours `general.image_markup` like `publish.md`/`render.md`, and each embedded thumbnail's alt is `Set N — <that set's hook>`. See [`docs/stages/pipeline.md`](docs/stages/pipeline.md).

Final deliverable is a `.blend` project for human editing, plus a reviewable
`publish.md` of the material needed to upload it and the thumbnails `render`
composited from it.

> **Naming convention:** Stages are identified only by their **functional /
> config-section name** — there are no stage numbers anywhere. The canonical
> identifiers are: `transcription:`, `audio_silence:`, `sentence_split:`,
> `gap_context:`, `summary:`, `text_filter:`, `plan:`, `plan_revise:`, `director:`, `guided_edit:`,
> `intervals:`, `blender:`, `publish:`, `render:`.
> Package dirs (`src/nagare_clip/<name>/`), `output/<name>/` subdirs, and
> `run_pipeline.sh --from-stage`/`--to-stage` all use these same names. A new
> stage can be inserted anywhere without renumbering the others.

## Pipeline Overview

`scripts/run_pipeline.sh` is a thin shim that execs `uv run python -m nagare_clip.pipeline "$@"` — the orchestration itself lives in the Python package `src/nagare_clip/pipeline/` (see [`docs/stages/pipeline.md`](docs/stages/pipeline.md)). Use `--from-stage <name>` to skip earlier stages and reuse their outputs.

### transcription — WhisperX Transcription

Speech-to-text with word-level alignment. Runs in a single Docker container for all source files to avoid model reload overhead.

- **Inputs:** source video files (mp4/mkv/mov/avi/webm)
- **Outputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text)

### audio_silence — Audio-Silence (Jump-Cut) Detection

Runs ffmpeg `silencedetect` on the waveform inside the whisperx Docker image (mirrors the transcription stage — no host/Python ffmpeg dependency). The orchestrator captures stderr to a log file and passes it to `audio_silence.run.run_audio_silence()` as `raw_path`, which parses it into an editable `{stem}_cuts.txt`. Disabled (or no captured output) → header-only file, so the downstream union is a no-op. The cut list is read by `sentence_split` (force-split), `gap_context` (gap selection) and `intervals` (the exclude union), so deleting a line affects all three. See [`docs/stages/audio_silence.md`](docs/stages/audio_silence.md).

- **Inputs:** source video file
- **Outputs:** `{stem}_cuts.txt` (one `START - END` silent span per line; delete a line to keep that span)

### sentence_split — LLM Sentence Re-Segmentation

An LLM (config `sentence_split:`, disabled by default) rewrites a WhisperX transcript into one-sentence-per-line units per source. The LLM returns **bunsetsu-index ranges**, not text, which the stage maps back to whole-word boundaries, so word timings are preserved and output text is verbatim by construction (guarded by a before/after `concat_word_text` check). Processing is windowed with a trailing-sentence carry over each seam, and each window degrades independently on LLM failure. When `force_split` is enabled (default), long silences from `{stem}_cuts.txt` become hard sentence boundaries — but only where the WhisperX word timings corroborate the silence with a real inter-word gap. Disabled → byte-identical copy-through of `output/transcription/{stem}.{json,txt}`.

See [`docs/stages/sentence_split.md`](docs/stages/sentence_split.md) for the re-segmentation core, windowing/carry-over mechanics, and the full force-split corroboration rule.

- **Inputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text) from transcription; the audio_silence `{stem}_cuts.txt`, passed as `cuts_txt` (for force-split silences)
- **Outputs:** re-segmented `{stem}.json` + `{stem}.txt` in `output/sentence_split/`

### gap_context — Silent-Gap Visual Context

A vision LLM (config `gap_context:`, disabled by default) runs once per video, between `sentence_split` and `summary`. Silent spans of at least `gap_context.min_gap` (default 3.0s) from `{stem}_cuts.txt` are snapshotted at up to 3 timestamps each, then described by one vision-LLM call per gap. Frame extraction for the **whole stage** runs in **one** `docker compose run` (`pipeline.external.build_snapshot_batch_cmd()`), because container startup otherwise dwarfs the ffmpeg work. A cheap SSIM prefilter over every consecutive frame pair skips the vision call when the minimum score is at least `gap_context.static_ssim` (default `0.96`, `0` disables both the SSIM planning and its use). The LLM report records frame **paths**, never base64 payloads. Descriptions are whitespace-collapsed (on the LLM response and on reading the file), so neither a multi-line reply nor a hand-edit can inject a fake `N: ...` line into a downstream numbered transcript. The vision LLM marks each gap `ACTION:`/`STATIC:`; `context.anchor_gaps()` skips static gaps so they never reach the summary/director prompts. Disabled → `{"gaps": []}` no-op, no Docker calls.

- **Inputs:** the audio_silence `{stem}_cuts.txt`; the source video file; optionally the sentence_split `{stem}.json`, passed as `json_path` (for the neighbour lines given to the vision LLM — `gap_context.context_lines`, default 1 per side)
- **Outputs:** `output/gap_context/{stem}_gaps.json` (hand-editable, purely time-based, read leniently); frames under `output/gap_context/frames/{stem}/`

See [`docs/stages/gap_context.md`](docs/stages/gap_context.md) for gap selection, frame sampling, the SSIM prefilter, the vision-call contract, the `{stem}_gaps.json` contract, and how `summary`/`director` anchor and render described gaps.

### summary — Project-Wide Summaries

A larger LLM (config `summary:`, disabled by default) runs **once project-wide, between sentence_split and text_filter**. For each video it maps the numbered transcript into line-range **parts** with a one-sentence summary each, plus misspelling-prone keywords and a mandatory whole-video summary (`summarize.segment_video()`, `{"parts":[{"lines":[a,b],"summary":...}],"keywords":[...],"video_summary":"..."}` → `(parts, keywords, video_summary)`; a missing/non-string/empty `video_summary` is a hard parse failure that retries, same as a missing `parts` array), then reduces all parts into one all-videos summary (`generate_project_summary()`); `build_summary()` is the map-then-reduce entry point and also collects each video's summary into `ProjectSummary.video_summaries` (`{stem: video_summary}`). The reduce call's input is grouped per video — a `## <stem> — <video_summary>` header per video with its parts nested beneath, global 1-based part numbering preserved across the whole document (`_format_parts_doc()`). Keywords are coerced leniently — non-string/empty entries dropped, empty list on absence. Reuses `director_llm`'s transcript-formatting helpers and `llm_retry`; any failure degrades gracefully to empty parts/summary/keywords/video_summaries. `summary.json` (`{summary, parts:[{stem,lines,summary,start?,end?,silence?}], keywords:{stem:[...]}, video_summaries:{stem:"..."}}`) is human-reviewable and feeds `text_filter`/`plan`/`director`; `summary_from_dict` reads `video_summaries` leniently and stays backward-compatible with files that lack it (absent → `{}`). Each part's optional `start`/`end` (seconds, via `timing.segment_times`, from the `json_paths` passed to `run_summary`) lets `plan` render per-part duration/gap; omitted when timing is unavailable. Each part also optionally carries `silence`: seconds of the part's span the `intervals` stage would drop with no op (`intervals.keep.dropped_ranges` over the sentence_split JSON and the `cuts_paths` ranges, measured with `timing.span_silence`), set only when > 0.0 and rendered by `plan` as a speech/silence split bracket. When `gap_context` produced described gaps for a video, `run_summary` anchors them to that video's transcript lines (`gap_context.context.anchor_gaps()`) and appends a `## Silent gaps (visual context)` block (`format_gap_block()`) to that video's `segment_video()` user content — an absent/empty gaps file leaves the prompt byte-identical. Disabled → `{"summary":"","parts":[],"keywords":{},"video_summaries":{}}` no-op.

- **Inputs:** every sentence_split `{stem}.txt`, passed as `txts` (stem derived from basename); optionally the sentence_split `{stem}.json` per source, passed as `json_paths` (for part `start`/`end` times); optionally each source's gap_context `{stem}_gaps.json`, passed as `gaps_paths` (for the `## Silent gaps` block); optionally each source's audio_silence `{stem}_cuts.txt`, passed as `cuts_paths` (for per-part `silence`)
- **Outputs:** `output/summary/summary.json`

### text_filter — Text Editing Checkpoint (mandatory)

Produces `{stem}_edits.txt` for human review. When `text_filter.use_llm` is `false` (default), copies the transcription `.txt` as-is. When enabled, runs LLM filter and writes output with `{{old->new}}` markers preserved, with this video's whole-video summary, part summaries and keywords from `summary.json` (passed as `summary_json`) — merged with the constant `text_filter.keywords` list — appended to the system prompt via `text_filter/context.build_enhanced_prompt`; missing/empty/malformed `summary.json` degrades to the base prompt, byte-identical. See [`docs/stages/text_filter.md`](docs/stages/text_filter.md).

A human (never the LLM filter, which runs before them) may also add markers, each parsed independently; see [`docs/stages/intervals.md`](docs/stages/intervals.md) for how the intervals stage resolves them:

- `<keep>...</keep>` force-preserves its audio in the intervals stage. It may open on one line and close on a later one, spanning several WhisperX segments and the silences between them.
- `<speed factor="N.N">...</speed>` plays the region at that speed in the blender stage. It does **not** force-preserve audio — silence inside it is still cut, and the speed applies only to the surviving spoken parts. To keep the audio **and** speed it, nest the tags: `<keep><speed factor="N.N">…</speed></keep>`. Multi-line/nesting/authorship rules follow `<keep>`.
- `<overlay text="..." duration="N.N"/>` is a **self-closing point marker** for an on-screen TEXT strip: where it sits is where the text appears, and `duration` is seconds on the **edited** timeline. There is no wrapping form and no maximum clamp — an end time derived from a closing tag's position is exactly the bug this replaced. Attribute order is fixed (`text` then `duration`); a non-positive duration or empty text is skipped with a warning; quotes inside `text="..."` are unsupported. It does not force-preserve audio, so the intervals stage **snaps** an anchor that fell on cut footage forward to the first surviving moment of its own line (`intervals.snap_overlay_starts`) and never clips `duration`; the overlay is skipped only when its line has no surviving footage. The marker must stay on one `_edits.txt` line (line N maps to segment N), so a line break in the caption travels escaped as `\n` (`escape_overlay_text()`/`unescape_overlay_text()` in `intervals/sync_json.py`); `check_edits()` reports any embedded newline in an edit line.
- `<cut>...</cut>` is a deletion shorthand that desugars to `{{wrapped->}}` patches (`sync_json._expand_cut_tags`), so the resulting gap is cut by word-gap silence detection — meant for deletions longer than `intervals.silence_threshold`. Balance/nesting rules follow `<keep>`; don't overlap it with `<keep>/<speed>/<overlay>` on the same span.

- **Inputs:** `{stem}.txt`; `output/summary/summary.json` (as `summary_json`)
- **Outputs:** `{stem}_edits.txt`

Validate a hand-edited `_edits.txt` before resuming with `python -m nagare_clip.intervals.check_edits --edits-txt <file> --json <file>` (`src/nagare_clip/intervals/check_edits.py`). Unlike the interval stage's fail-fast `ValueError`, it collects **every** problem at once (by physical line, exit 1 if any): speech-line count vs. JSON segments, misplaced silence lines (`--silence-line-min`), `{{old->new}}` syntax, decomposition integrity, and tag balance/validity for all four markers. When every itemised check passes, a final **parity guard** runs the real `sync_text_to_json` and reports any rejection as a Problem — the checker can never bless a file the intervals stage would crash on. Pure `check_edits(edit_lines, json_data) -> list[Problem]`, never raises.

### plan — Cross-Video Rough Directions

A larger LLM (config `plan:`, disabled by default) runs once project-wide after `summary`, reading all per-part summaries plus the overall summary to emit a coarse **cross-video** direction per part (`plan_llm.generate_plan()`, `{"directions":[{"index":N,"direction":...}]}` mapped back by 1-based index). Context lines render each part's duration + gap-to-next (`N: stem [a-b] [12.4s, gap 1.5s] — summary`, gap shown only within the same video and omitted when it would render 0.0s; times come from `summary.json`); when the part carries `silence`, the bracket splits into `[12.9s speech, 62.9s silence]` — the default `plan.prompt` documents both forms (a test pins them to `timing.format_dur_gap`'s output). `format_parts_for_plan()` prints a `Video "<stem>": <video_summary>` header above each video's first part when that video has a summary. The plan vocabulary deliberately avoids the word **keep**: `keep` is a director *op* with a mechanical cost (it restores every silence in its range) and `plan.json` is fed to the director, so the prompt offers `feature`/`retain`/`emphasise` and forbids `keep` in a direction (a test asserts every occurrence of the word in the prompt is that rule, and that no example direction verb is a `director_llm.VALID_TYPES` op name). Out-of-range/malformed entries are dropped (logged); parse/LLM failure retries via `llm_retry`, then degrades to empty. Disabled → `{"directions":[]}` no-op.

- Every response carries a **`message`** — an account of the plan just made — appended to `plan_dialogue/history.md` as a `## plan` turn below the divider, followed by a `## human` heading to reply under. It is written on every run; a missing/empty message only logs a warning.
- `plan` is a **pure function of the summaries**: it reads neither its previous output nor the conversation. A `plan` run therefore invalidates what was built on the plan it replaces: it deletes `plan_revise/plan.json` (on the disabled path too) and appends a `--- plan re-ran … ---` divider to `plan_dialogue/history.md` (turns are divided, not deleted; nothing is appended when nothing was said since the last divider). **Do not re-run `plan` to apply a turn** — re-run `plan_revise`.
- A direction may carry its own **`"lines": [a, b]`** inside its part's range, and several directions may share one `index`, so `plan` can **split** a part `summary` got wrong. A range outside the part is dropped, not clamped.
- Consumers read one plan file only — `plan_revise/plan.json` when it exists, `plan/plan.json` otherwise (`pipeline.stages._effective_plan_json`: the director's starting order and `publish`) — and must not read `plan_dialogue/`. The **director does not read the directions**: it writes its own plan (see below).
- There is no plan/director divergence note any more (`plan/divergence.py` is gone): the director no longer reads the directions it would diverge from. The director stage deletes a stale `llm_report/notes/plan_divergence.md`.
- A response may carry an **`order`** (playback order as segments). It is the **director's starting order**, not the final one: the director may replace it (see below). It must cover every line of every source exactly once; any problem drops it **whole** and the pipeline falls back to shooting order. An empty order writes no `order` key at all, and a full-range segment is normalised to the whole-source form, so a present-and-identity order behaves exactly like an absent one. The prompt carries no worked reorder. See [`docs/stages/order.md`](docs/stages/order.md).

See [`docs/stages/plan.md`](docs/stages/plan.md) for the purity contract, the `message`, the split rules and who still reads the directions.

- **Inputs:** `output/summary/summary.json`; optionally `output/plan_dialogue/history.md`, passed as `history` (a divider is appended to it), and `output/plan_revise/plan.json`, passed as `revised` (deleted)
- **Outputs:** `output/plan/plan.json`; a divider plus its own `## plan` account in `output/plan_dialogue/history.md`

### plan_revise — Revising the Plan with the Human Editor

A larger LLM (config `plan_revise:`, disabled by default) runs once project-wide between `plan` and `director`. It owns the **conversation** (`plan/dialogue.py`, `output/plan_dialogue/history.md`) and expresses a revision as **operations** against the directions `plan` wrote: `{"delete":["k7f2"], "add":[{"index":21,"lines":[60,83],"direction":"…"}], "update":[{"id":"m3q8","direction":"…"}], "message":"…"}`, where `message` is a reply to the person. A direction no operation names is carried through **by the code** (`revise_llm.apply_revision`), so output size is proportional to the change. It writes `plan_revise/plan.json`, never into `plan/`, so `diff plan/plan.json plan_revise/plan.json` is exactly the human's influence.

- **It only fires when a human turn is unanswered** (`dialogue.has_unanswered_human` over the turns after the last divider): no conversation, no LLM call. A failed call answers nothing, so the next run retries; when it does not fire, an existing `plan_revise/plan.json` is left alone.
- A human appends a turn with `./scripts/plan_say.sh "…"` (or types into the file) and re-runs `--from-stage plan_revise --to-stage plan_revise`. The history is forgiving markdown: `## human`/`## plan` headings at any level, text before the first heading read as a human turn, `<!-- … -->` stripped, divider lines skipped, blank turns dropped, nothing raises. It lives in its own output subdir and deliberately **not** in the `project:` brief, which reaches more stages than intended.
- `add` carries **no insertion position** (part `index` + `lines` fully determine order); `add`/`update` reuse `plan_llm.coerce_lines`; `update` changes only the text. Unknown/ambiguous ids are dropped and logged; only invalid JSON, or a response with none of the four keys, retries. Operations are not persisted.
- Ids (`plan_revise/ids.py`) are a **hash of `(stem, lines)`** abbreviated to the shortest unique prefix (floor 4). A human never types an id.
- The input renders the current `order` and the response restates it whole; an omitted or rejected order is inherited from the plan, so `plan_revise/plan.json` always carries the effective order.

See [`docs/stages/plan_revise.md`](docs/stages/plan_revise.md) for the firing condition, the operation contract, the id rules and the invalidation behaviour in detail.

- **Inputs:** `output/plan/plan.json`; `output/summary/summary.json` (the parts document an `add` addresses); `output/plan_dialogue/history.md`, passed as `history`
- **Outputs:** `output/plan_revise/plan.json` (only when it fires); an appended `## plan` turn in `output/plan_dialogue/history.md`

### director — LLM High-Level Edit Operations (Pass A)

**One conversation over the whole video, not one call per segment.** The view is **always shooting order**: `director.display.build_display_view()` numbers every source's lines 1..N **once**, one `[k]` block per source, speech lines and silence lines together, and `DisplayView.to_source()` / `from_source()` convert between that numbering and the per-source coordinates `_director.json`, `guided_edit` and `intervals` speak — so an op crossing a source boundary is *unrepresentable* (a join-crossing range comes back `None`; `loop.apply_reply` splits a `cut`/`keep` at the join and refuses everything else with the join's line number).

**The director writes its own plan.** The first turn asks for a plan in prose (`loop.PLAN_REQUEST`: the throughline, what to cut/compress and roughly where, caption moments, the order and why, the expected runtime) as `{"plan": "…", "order"?: …}`; a first reply without `"plan"` is unusable, and ops without a `range` are refused. The plan is state (`LoopState.plan`) replaced whole by any later reply carrying `"plan"` (alone or beside ops), and every turn's `edit_state` opens with it under `preview.PLAN_HEADER`, so drift between plan and ops stays in view. `ConversationResult.plan` is written to `director/plan.md` (before a failing conversation raises; deleted at the start of a run). The turn cap gains one turn for it. See `docs/superpowers/specs/2026-09-27-director-writes-its-plan-design.md`.

**The director decides the order.** It is conversation state (`LoopState.order`, display ranges), never the view's shape, so display numbers and the cached system message never change under it. Any reply may carry `"order": [[a, b], …]` (ranges in playback order, replacing the order in force whole; `{"order": …}` alone changes nothing else). It must tile `1..N` or it is refused — the order only; the reply's ops still land. A range may start or end on a silence line (the silence plays with its range; ending on one becomes `Segment.gap_end`, spelled `"b~"`); a range of only a silence line is refused; a range over a source boundary is split there. An order whose break (`loop.order_breaks`: a range end whose next range does not start on the next line) falls inside a timelapse still standing after the reply is refused, and a new timelapse across a break of the order in force is refused; `cut`/`keep` may cross a break. The seed is the plan's resolved order (`_resolve_order(ctx, director=False)` → `loop.order_ranges`). When the order moves anything, `edit_state` adds the video as it plays: `timeline_runs()` takes the breaks as forced run ends and the runs are listed range by range, both sides of every seam quoted, captions in playback order. `ConversationResult.order` (source coordinates, normalised) is written to `director/order.json` on every run — the seed when the model sent none, a disabled director included, and before a failing conversation raises. See [`docs/stages/order.md`](docs/stages/order.md). `director.loop` owns the turn protocol: `next_request()` asks for an **approximate** range (hard boundaries are how plan ranges became op boundaries) and `apply_reply()` REPLACES every op whose START falls inside the range the reply names, so rewriting an earlier range is the same operation as a first pass; `reviewed_through` is monotonic and required, and `done` is refused while lines remain unreviewed. `run_director_conversation` **writes nothing** — it returns a `ConversationResult(ops keyed by stem, order, ok, error, reviewed_through, turns)` and the orchestrator writes every source's `{stem}_director.json` line-sorted, **before** failing on a bad ending, so the turn cap (`ceil(display lines / director.chunk_lines) * 2`) or a turn that exhausts the retry ladder leaves a usable edit behind for `--from-stage guided_edit`. `--source` does not narrow the director (logged once). `llm_report` shows one unit for the run and one attempt per turn, plus the deterministic preview that answered it. **What the director is sent, in full, is pinned in `tests/director/golden/conversation.txt`**: every message of a scripted five-turn conversation over a small real project (the planning turn with a reorder, a plan revision, a refused order, a timelapse over a silence line), then each `_director.json`, `order.json`, `plan.md` and the order in seconds. Read it to see the context; after an intended change regenerate with `UPDATE_GOLDEN=1 uv run pytest tests/director/test_conversation_golden.py` and read the diff.

**Ops.** `{stem}_director.json` is `{"ops": [{type, lines:[a,b], factor?, text?, duration?, note}]}`, `type ∈ {cut, timelapse, overlay, keep, edit}`, lines 1-based. It **never re-outputs transcript text**, avoiding whole-file-editing's format-breakage/modification failure modes. `parse_director_response()`/`ops_from_dict()` drop malformed/out-of-range ops individually (logged). Retried (config `director.max_retries`, default 2) on connection error or hard parse failure only — a valid empty `{"ops": []}` is accepted; each retry nudges temperature up via `llm_retry.cfg_for_attempt()`.

- **`speed` is off the director's menu** (`director_llm.MENU_TYPES = VALID_TYPES - {"speed"}`): repeated prompt rounds could not keep the mild 1.3–2.0 band from returning over large shares of the video, because a middle option is always cheaper to choose than a `timelapse`. `speed` stays in `VALID_TYPES` because it is the marker `guided_edit.timelapse.expand_timelapse_ops()` builds; an LLM-emitted `speed` op is dropped by `director_llm._drop_off_menu_ops()` in `try_parse_director_response()` (logged + recorded as `dropped-items`), while `ops_from_dict()` keeps one, because a human who writes it into `_director.json` means it.
- **`overlay`** must carry a positive `duration` alongside its `text`, or it is dropped; its `lines` only say where the caption appears (the first line of the range). The prompt asks for a duration proportional to reading length, and a test parses the prompt's own overlay example through `parse_director_response`. `director_llm` normalises CR/CRLF in an overlay's `text` to `\n` and drops an op whose text is blank.
- **`timelapse`** carries `lines`, a required `factor` of at least `director_llm.TIMELAPSE_MIN_FACTOR` (4.0, enforced on the LLM path and on a hand-edited `_director.json` — below it the op would smuggle an uncapped keep over talking) and an optional `text`; never a `duration`, which `guided_edit` derives. The prompt states a target on-screen runtime ("about a minute on screen") and shows **no** worked factor values: a single anchor number in a prompt has repeatedly become the director's uniform output, and making the right option harder to choose pushes the director to the easier one (a test guards the examples from creeping back). The accepted risk is factors clustering on the 4.0 floor or on the `8.0` of the JSON-shape example; the answer to that would be deriving the factor in code, not more prompt text.
- **`keep`** width **follows what is on screen**: the narrowest range covering a single silent gap (normally `[N, N+1]`), or the **whole event** in one keep when a continuous action plays out across several gaps — rescuing each gap separately chops the payoff into jump cuts. Widening a keep over **talking** stays forbidden (speech is never dropped by default, so such a keep only restores its pauses), and the prompt states that a `feature`/`retain` direction is editorial emphasis, NOT a keep request. `director.max_keep_lines` (default 8, `0` = no limit) **drops** a wider keep — dropping degrades to the default, whereas clipping would restore a silence nobody chose. The cap has no exemption (a `timelapse`'s keep is created in `guided_edit`, after the cap), runs as a post-pass in `try_parse_director_response()` (`director_llm._apply_keep_cap()`) so the drop logging stays in one place, and applies to the LLM response only — `ops_from_dict()` is deliberately uncapped. The number is stated in the system prompt at runtime via `director_llm.keep_limit_note()`, not baked into `director.prompt`.

**Timing.** Lines are annotated with duration + gap-to-next (`3: text [4.2s, gap 0.8s]`; a gap that would render 0.0s is omitted) from the video's WhisperX JSON (`json_path`, `format_numbered_transcript_timed`); it falls back to the untimed transcript if `json_path` is absent or its segment count mismatches. The prompt documents that bracket (a test pins the example to `timing.format_dur_gap`'s output) and forbids a `cut` range from overlapping any other op's range. `load_segment_transcript` prices each line's silence as **what the `intervals` stage drops from it with no op** (`director.run.source_drops()` → `intervals.keep.dropped_ranges()`, the same `compute_keep_intervals()` chain `run_intervals` runs, with the project's `intervals:` section passed as `SegmentInputs.intervals_cfg`), so speech + silence is the line's footage and the speech figure is exactly what plays at 1x. A line with long internal silence renders `[12.9s speech, 62.9s silence]`. Every figure downstream — brackets, `speech_seconds`/`line_seconds`, default runtime, the preview and `timeline_runs()` — reads that one `silences` list, so none can disagree with the render. The cut list is optional.

**Context.** When `summary` is enabled, `director.context.project_context_block()` renders the overall summary and one line per `[k]` source with its whole-video summary — facts only. The `plan` stage's directions are **not** rendered (the director writes its own plan), so no plan line range and no `SECTION_BOUNDARY_NOTE` reach the prompt; the "ranges are sections, not op boundaries" rule now lives in `DIRECTOR_PROMPT`'s Plan paragraph, about the model's own plan. `DIRECTOR_PROMPT`'s one-op-per-`[k]`-block rule states that each block was recorded as its own video, so it can open with a greeting or end with a sign-off — seeing a join is not knowing it is one; whether to cut is the model's call. A test keeps `DIRECTOR_PROMPT` below 6900 characters; trim restatements of what the rest of the assembled message already says rather than rules. Lives in `director/context.py`, not `director_llm.py`, so `summary` can import `director_llm` without a cycle. Described gaps from `gap_context` are anchored to the timed transcript (`gap_context.context.anchor_gaps()`) and rendered as indented, un-numbered `    [silent gap: description]` lines (`DisplayView.render()`); the prompt documents this rendering, asks for a `keep` over `[N, N+1]` (or the whole run in ONE keep) to rescue a gap worth watching, and forbids referencing an annotation line in an op. This needs `seg_times`; an absent/empty gaps file leaves the prompt byte-identical.

**Prompt goal.** The default `director.prompt` frames its goal as tighten-AND-stage: either the speech is worth listening to (1x; `cut` the weakest parts if it drags) or the span is manual work worth a real `timelapse` (factor 4.0+, audio deliberately sacrificed). The paragraph above the op list names no fast option for repeated SPEECH (a test asserts the word `speed` does not appear in it), since naming an op the director may not emit is how it gets emitted. It prefers `timelapse` over `cut` only where the repetition is VISIBLE WORK building toward a payoff, reserves `cut` for spans that leave the throughline, gives `overlay` a when-to-use rule (turning points, conclusions, failures, mishaps) plus a loose ~1-per-3-5-minutes density target, and reads an ordinary long timing-gap as a possible keep candidate when the speech around it announces an accident, cleanup or other event.

**The cached prefix** (`director.run.system_message()`): `director.prompt` (with the editorial brief appended by `apply_brief`), then `keep_limit_note()`, then `project_context_block()`, then `VIEW_HEADER` and `DisplayView.render()` — the WHOLE message is declared cacheable (`CACHEABLE_PREFIX_KEY`) because nothing in it varies per turn. A test asserts it is byte-identical across every turn. Each segment is headed `[k] <segment_label>`; `default runtime` (sum of `director_llm.speech_seconds()`; an untimed line makes it unknown) is reported by the preview.

**Playback preview** (`director/preview.py`, pure, no LLM): `preview_segment()` takes one segment's inputs (edit lines, `seg_times`, silences, segment-relative anchored gaps, `first_line`, the parsed ops and the parser's `drops`, optionally `elsewhere_seconds`) and states, per op, what it will play: range as sent and as clipped (with which op took the lost lines); footage seconds → on-screen seconds beside the default seconds for the same lines (what a span *buys*, not only what it costs — showing only costs has pushed this stage off timelapses before); for keep/timelapse both boundary gaps (by `timing.gap_shown`) as "outside this op — dropped" with any `[silent gap: …]` describing that gap; for any span at `UNINTELLIGIBLE_FACTOR` (2.0x, the one threshold) or faster every line inside with its bracket figure and first 24 characters, the total speech as unintelligible, and the footage split into speech / gaps between lines / silence within lines; caption seconds (`timelapse.caption_duration`) and any other caption on screen at the same time (one Blender channel); for a cut the lines and seconds removed; then **what the segment looks like in order** (`timeline_runs()`): consecutive display lines grouped into runs by their fate — `1x`, `1x+silence` (a keep), `timelapse`/`speed` with the factor and caption, `cut` — each with its on-screen seconds and line range, a run ending wherever the governing OP changes, so the island of 1x footage between two timelapses is a run of its own. The runs sum to the runtime the footer prints (a test pins it). An overlay changes no runtime and rides the run it starts in. Then the parser's drops and `default → with these ops` for the segment. Overlap resolution is **not** re-modelled: `resolve_placements()` runs the real `expand_timelapse_ops()` and `guided_edit.apply.resolve_span_ops()` — the same `span_op_order()` (cuts last) and `place_span_op()` that `apply_ops()` calls — and a table test asserts both give identical ranges. A line is quoted with its bracket figure (`director_llm.line_seconds()` / `timing.bracket_seconds()`, which fold a sub-second silence back in), while segment runtimes sum `speech_seconds()`, since the audio_silence cut removes that silence regardless. `./scripts/director_preview.sh --config <yml> [--source X] [--director-dir DIR] [--report-dir DIR]` (`python -m nagare_clip.director.preview_cli`) prints it for every segment in playback order, loading inputs with `load_segment_transcript()`; it writes nothing. Drops are not in `_director.json`, so they are recovered by re-parsing the last recorded response in the director LLM report (default `llm_report/director`). Markers already in the edit lines steer the clipping but are not played back.

- **Inputs:** every source's `{stem}_edits.txt` (from text_filter), whole, in shooting order; optionally the sentence_split `{stem}.json`, passed as `json_path` (for per-line timing), the gap_context `{stem}_gaps.json`, passed as `gaps`, the audio_silence `{stem}_cuts.txt`, passed as `cuts_txt` (for the speech/silence split), `output/summary/summary.json`, and the effective plan (`output/plan_revise/plan.json` when it exists, `output/plan/plan.json` otherwise) for its `order` only, which seeds the conversation's
- **Outputs:** `{stem}_director.json`; `director/order.json` (the playback order, hand-editable); `director/plan.md` (the director's own plan, for the human)

### guided_edit — Apply Director Ops (Pass B2)

Applies each director op into `_edits.txt`. A `timelapse` op is desugared before anything is applied (`guided_edit.timelapse.expand_timelapse_ops`, called from `run_guided_edit`): it becomes an `overlay` point op at its first line followed by `speed` and `keep` span ops over its whole range, which yields `<keep><speed factor="F"><overlay …/>` on the first line and `</speed></keep>` on the last. The caption's duration is `round((end_of_last_line − start_of_first_line) / factor, 2)` — exact, because the derived `keep` preserves the whole span. It is deliberately unclamped: all overlays share one Blender channel, so a padded caption would collide with the next consecutive timelapse's. When segment times are unavailable the span ops are still emitted and only the caption is dropped (logged). `apply_ops` reports an unexpanded `timelapse` op as unapplied rather than raising.

**Span ops** (`cut`/`speed`/`keep`) are a pure whole-line-range wrap: `apply.apply_span_op()` deterministically prepends the open tag to the first boundary line and appends the close tag to the last (both on one line for single-line ranges), **with no LLM call**. Existing markers/patches on a line are left intact (nested inside). Because the director reads tag-stripped text (and may overlap its own ops), `apply.clip_range()`/`blocked_lines()` clip a span op to the largest contiguous run of lines it may touch (ties → earliest). An `overlay` op is a **point**: `apply.apply_point_op()` prepends the self-closing marker to the first free line of its range (escaping its text), so no closing tag exists to be misplaced. An op is blocked by a **same-type** span (nesting is rejected by the intervals extractors) and by a **`<cut>`** span; a `cut` op is blocked by ANY existing tag (cut deletes whatever it wraps) and cut ops are applied **after** all other span ops so protections land first and the cut clips around them (e.g. `cut [81,96]` + `keep [96,96]` resolves to `cut [81,95]`). A clip is logged; an op whose whole range is blocked is dropped (logged and recorded in the LLM report). keep/speed/overlay never block each other, but a line already carrying an overlay marker blocks another overlay op (two captions at one point would stack on screen).

Only **`edit` ops** (a within-line `{{old->new}}` described in prose) use the small local LLM (config `guided_edit:`, disabled by default), with **one call over just the op's boundary line(s)** (wide ranges show only first/last line with an omission marker). `reconcile.verify_op()` checks that the underlying text is unaltered (`clean_old()` before/after) and that the op landed — for span ops, the **opening** tag on the first boundary line and the **closing** on the last; for an overlay, the marker on the op's own line. A failing `edit` op retries (config `guided_edit.max_retries`, default 2) with the temperature nudged up; after all retries fail (or a span op fails verification) the op is reverted and logged (recorded as `dropped-items`). Disabled → copies `_edits.txt` through with only the silence lines added. A final `check_edits` pass (the orchestrator always passes `json_path`) logs residual problems, including an embedded newline.

**Silence lines.** guided_edit first writes the director's silence lines into its `_edits.txt` (`run.with_silence_lines()`, whole source, waits ≥ `director.silence_line_min`, text from `SilenceLine.body()` — the same renderer as the director's view). Every op is mapped to physical lines (`apply.to_physical()`): a `"n~"` edge is the silence line after `n`, so every op lands as a marker. A `"n~"` with no silence line, or an `edit` op on one, is reported unapplied; a clip never leaves an edge on a silence line the op did not address; `verify_op` refuses any change to the silence lines. The director preview places ops the same way.

- **Inputs:** `{stem}_edits.txt` (text_filter), `{stem}_director.json`, `{stem}.json`, `{stem}_gaps.json` (as `gaps_path`)
- **Outputs:** augmented `{stem}_edits.txt`

### intervals — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. The audio_silence `_cuts.txt` ranges are unioned into the exclude set (via `cuts_txt`) before inversion, and `<keep>` ranges are subtracted from the excludes so the wrapped audio survives both silence sources. `<speed>` spans do **not** force-keep audio: they are written verbatim to a top-level `speed_ranges` array, and the blender stage splits keep intervals at their boundaries. `<overlay/>` markers go to a top-level `overlays` array as `{start, duration, text}` (no end time anywhere in the contract), with the start snapped once the keep intervals are final, and their text bunsetsu-spaced (reusing `caption.bunsetu_separator`) because Blender's TEXT strip only wraps at spaces. A final `merge_close_intervals` pass (`intervals.min_cut`, default 0.4s, `0` disables) absorbs any remaining gap between adjacent keep intervals shorter than the threshold — the only pass constraining gaps rather than intervals. Runs per source in-process. See [`docs/stages/intervals.md`](docs/stages/intervals.md) for the markers, margins and captions.

`intervals` is also the **single conversion point from lines to seconds**: it resolves the order (`director/order.json`, else the effective plan, else shooting order) and writes `output/intervals/timeline.json` (`{"segments": [{stem, lines?, start, end}]}`, source seconds, playback order). Each segment owns the silent gap *preceding* its first line — unless the segment before ends `"b~"`, which puts that boundary at `max(end(b), start(b+1) − keep_pre_margin)` — and an unresolvable boundary degrades the **whole** manifest to shooting order. **The authority rule:** the director is the authority on the order up to and including `intervals`; `intervals/timeline.json` is the authority after it; nothing downstream of `intervals` re-derives a time from a line number, and nothing upstream reasons in seconds. See [`docs/stages/order.md`](docs/stages/order.md).

**Silence lines** (`edit_lines.py`): every `_edits.txt` reader goes through `parse_edit_lines()` (speech lines + silence lines identified by position; the `[silent …]` body is opaque to marker/patch scans). A tag on the silence line after `n` resolves to that silence's edge (opener → start, closer → end, from the pre-sync JSON); `<cut>` deletes only the speech it wraps and drops every silence it covers. A missing, moved, duplicated or text-carrying silence line is refused; a file with none is the legacy shape. See [`docs/stages/intervals.md`](docs/stages/intervals.md#silence-lines).

- **Inputs:** `{stem}_edits.txt`, `{stem}.json` (transcription original), `{stem}_cuts.txt` (audio_silence)
- **Outputs:** `{stem}_intervals.json` (keep intervals + captions); `timeline.json` (the ordered manifest)

### blender — Blender VSE Layout

Auto-assembles the rough cut in headless Blender. References original media in-place (no re-encoding). Concatenates the **segments** of `intervals/timeline.json` onto a single timeline, in playback order (`--manifest`; absent → the per-source loop). Each segment is that source's intervals JSON sliced to its time window by `blender/frames.py::slice_intervals_data`. The pure placement helpers live in `frames.py` (no `bpy`) because `publish.timeline` and `cut_report.metrics` reuse them and must not hold a second copy of the arithmetic. Strip copies are grown by **doubling** (`allocate_pairs()`), because per-interval operator calls made placement quadratic. The cursor advances by the duration Blender **really built**, not a predicted one (a cursor one frame short shunts the next strip onto the caption/badge channels); `build_timeline_map()` must still predict, so both share `frames.retimed_frame_count()`, which rounds a half frame away from zero as Blender does. The stage records its own WARNING lines to `output/blender/blender_warnings.json` (`blender/warnings_file.py`, always written, in a `finally`) for `cut_report` to read.

**Render/output settings** (`blender.render`, empty by default) are forwarded 1:1 to `scene.render` by `render_settings.apply_render_settings()`, a dict value recursing into the sub-struct of that name (`image_settings`, `ffmpeg`), so the `.blend` opens ready to render. Unknown **key** → logged and skipped; invalid **value** → hard error. Applied after the source-derived fps/resolution and before `effective_fps` is read back; `fps` without `fps_base` resets `fps_base` to `1.0`. There is **no `blender.default_fps`**: unreadable source metadata falls back to `scene.FALLBACK_FPS`, and a wanted fps is `render.fps`. Caption/overlay/speed-mark colour keys accept an RGBA list or a **hex string** (`color: "#FFCC00"`) — the copy-paste-safe route, since Blender's Ctrl-C linearises a colour while its Hex field does not; a malformed hex string is a hard error.

See [`docs/stages/blender.md`](docs/stages/blender.md) for placement, retiming, text styling and the Blender-bug workarounds.

- **Inputs:** source video files, `{stem}_intervals.json` for each source
- **Outputs:** `{stem}_edited.blend` — ready for human editing, with `blender.render`'s settings already on the scene; `blender_warnings.json`

### cut_report — Finished-Cut Metrics + Checks (deterministic, no LLM)

**Not a stage** — a report, like the order note: no `output/cut_report/` dir, no entry in `STAGE_NAMES`. `pipeline.stages.write_cut_report(ctx)` runs at the end of the `intervals` adapter and again at the end of the `blender` one (only to pick up `blender_warnings.json`), writing `llm_report/notes/cut_report.md`, which `llm_report.rebuild_index()` inlines into `index.md`. It describes `ctx.stems`, not the whole project.

**Measurements print every run** (durations and shares, 1x vs. timelapse split, keep/strip/caption/overlay counts, gap and fragment summaries, a row per `speed_range`) — they are the per-run regression table, and a report that only speaks on failure cannot serve that purpose. **Findings print only on a breach**, each carrying the threshold it breached (`checks.KIND_ORDER`: `caption-compressed`, `caption-fast`, `timelapse-long`, `keep-gap`, `keep-fragment`, `blender-warning`). The caption check reads the **authored** density (`cut_report.caption_chars_per_sec`), not merely being inside a speed range. A timelapse's on-screen time is `kept / factor`, never `span / factor`; only the **long** side is flagged (`cut_report.timelapse_max_screen`) and there is deliberately **no floor, not even a configurable one**. Gaps are checked against `intervals.min_cut` strictly **within** one segment; a zero-length gap between touching intervals is dropped rather than reported as a sub-threshold cut. Fragments use `cut_report.min_keep_fragment`. Comparing against `project.target_duration` or the brief is deliberately out of scope. `metrics.measure()` calls `publish.timeline.build_placements()`, so the report and the concatenation cannot disagree. Nothing here can fail a run; no readable intervals, or `cut_report.enabled: false`, deletes a stale note.

See [`docs/stages/cut_report.md`](docs/stages/cut_report.md) for the thresholds, the Blender-warning capture and the failure modes in detail.

- **Inputs:** every `{stem}_intervals.json`; optionally `output/blender/blender_warnings.json`
- **Outputs:** `output/llm_report/notes/cut_report.md` (inlined into `index.md`)

### publish — Title, Description + Chapters, Thumbnail Material

A larger LLM (config `publish:`, disabled by default) runs **once project-wide after `blender`**, turning material the pipeline already holds into what a human otherwise retypes for every upload. It writes files; uploading stays manual.

- **Copy** (one call, `publish_llm.generate_publish_copy()`): several *title candidates* (`publish.temperature` defaults to `0.7` so they differ), a description *lead*, *chapter titles* keyed by the part's **original** 1-based index, and *thumbnail copy* as sets of one to three lines tagged `tag`/`hook`/`subtitle`. The call is **blind** — `PUBLISH_PROMPT` carries no colour/placement vocabulary (a test asserts it) and `_parse_thumb_set` drops any style key that arrives anyway. Hard failure retries via `llm_retry`; all attempts failing degrades to empty copy and the files are still written.
- **Timing** is deterministic and never asked of the LLM: `publish/timeline.py` reproduces the blender concatenation over the manifest's segments, in seconds, reusing `split_intervals_by_speed()` (which lives in `blender/frames.py` because `bpy` does not exist in the pipeline process — a fresh-interpreter test guards that import). `publish/chapters.py` makes YouTube's conditions hold (first entry forced to `0:00`, chapters under `publish.min_chapter_duration` dropped, non-ascending entries dropped, timestamps truncated) and always writes the list, reporting what is missing via `chapter_issues()`.
- **Frame descriptions** (`publish/describe_frames.py`, `publish.describe_frames:`, disabled by default): one vision call per candidate still, run before the copy call, without showing the director's label. Results land in `output/publish/frames.json`, keyed by a sha256 of the JPEG's bytes, so an unchanged shortlist costs zero calls and a hand-rewritten description survives.
- **Thumbnail frame candidates** come from the director's ops (`thumbs.select_candidates()`), capped by `publish.max_frames` (`cap_candidates()`), extracted in one `docker compose run` via `build_snapshot_batch_cmd`. Images are embedded per `general.image_markup` via `markdown.embed_image()`. **`publish` composites nothing** — that is `render`; `publish.json`'s `thumbnail_copy` is the hand-editable contract for the look.
- **Pairing** (`publish/pairing.py`, `publish.pairing:`, enabled by default) is a second, text-only call that picks each set's frame **by index** (resolved to a path by `apply_pairing()`) and its look in ImageMagick's vocabulary. It is separate from the copy call because frame descriptions in front of the copy call anchor it to captioning the pictures. Values are not validated here; `render` falls back per key to a preset. It uses `publish:`'s provider/model and sampling settings (`_pairing_cfg()`); its `temperature`/`max_retries`/`retry_temp_step`/`retry_temp_cap` default to `None` = **inherit**, because a hardcoded default beside an inherited model can be one that model rejects. `llm_retry.cfg_for_attempt()` bounds only a rise (`min(base + step*attempt, max(cap, base))`), so a retry never drops below the configured base, and `retry_temp_step: 0` pins the temperature.

Every input is optional — a missing `summary.json`/`plan.json`/`_director.json`/`_intervals.json` degrades only the part that needed it. Disabled → `publish.json` holds the full shape with nothing in it, no LLM or Docker call.

See [`docs/stages/publish.md`](docs/stages/publish.md) for the timeline mapping, the chapter rules, the frame descriptions, the pairing and the frame shortlist in detail.

- **Inputs:** `output/summary/summary.json`; optionally `output/plan/plan.json`, each source's `{stem}_intervals.json` (in blender's concatenation order, as `intervals_paths`), `{stem}_director.json` and the sentence_split `{stem}.json` (for the frame shortlist and the caption list)
- **Outputs:** `output/publish/publish.md` (reviewable), `output/publish/publish.json` (the hand-editable look contract), `output/publish/frames.json` (the described shortlist), `output/publish/frames/{stem}/{t}.jpg`

### render — Compositing the Thumbnails (no LLM call, ever)

The **last** stage, and the only one that never calls a model under any circumstances. It reads `output/publish/publish.json` and composites one image per copy set. `render/thumbnail.py` validates every model-authored style value against an allowlist (unknown key dropped, bad value falls back **per key** to a `preset_for()` preset); a line naming no known font slot falls back to the **first face listed in `render.fonts`** (`fallback_font()`), since ImageMagick's default face draws a CJK character as nothing at all. Every `magick` invocation is an argument list, never a shell string. Line *positions* stay code's: `layout_lines()` stacks lines by **measured** height and shrinks an over-wide line. The background is **per set** (`thumbnail_copy[i].background`, resolved by `set_background()` against the publish stage dir; any path and aspect ratio works); a set naming nothing falls back to the first shortlist candidate on disk, and a set naming a missing path is **dropped** with a warning. Every skip carries a **reason**, recorded in `render.json` and printed in `render.md` as `**Not rendered:** <reason>`.

It is a stage rather than a flag so the loop "edit `publish.json`, `--from-stage render --to-stage render`, read `render.md`" costs **zero** calls and keeps the copy being judged; there is deliberately no second path (no standalone thumbnail CLI, no project-wide background). Zero calls is a property of the **code**: nothing under `src/nagare_clip/render/` names `llm_client`/`call_llm`, guarded statically and in a fresh interpreter. See [`docs/stages/render.md`](docs/stages/render.md).

- **Inputs:** `output/publish/publish.json` (missing/unreadable → an empty contact sheet, never a traceback)
- **Outputs:** `output/render/thumbnails/set{N}.jpg`, `output/render/render.json`, `output/render/render.md`

### Human Editing Workflow

1. Run transcription–text_filter → audio_silence produces `{stem}_cuts.txt`, text_filter produces `{stem}_edits.txt`
2. Human edits `_cuts.txt` (delete/adjust silent spans) and `_edits.txt` (`{{old->new}}` patches; optional `<keep>`, `<speed factor="N.N">`, `<overlay text="..." duration="N.N"/>`)
3. Resume with `--from-stage intervals` → unions cuts, applies patches, syncs JSON, carves out `<keep>` ranges (not `<speed>`, which only annotates playback speed), computes intervals, runs Blender (with Speed Control effects for `<speed>` regions)

## Hard Constraints

- Dependency management uses uv + pyproject.toml.
- LiteLLM is the LLM transport dependency: all provider access (OpenAI/Gemini/Anthropic/Ollama) goes through `nagare_clip.llm_client.call_llm` — do not add provider-specific HTTP clients.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Route media tooling (ffmpeg) through the existing whisperx Docker image; do not add host binaries or new Python audio deps. ImageMagick (`magick`, `render/thumbnail.py`) is a deliberate, documented exception: it runs on the **host**, like the `blender` stage already does, because the whisperx image has neither ImageMagick nor CJK fonts, and font slots resolve through host fontconfig (which is what makes a CJK font slot work at all).
- Preserve the interval JSON (`intervals/` package) as the human-editable contract for the Blender stage.
- `_edits.txt` is the single human-editable record of every edit. `intervals` applies only what is in it (plus the audio_silence `_cuts.txt`, a detection result the human prunes). No stage may feed `intervals` an edit from another source; an edit the file cannot express is a format change to `_edits.txt`, not a side channel. Guarded by `tests/intervals/test_single_edit_record.py`.
- The Blender stage must reference original media; do not re-encode/copy source media.
- Commit straight to `main` by default. The exception is parallel agent work: a coordinating session may dispatch agents into separate worktrees, each on a short-lived branch, and those branches land on `main` by fast-forward once reviewed. An agent never pushes or merges on its own — the coordinator or the user does that.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  llm_retry.py                # Shared bounded-retry helpers (director/guided_edit): retry_attempts(), cfg_for_attempt()
  llm_report.py               # Structured per-call LLM report: Recorder + rebuild_index (index.md + per-call <stage>/<unit>.md)
  llm_client.py               # Unified LiteLLM transport: call_llm(messages, cfg) -> str (OpenAI/Gemini/Anthropic/Ollama)
  markdown.py                 # embed_image(): the one image embed shared by publish.md and render.md
  index_page.py               # output/index.md: the one page at the top of the output dir (no LLM)
  order.py                    # Segment/TimelineSegment: the playback order, identity, coverage contract, manifest
  order_note.py               # format_order_note(): says plainly when the order is not shooting order (no LLM)
  brief.py                    # project: editorial brief -> format_brief()/apply_brief() (summary/plan/director/text_filter prompts)
  edit_lines.py               # _edits.txt line contract: silence_body() renderer + parse_edit_lines()
  timing.py                   # Pure timing helpers: segment_times(), span_silence(), segment_silences(), format_dur_gap() (plan/director duration context)
  __main__.py                 # python -m nagare_clip support (re-aliased to the pipeline CLI)
  pipeline/                   # pipeline orchestrator (replaces bash run_pipeline.sh)
    errors.py                 # PipelineError — user-facing orchestration failure
    sources.py                # SourceMedia; discover/resolve/stage source videos for Docker
    external.py               # docker/blender command builders + run_command() — the only subprocesses
    runner.py                 # Stage dataclass, PipelineContext, resolve_window(), run_stages() (windowing/skip-validation)
    stages.py                 # STAGE_NAMES + one adapter per stage + STAGES registry (owns per-stage LLM-report recorder lifecycle)
    cli.py                    # pipeline CLI: same flags as the old bash script; CLI > YAML > defaults via get_effective_config
    __main__.py                # python -m nagare_clip.pipeline entry point
  audio_silence/              # audio_silence stage (audio-silence detection)
    detect.py                 # parse_silencedetect_output() (pure)
    cuts_file.py              # write_cuts() / read_cuts() — editable cut-list format
    run.py                    # run_audio_silence() — consumes captured ffmpeg stderr
  sentence_split/             # sentence_split stage (LLM re-segmentation)
    segment.py                # pure: windowing, char/word map, rebuild_window_segments, verbatim check
    nlp.py                    # GiNZA bunsetsu extraction (lazy import)
    llm.py                    # prompt + bunsetsu-range parse/validate + retry/degrade
    run.py                    # run_sentence_split() (copy-through when disabled)
  gap_context/                # gap_context stage: silent-gap visual context (vision LLM)
    snapshot.py                # pure: select_gaps(), frame_times(), frame_relpath()
    gaps.py                    # Gap dataclass + {stem}_gaps.json contract (to/from dict, load_gaps)
    describe.py                # one vision-LLM call per gap: base64 image parts in, plain-text description out
    context.py                 # anchor_gaps(), format_gap_block() (shared by summary/director)
    run.py                     # run_gap_context() (writes {stem}_gaps.json; no-op when disabled)
  text_filter/                # text_filter stage modules (text editing checkpoint)
    run.py                    # run_text_filter() typed entry point
    llm_filter.py             # LLM API calls, {{old->new}} patch parsing, apply_patches_to_lines()
    context.py                # build_enhanced_prompt(): summary.json context -> filter prompt
  summary/                    # summary stage (project-wide): per-part + all-videos summaries
    summarize.py              # PartSummary/ProjectSummary(+keywords), segment_video(), build_summary()
    run.py                    # run_summary() (repeated sentence_split txt/json paths -> summary.json)
  plan/                       # plan stage (project-wide): cross-video rough directions
    plan_llm.py               # PartDirection, generate_plan(), format_parts_for_plan(), plan_to/from_dict()
    dialogue.py               # plan_dialogue/history.md: turns, the divider + the plan_say CLI
    # plan_llm also owns the order: order_from_dict/format_order/coerce_order
    run.py                    # run_plan() (summary.json -> plan.json; invalidates the revision)
  plan_revise/                # plan_revise stage (project-wide): the conversation, as operations
    ids.py                    # (stem, lines) hash ids, abbreviated to the shortest unique prefix
    revise_llm.py             # ReviseOps/Revision: parse delete/add/update, apply_revision()
    run.py                    # run_plan_revise() (fires only on an unanswered human turn)
  director/                   # director stage (Pass A): high-level edit ops
    director_llm.py           # DirectorOp, parse/validate JSON ops, generate via LLM
    context.py                # project_context_block(): summary+plan -> the cached prefix, in DISPLAY numbers
    display.py                # DisplayView: the whole video under one numbering, and back (pure)
    loop.py                   # next_request()/apply_reply(): the turn protocol + the order (pure, no I/O)
    run.py                    # run_director_conversation(): load, render, run the loop, ops per source
    preview.py                # preview_segment(): what a segment's ops will play (pure, no LLM)
    preview_cli.py            # director-preview CLI over existing _director.json files (read-only)
  guided_edit/                # guided_edit stage (Pass B2): apply director ops
    apply.py                  # per-op LLM call + splice + revert-on-failure; place_span_op()/resolve_span_ops() (shared with the director preview)
    reconcile.py              # verify_op(): verbatim-safety + op-reflection checks
    timelapse.py              # expand_timelapse_ops(): one timelapse op -> keep+speed+overlay
    run.py                    # run_guided_edit() (writes augmented _edits.txt)
  intervals/                  # intervals stage modules (patch application + intervals)
    manifest.py               # build_manifest(): the order in lines -> timeline.json in seconds
    run.py                    # run_intervals() typed entry point (patch + intervals; cuts_txt)
    keep.py                   # compute_keep_intervals() (the whole keep chain) + dropped_ranges() (what the render drops; director/summary brackets)
    check_edits.py            # Standalone _edits.txt integrity checker (reports ALL problems at once)
    sync_json.py              # Sync corrected text back into WhisperX JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py                # Caption chunking
    io.py                     # Source file inference
  blender/                    # blender stage modules (Blender VSE)
    blender_cli.py            # Blender-stage CLI (separate process, runs inside Blender)
    scene.py                  # Blender scene setup
    timeline.py               # Strip and caption placement
    frames.py                 # Pure placement helpers, no bpy (clamp_frames, split_intervals_by_speed, slice_intervals_data, placement_order, ordered_sources -- shared with publish/cut_report)
    color.py                  # Hex -> RGBA for caption_style colour keys (Blender's Ctrl-C linearises; its Hex field does not)
    render_settings.py        # blender.render -> scene.render RNA, forwarded 1:1 (no bpy; recurses into image_settings/ffmpeg)
    warnings_file.py          # capture_warnings()/write_warnings(): blender_warnings.json for cut_report
  cut_report/                 # finished-cut metrics + checks (no LLM; NOT a stage)
    metrics.py                # CutMetrics/SpeedSpan/SpanStats + measure() (pure)
    checks.py                 # Finding + find_issues() + read_blender_warnings()
    report.py                 # format_cut_report() / build_cut_report(sources, cfg)
  publish/                    # publish stage (project-wide, after blender)
    timeline.py               # source seconds -> finished-timeline seconds (build_placements/first_surviving_time)
    chapters.py               # YouTube chapter rules: 0:00 anchor, 10s merge, ascending, M:SS
    thumbs.py                 # ThumbCandidate/ThumbShot: payoff moments from director ops
    describe_frames.py        # one vision call per candidate still -> frames.json, cached by content hash
    pairing.py                # the second text call: copy set -> frame index + look; index resolved to a path
    publish_llm.py            # titles/lead/chapter titles/thumbnail copy (incl. LOOK) in one call
    run.py                    # run_publish() (writes publish.json + publish.md)
  render/                     # render stage (project-wide, after publish; never calls an LLM)
    thumbnail.py              # ThumbLine/ThumbSet contract; escape/validate/layout/build magick argv; render_sets()
    run.py                    # run_render() (publish.json -> render.json + render.md)
scripts/
  run_pipeline.sh             # Shim: exec uv run python -m nagare_clip.pipeline "$@"
  plan_say.sh                 # Shim: append a human turn to plan_dialogue/history.md
  director_preview.sh         # Shim: print the director playback preview (python -m nagare_clip.director.preview_cli)
docs/
  stages/                     # Deep per-stage runtime notes (loaded on demand)
Makefile                      # Canonical dev commands (make help / check / validate / test)
.github/workflows/ci.yml      # CI: ruff lint + format check, shell syntax, pytest
tests/
  test_config.py              # Config module unit tests + example-config sync lint
  pipeline/                   # pipeline orchestrator tests: cli/external/runner/sources/stages
  audio_silence/              # audio_silence (detect / cuts_file / run) unit tests
  sentence_split/             # sentence_split unit + run() tests
  gap_context/                # gap_context (snapshot / gaps / describe / context / run) unit tests
  text_filter/                # text-editing checkpoint unit tests
  summary/                    # summary segment/build + run() tests
  plan/                       # plan generate/parse + dialogue + run() tests
  plan_revise/                # plan_revise ids / operation parse+apply / run() / loop tests
  director/                   # director op parsing/generation + context + run() tests
  guided_edit/                # guided_edit apply/reconcile + run() tests
  intervals/                  # interval-stage unit tests (incl. <keep>/<cut> markers, cuts_txt union)
  blender/                    # Blender-stage tests
  publish/                    # publish (timeline / chapters / thumbs / publish_llm / run / stage wiring) tests
  render/                     # render (thumbnail argv/layout / run / stage wiring) tests
  cut_report/                 # finished-cut metrics / checks / report / stage-wiring tests
  test_index_page.py          # output/index.md: rows, mtimes, headline, thumbnails, zero calls
```

## Configuration System

All tunable parameters are defined as typed **pydantic-settings models** in
`src/nagare_clip/config.py` (one `BaseModel` per section, composed by
`NagareClipConfig`):

- The models are the single source of truth for defaults, types, and docs.
  `DEFAULTS` is **derived** (`NagareClipConfig.model_validate({}).model_dump()`).
- `get_effective_config(config_path, cli_overrides)` merges defaults ← file ←
  CLI, then **validates**: unknown or wrongly-typed keys raise `ValidationError`
  (so a typo no longer vanishes silently). Exception: `blender.caption_style`,
  `overlay_style`, and `speed_mark` use `extra="allow"` — they are open-ended
  Blender TextStrip pass-throughs (any RNA attribute incl. `font`) — and so does
  `blender.render`, a pass-through to `scene.render` (a nested mapping recurses
  into `image_settings`/`ffmpeg`). A section whose keys are ALL commented
  examples (`blender.render`, `project`) is emitted by the example generator as
  an explicit `{}`: a bare heading followed by comments parses as `null`, which
  had made `config.example.yml` unloadable as a config.
- It still returns a plain `dict` (the "dict boundary"), so all stage `run()`
  functions and `call_llm`/`llm_retry` are unchanged.
- `config.example.yml` is **generated** from the models — run
  `make config-example`. A test (`tests/test_config.py::test_example_file_matches_generator`)
  fails if the committed file drifts from the generator output.

**Priority order (highest first):** CLI flags > YAML config file > model defaults.

The `project:` section is the project-wide **editorial brief** (audience,
purpose, target_duration, tone, story_so_far, previous_summary — all free text,
all empty by default). `nagare_clip.brief.apply_brief()` appends the rendered
brief to the system prompts of `summary` (both `prompt` and `overall_prompt`),
`plan`, `plan_revise`, `director`, `text_filter` and `publish`, called from those stages' `run.py`
so no LLM module needed a new parameter. `previous_summary` is a path to a previous
project's `summary.json`; its overall summary joins the brief (a missing/unreadable
file drops only that line). Every field empty → `apply_brief` returns the same
dict and prompts are byte-identical to a run without the section — regression-tested
per stage. `gap_context`/`sentence_split`/`guided_edit` are deliberately not briefed
(mechanical stages). See [`docs/stages/project_brief.md`](docs/stages/project_brief.md).

All LLM stages (`sentence_split`, `gap_context`, `summary`, `text_filter`, `plan`, `plan_revise`, `director`, `guided_edit`, `publish`) route through `nagare_clip.llm_client.call_llm` (LiteLLM). Each block selects its backend with a `provider` key (default `ollama_chat`); the model id sent to LiteLLM is `"<provider>/<model>"`. An empty `api_base` falls back to `http://localhost:11434` for an ollama provider, or is omitted for a cloud provider. `api_key` is forwarded when set (or use the provider's env var). `response_format: "json"` maps to a JSON-object request; `reasoning_effort` is passed to LiteLLM as `reasoning_effort` **unchanged** — the user reads the config key as "this goes straight to LiteLLM", so there is no translation and no per-provider special case (what a value means for a model is LiteLLM's contract; working around its behaviour is not this pipeline's job). Unset (`null`, the default) sends nothing, so the model runs at its own default. The old `thinking` key (which *was* translated: `false` → off, `true` → `"high"`) is **removed, not aliased**: `config._reject_removed_keys()` fails `get_effective_config` on a `thinking` key anywhere in the merged config, before validation, with a message naming every such path and saying to use `reasoning_effort` — a silent alias would hand the old values a different meaning.

Only `blender/blender_cli.py` still takes a `--config <path>` flag on its command line — it runs as a separate Blender subprocess, so the pipeline CLI (`nagare_clip.pipeline.cli`) passes its resolved `config_path` through explicitly. Every other stage receives the already-merged `cfg` dict in-process (no subprocess, no re-parsing of `--config`).

## Current Runtime Quirks

Deep per-stage implementation detail — edge cases, invariants, and Blender-bug
workarounds — lives in [`docs/stages/`](docs/stages/), loaded on demand. **Always
read the relevant file first when you need to touch a stage, and keep every
`docs/stages/` file up to date whenever you change that stage's behavior** (see
the [Documentation Policy](#documentation-policy)):

- project brief (`project:` config → summary/plan/director/text_filter/publish prompts) → [`docs/stages/project_brief.md`](docs/stages/project_brief.md)
- audio_silence → [`docs/stages/audio_silence.md`](docs/stages/audio_silence.md)
- sentence_split (re-segmentation, windowing/carry-over, force-split) → [`docs/stages/sentence_split.md`](docs/stages/sentence_split.md)
- gap_context (gap selection, frame sampling/extraction, vision-call contract, summary/director consumption) → [`docs/stages/gap_context.md`](docs/stages/gap_context.md)
- plan (purity, part splitting, who still reads the directions) → [`docs/stages/plan.md`](docs/stages/plan.md)
- plan_revise (the conversation, revision operations, ids, invalidation) → [`docs/stages/plan_revise.md`](docs/stages/plan_revise.md)
- text_filter (+ summary-stage filter context) → [`docs/stages/text_filter.md`](docs/stages/text_filter.md)
- intervals (`<keep>`/`<speed>`/`<overlay/>`/`<cut>` markers, margins, captions) → [`docs/stages/intervals.md`](docs/stages/intervals.md)
- blender (VSE layout, text styling, retiming) → [`docs/stages/blender.md`](docs/stages/blender.md)
- publish (finished-timeline mapping, chapter rules, thumbnail material) → [`docs/stages/publish.md`](docs/stages/publish.md)
- render (style validation, layout/escaping, the zero-call loop) → [`docs/stages/render.md`](docs/stages/render.md)
- cut_report (finished-cut metrics + checks, Blender-warning capture) → [`docs/stages/cut_report.md`](docs/stages/cut_report.md)
- segment order (the director's `order`, seeded by the plan's; the `intervals/timeline.json` manifest; the order note) → [`docs/stages/order.md`](docs/stages/order.md)
- pipeline orchestration (`nagare_clip.pipeline`) → [`docs/stages/pipeline.md`](docs/stages/pipeline.md)
- observability (LLM report + Langfuse tracing) → [`docs/stages/observability.md`](docs/stages/observability.md)

## Python Execution

Always use `uv run` to invoke Python tools in this repo. Examples:

```bash
uv run pytest
uv run python -m nagare_clip.pipeline --from-stage intervals --to-stage intervals
```

## Preferred Validation

Canonical developer commands live in the `Makefile` (run `make help`):

- `make check` — everything CI runs: `lint` + `format-check` + `validate` + `test`.
- `make validate` — fast structural checks: `docker compose config`, `python -m py_compile` on every module under `src/nagare_clip/`, and `bash -n scripts/run_pipeline.sh`.
- `make lint` / `make format` — ruff lint / auto-format.
- `make test` — the pytest suite.

The same steps run in CI (`.github/workflows/ci.yml`) on every push and PR.

If environment allows, also validate with a full run:

```bash
# Single source
./scripts/run_pipeline.sh --source input/<sample>.mp4
# All videos in default directory
./scripts/run_pipeline.sh
```

## Dependency Updates

Dependabot (`.github/dependabot.yml`) opens one grouped PR a week for every Python dependency (`uv.lock`) and one for the GitHub Actions; CI must pass before it is merged. CI never calls an LLM, so it cannot see a LiteLLM change that breaks a real request. When a Dependabot PR moves **litellm**, make one tiny real call per `(provider, model, reasoning_effort)` the project config uses (through `llm_client.call_llm` with that stage's config section, including one image call for the vision models) before merging. That is also the first step whenever a config switches to a newly released model: LiteLLM refuses parameters for a model it does not know yet (gpt-6-luna on 1.89.0: `UnsupportedParamsError ... reasoning_effort`), and the fix is to upgrade LiteLLM, not to work around it here.

## Documentation Policy

Before touching a stage, **read its [`docs/stages/`](docs/stages/) file first**
(and this stage's overview above) so you don't contradict an existing invariant
or Blender-bug workaround.

When behavior changes, update all of:

- `README.md` (user-facing usage)
- `plan.md` (implementation/status)
- this `AGENTS.md` (agent guardrails)
- the stage's [`docs/stages/<stage>.md`](docs/stages/) deep-dive — keep it in
  sync with the code; the concise overview lives here in `AGENTS.md`, the
  function-level detail lives in the stage doc.
