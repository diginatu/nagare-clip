# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a multi-stage rough-cut pipeline:

1. Audio-silence (jump-cut) detection — Silero VAD v6 in the `transcribe/` worker, always on and **first**; editable cut list, and its speech regions define the transcription chunks
2. Haas-ja-v1.0 transcription (`transcribe/` worker) of chunks packed from those regions, shaped into the WhisperX JSON format
3. LLM long-line splitting — splits each transcript line longer than `sentence_split.max_line_seconds` at sentence boundaries, rewriting `{stem}.json`/`{stem}.txt`; never merges, never touches a short line (disabled by default)
4. gap_context — a vision LLM snapshots+describes long silent gaps (audio_silence spans) so summary/director can see what the transcript can't (disabled by default)
5. summary — a larger LLM segments every video into line-range parts + summaries (and lists per-video misspelling-prone keywords) and writes one all-videos summary (project-wide)
6. Text editing checkpoint — copies `.txt` or runs LLM filter with `{{old->new}}` markers, optionally primed with the summary stage's summaries/keywords
7. director — a **checkpoint** with no LLM that always stops the run: the `nagare-director` agent skill (a coding agent directing with the user through deterministic tools) writes the plan, the high-level edits (cut/timelapse/overlay/keep/edit) as a reviewable JSON op list and the playback order; the run resumes with `--from-stage guided_edit`, which checks those files — see [`docs/stages/director_skill.md`](docs/stages/director_skill.md)
8. guided_edit — applies the director's ops into `_edits.txt` deterministically (no LLM), verified
9. Patch application + keep-interval computation in Python (audio cuts unioned in)
10. Blender VSE auto-layout in headless mode
11. publish — title candidates, a description with chapter timestamps taken from the finished timeline, thumbnail copy and candidate stills (disabled by default)
12. render — composites one thumbnail per copy set with ImageMagick; the last stage, and the only one that never makes an LLM call

Plus three deterministic, non-stage artifacts (no LLM call):

- the **order note** (`order_note.py` → `llm_report/notes/order.md`), which states plainly whenever the finished video is not in shooting order — or that an order was rejected — because a reorder changes the shape of the finished video more than any other single decision and must never arrive unannounced (see [`docs/stages/order.md`](docs/stages/order.md));
- **`cut_report`**, which measures the finished cut after `intervals`/`blender` and writes a section into `llm_report/index.md` (see below);
- **`output/index.md`** (`index_page.py`), written by a `finally` in `pipeline/cli.py` after **every** invocation, failed ones included: one page naming the `.blend` and linking the files written for a human, each row carrying an mtime or `—` and **never explaining an absence** (an explanation is a guess by something that cannot see the run). Deliberately not a stage and no `Stage.when`/`ALWAYS` concept in the runner. Zero LLM calls, enforced the way `render`'s are (an AST import check plus a fresh-interpreter run); it honours `general.image_markup` like `publish.md`/`render.md`, and each embedded thumbnail's alt is `Set N — <that set's hook>`. See [`docs/stages/pipeline.md`](docs/stages/pipeline.md).

Final deliverable is a `.blend` project for human editing, plus a reviewable
`publish.md` of the material needed to upload it and the thumbnails `render`
composited from it.

> **Naming convention:** Stages are identified only by their **functional /
> config-section name** — there are no stage numbers anywhere. The canonical
> identifiers are: `audio_silence:`, `transcription:`, `whisper:`, `sentence_split:`,
> `gap_context:`, `summary:`, `text_filter:`, `director:`, `guided_edit:`,
> `intervals:`, `blender:`, `publish:`, `render:`.
> Package dirs (`src/nagare_clip/<name>/`), `output/<name>/` subdirs, and
> `run_pipeline.sh --from-stage`/`--to-stage` all use these same names. A new
> stage can be inserted anywhere without renumbering the others.

## Pipeline Overview

`scripts/run_pipeline.sh` is a thin shim that execs `uv run python -m nagare_clip.pipeline "$@"` — the orchestration itself lives in the Python package `src/nagare_clip/pipeline/` (see [`docs/stages/pipeline.md`](docs/stages/pipeline.md)). Use `--from-stage <name>` to skip earlier stages and reuse their outputs.

### audio_silence — Audio-Silence (Jump-Cut) Detection

Runs Silero VAD v6 through the `transcribe/` worker subproject (`python -m nagare_transcribe vad`, one process for all sources, no Docker); the speech regions are written to `{stem}_vad.json` and `audio_silence.run.run_audio_silence()` takes their complement (gaps shorter than `audio_silence.min_silence`) as the editable `{stem}_cuts.txt`, recording the media duration. `min_silence` also sets line granularity: transcription joins VAD regions closer than it, so transcript lines break only at cuts. Always on (`audio_silence.enabled`/`.noise` are removed config keys). The cut list is read by `gap_context` (gap selection) and `intervals` (the exclude union), so deleting a line affects both. See [`docs/stages/audio_silence.md`](docs/stages/audio_silence.md).

- **Inputs:** source video file
- **Outputs:** `{stem}_vad.json`, `{stem}_cuts.txt` (one `START - END` silent span per line; delete a line to keep that span)

### transcription — Haas-ja Transcription

Runs after `audio_silence`. Haas-ja-v1.0 (third-intelligence/Haas-ASR, NeMo TDT; needs a CUDA GPU) transcribes **chunks packed from the audio_silence VAD speech regions**: `transcription.chunks.pack_regions()` merges consecutive regions greedily while the chunk spans at most `transcription.max_chunk_seconds` (30 s), every chunk starting at a region start. The `transcribe/` worker (`python -m nagare_transcribe asr`, one process for all sources, no Docker) returns raw token timings; `transcription.whisperx_json.build_whisperx_json()` shapes them into the **WhisperX JSON format** so every downstream stage is unchanged: one segment per speech span between cuts (VAD regions closer than `audio_silence.min_silence` are joined by `audio_silence.vad.join_close_regions()` — a word, 結構, was once split across two lines — so a line boundary exists exactly where a cut exists; `{stem}_vad.json` stays Silero's raw output and the chunks are packed from the joined spans), `words` one entry per non-whitespace character (intervals indexes `words` by character position), `{stem}.txt` one segment per line. WhisperX itself is no longer run. Resuming at `transcription` needs `{stem}_vad.json` on disk. See [`docs/stages/transcription.md`](docs/stages/transcription.md).

- **Inputs:** source video files (mp4/mkv/mov/avi/webm); `{stem}_vad.json` from audio_silence
- **Outputs:** `{stem}.json` (word timings, WhisperX shape), `{stem}.txt` (plain text), the worker's raw `{stem}_haas.json`

### whisper — Whisper Hints (optional)

Runs right after `transcription` (config `whisper:`, disabled by default; language fixed to `ja`). A second recognizer, faster-whisper (default `large-v3`), re-transcribes the **same chunks Haas used** (`{stem}_haas.json` → `"chunks"`) in ONE `docker compose run -T --entrypoint python whisperx - <request>` for all sources, in the existing `whisperx` image; the worker script (`src/nagare_clip/whisper/worker.py`, never importing `nagare_clip`) goes on stdin. Haas stays the transcript: `whisper.hints.load_line_hints()` aligns each line with Whisper's reading, and `text_filter` shows a differing reading to its LLM as a reference only (never truth; Whisper drops fillers and sometimes hallucinates). Disabled → no Docker call, no output, and text_filter uses no hints even if old `{stem}_whisper.json` files exist. `_text_filter_run` ignores hints whose count differs from the `.txt` line count (warning). See [`docs/stages/whisper.md`](docs/stages/whisper.md).

- **Inputs:** source video files; `{stem}_haas.json` (transcription)
- **Outputs:** `output/whisper/whisper_request.json`, `{stem}_whisper.json` (`{"chunks": [[s, e], ...], "texts": [...]}`)

### sentence_split — Split Long Lines

A transcript line is one speech span between two audio_silence cuts, and those boundaries are kept: an LLM (config `sentence_split:`, disabled by default) splits **only** a line longer than `sentence_split.max_line_seconds` (default `8.0`; `end - start` strictly greater, numeric `start`/`end` required) — one call per long line, no neighbouring-line context, unit `"{stem} line N"` in the LLM report. Lines are never merged; a short or untimed line is the same dict in the output and never sent. The LLM returns **bunsetsu-index ranges** (`{"sentences": [[a,b],...]}`, validated by `llm.parse_ranges`), not text, which `segment.rebuild_segments()` maps back to whole-word boundaries of that line's own words, so word timings are preserved and the text is verbatim (guarded by a before/after `concat_word_text` check per line). Any failure — LLM error after retries, invalid ranges, verbatim mismatch, or a single range (one sentence) — keeps the line; a line with fewer than 2 bunsetsu is not sent. GiNZA loads only when some line is long, and when no line is split the transcription files are copied through byte-identically (a speechless source's empty `.txt` stays empty). `max_line_seconds` must be `> 0`. Windowing, the carry over window seams and the force split at long silences are **removed** (`window_segments`, `force_split`, `force_split_min_silence` are in `config.REMOVED_KEYS`); the stage no longer reads `{stem}_cuts.txt`. Disabled → byte-identical copy-through of `output/transcription/{stem}.{json,txt}`.

See [`docs/stages/sentence_split.md`](docs/stages/sentence_split.md).

- **Inputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text) from transcription
- **Outputs:** `{stem}.json` + `{stem}.txt` in `output/sentence_split/` (each line untouched or replaced by its pieces)

### gap_context — Silent-Gap Visual Context

A vision LLM (config `gap_context:`, disabled by default) runs once per video, between `sentence_split` and `summary`. Silent spans of at least `gap_context.min_gap` (default 3.0s) from `{stem}_cuts.txt` are snapshotted at up to 3 timestamps each, then described by one vision-LLM call per gap. Frame extraction for the **whole stage** runs in **one** `docker compose run` (`pipeline.external.build_snapshot_batch_cmd()`), because container startup otherwise dwarfs the ffmpeg work. Its script goes to `sh -s` **on stdin** (`ShellBatch.script` → `run_command(stdin_text=…)`, compose `run -T`), never in argv — one argv string is capped at 128 KiB and an 831-frame batch overflowed it — so every ffmpeg line's `-nostdin` is load-bearing; a failed batch is a Docker-level failure (every line already ends in `|| true`) and **stops the run** with a `PipelineError` carrying the exception and the stderr tail (the same holds for `publish`'s thumbnail batch). A cheap SSIM prefilter over every consecutive frame pair skips the vision call when the minimum score is at least `gap_context.static_ssim` (default `0.96`, `0` disables both the SSIM planning and its use). The LLM report records frame **paths**, never base64 payloads. Descriptions are whitespace-collapsed (on the LLM response and on reading the file), so neither a multi-line reply nor a hand-edit can inject a fake `N: ...` line into a downstream numbered transcript. The vision LLM marks each gap `ACTION:`/`STATIC:`; `context.anchor_gaps()` skips static gaps so they never reach the summary/director prompts. Disabled → `{"gaps": []}` no-op, no Docker calls.

- **Inputs:** the audio_silence `{stem}_cuts.txt`; the source video file; optionally the sentence_split `{stem}.json`, passed as `json_path` (for the neighbour lines given to the vision LLM — `gap_context.context_lines`, default 1 per side)
- **Outputs:** `output/gap_context/{stem}_gaps.json` (hand-editable, purely time-based, read leniently); frames under `output/gap_context/frames/{stem}/`

See [`docs/stages/gap_context.md`](docs/stages/gap_context.md) for gap selection, frame sampling, the SSIM prefilter, the vision-call contract, the `{stem}_gaps.json` contract, and how `summary`/`director` anchor and render described gaps.

### summary — Project-Wide Summaries

A larger LLM (config `summary:`, disabled by default) runs **once project-wide, between sentence_split and text_filter**. For each video it maps the numbered transcript into line-range **parts** with a one-sentence summary each, plus misspelling-prone keywords and a mandatory whole-video summary (`summarize.segment_video()`, `{"parts":[{"lines":[a,b],"summary":...}],"keywords":[...],"video_summary":"..."}` → `(parts, keywords, video_summary)`; a missing/non-string/empty `video_summary` is a hard parse failure that retries, same as a missing `parts` array), then reduces all parts into one all-videos summary (`generate_project_summary()`); `build_summary()` is the map-then-reduce entry point and also collects each video's summary into `ProjectSummary.video_summaries` (`{stem: video_summary}`). The reduce call's input is grouped per video — a `## <stem> — <video_summary>` header per video with its parts nested beneath, global 1-based part numbering preserved across the whole document (`_format_parts_doc()`). Keywords are coerced leniently — non-string/empty entries dropped, empty list on absence. Reuses `director.transcript`'s transcript-formatting helpers and `llm_retry`; any failure degrades gracefully to empty parts/summary/keywords/video_summaries. `summary.json` (`{summary, parts:[{stem,lines,summary,start?,end?}], keywords:{stem:[...]}, video_summaries:{stem:"..."}}`) is human-reviewable and feeds `text_filter`/`director`/`publish`; `summary_from_dict` reads `video_summaries` leniently and stays backward-compatible with files that lack it (absent → `{}`). Each part's optional `start`/`end` (seconds, via `timing.segment_times`, from the `json_paths` passed to `run_summary`) lets `publish` place each chapter on the finished timeline; omitted when timing is unavailable. When `gap_context` produced described gaps for a video, `run_summary` anchors them to that video's transcript lines (`gap_context.context.anchor_gaps()`) and appends a `## Silent gaps (visual context)` block (`format_gap_block()`) to that video's `segment_video()` user content — an absent/empty gaps file leaves the prompt byte-identical. Disabled → `{"summary":"","parts":[],"keywords":{},"video_summaries":{}}` no-op.

- **Inputs:** every sentence_split `{stem}.txt`, passed as `txts` (stem derived from basename); optionally the sentence_split `{stem}.json` per source, passed as `json_paths` (for part `start`/`end` times); optionally each source's gap_context `{stem}_gaps.json`, passed as `gaps_paths` (for the `## Silent gaps` block)
- **Outputs:** `output/summary/summary.json`

### text_filter — Text Editing Checkpoint (mandatory)

Produces `{stem}_edits.txt` for human review. When `text_filter.use_llm` is `false` (default), copies the transcription `.txt` as-is. When enabled, runs LLM filter and writes output with `{{old->new}}` markers preserved, with this video's whole-video summary, part summaries and keywords from `summary.json` (passed as `summary_json`) — merged with the constant `text_filter.keywords` list — appended to the system prompt via `text_filter/context.build_enhanced_prompt`; missing/empty/malformed `summary.json` degrades to the base prompt, byte-identical. See [`docs/stages/text_filter.md`](docs/stages/text_filter.md).

A human (never the LLM filter, which runs before them) may also add markers, each parsed independently; see [`docs/stages/intervals.md`](docs/stages/intervals.md) for how the intervals stage resolves them:

- `<keep>...</keep>` force-preserves its audio in the intervals stage. It may open on one line and close on a later one, spanning several transcript segments and the silences between them.
- `<speed factor="N.N">...</speed>` plays the region at that speed in the blender stage. It does **not** force-preserve audio — silence inside it is still cut, and the speed applies only to the surviving spoken parts. To keep the audio **and** speed it, nest the tags: `<keep><speed factor="N.N">…</speed></keep>`. Multi-line/nesting/authorship rules follow `<keep>`.
- `<overlay text="..." duration="N.N"/>` is a **self-closing point marker** for an on-screen TEXT strip: where it sits is where the text appears, and `duration` is seconds on the **edited** timeline. There is no wrapping form and no maximum clamp — an end time derived from a closing tag's position is exactly the bug this replaced. Attribute order is fixed (`text` then `duration`); a non-positive duration or empty text is skipped with a warning; quotes inside `text="..."` are unsupported. It does not force-preserve audio, so the intervals stage **snaps** an anchor that fell on cut footage forward to the first surviving moment of its own line (`intervals.snap_overlay_starts`) and never clips `duration`; the overlay is skipped only when its line has no surviving footage. The marker must stay on one `_edits.txt` line (line N maps to segment N), so a line break in the caption travels escaped as `\n` (`escape_overlay_text()`/`unescape_overlay_text()` in `intervals/sync_json.py`); `check_edits()` reports any embedded newline in an edit line.
- `<cut>...</cut>` is a deletion shorthand that desugars to `{{wrapped->}}` patches (`sync_json._expand_cut_tags`), so the resulting gap is cut by word-gap silence detection — meant for deletions longer than `intervals.silence_threshold`. Balance/nesting rules follow `<keep>`; don't overlap it with `<keep>/<speed>/<overlay>` on the same span.

- **Inputs:** `{stem}.txt`; `output/summary/summary.json` (as `summary_json`)
- **Outputs:** `{stem}_edits.txt`

Validate a hand-edited `_edits.txt` before resuming with `python -m nagare_clip.intervals.check_edits --edits-txt <file> --json <file>` (`src/nagare_clip/intervals/check_edits.py`). Unlike the interval stage's fail-fast `ValueError`, it collects **every** problem at once (by physical line, exit 1 if any): speech-line count vs. JSON segments, misplaced silence lines (`--silence-line-min`), `{{old->new}}` syntax, decomposition integrity, and tag balance/validity for all four markers. When every itemised check passes, a final **parity guard** runs the real `sync_text_to_json` and reports any rejection as a Problem — the checker can never bless a file the intervals stage would crash on. Pure `check_edits(edit_lines, json_data) -> list[Problem]`, never raises.

### director — the checkpoint (no LLM)

**The director is the `nagare-director` agent skill** (below): a coding agent directs the cut with the user and writes `director/` — `{stem}_director.json` per source, `order.json`, `plan.md`. The LLM director (conversation loop over LiteLLM, `conversation.md`, `director_say.sh`, `DIRECTOR_PROMPT`) is **gone**, and every director LLM key fails config loading naming its path (`config.REMOVED_KEYS`).

**The stage is a checkpoint that always stops** (`pipeline.stages._director_run`, no LLM): a run that reached it re-ran the stages before it, so any director files on disk were made for the previous transcripts — continuing past them is never wanted. It raises `PipelineStop` (exit 0) saying either "no director files yet" (direct with the skill) or "made before the stages above re-ran" (revise: `dump` then `write`), then resume with `--from-stage guided_edit`. Nothing writes empty director files. **The checks run on resume** (tested): the runner's skip-validation via `_director_required` (`order.json` + every `{stem}_director.json`; `plan.md` is not required), then `_check_director_files()` at the start of guided_edit, for the whole project (`project_stems(...) or ctx.stems`) before any source — the tools' own `director.tools.project.current_state().unmapped`, not re-modelled — raising a `PipelineError` (the user asked to continue) that lists what does not map and says `dump` then `write`; on success it prints `Continuing with N op(s) (a 2, b 0); shooting order` (or `a reordered playback`) so an empty edit is visible, and writes the order note.

**The view** is **always shooting order**: `director.display.build_display_view()` numbers every source's lines 1..N **once**, one `[k]` block per source, speech lines and silence lines together, and `DisplayView.to_source()` / `from_source()` convert between that numbering and the per-source coordinates `_director.json`, `guided_edit` and `intervals` speak — so an op crossing a source boundary is *unrepresentable* (`checks.join_problem` refuses it). `DisplayLine.raw` carries a speech line's raw `_edits.txt` text (what an `edit` op's patch is placed in). Loading: `director.files.director_inputs()` and `director.load.load_segment_transcript()`, shared by the tools and the checkpoint (`tools.project.project_for(ctx, stems)`).

**Ops.** `{stem}_director.json` is `{"ops": [{type, lines:[a,b], factor?, text?, duration?, old?, new?, note}]}`, `type ∈ {cut, timelapse, overlay, keep, edit}` (plus `speed`, below), lines 1-based, `"n~"` = the silence after line `n`, `"0~"` = the leading silence before line 1 (only as a silence edge of a range starting at the source's line 1). It **never re-outputs transcript text** except an `edit` op's exact `old`/`new`. `director.ops.parse_op()` validates one op; `ops_from_dict()` (every reader: guided_edit, publish, the tools) drops a malformed one (logged).

- **`speed` is off the director's menu** (`ops.MENU_TYPES = VALID_TYPES - {"speed"}`, `off_menu_problem()`): repeated prompt rounds could not keep the mild 1.3–2.0 band from returning over large shares of the video, because a middle option is always cheaper to choose than a `timelapse`. `speed` stays in `VALID_TYPES` because it is the marker `guided_edit.timelapse.expand_timelapse_ops()` builds, and `ops_from_dict()` keeps a hand-written one.
- **`overlay`** must carry a positive `duration` alongside non-blank `text` (CR/CRLF normalised to `\n`); its `lines` only say where the caption appears (the first line of the range).
- **`timelapse`** carries `lines`, a required `factor` of at least `ops.TIMELAPSE_MIN_FACTOR` (4.0 — below it the op would smuggle an uncapped keep over talking) and an optional `text`; never a `duration`, which `guided_edit` derives. No worked factor values anywhere the director reads: a single anchor number has repeatedly become the uniform output.
- **`keep`** width follows what is on screen — ONE keep over an on-screen event, never over talking. `director.max_keep_lines` (default 8, `0` = no limit) is the cap `write` enforces (`ops.keep_cap_problem()`), counting keeps that touch as one; `ops.keep_limit_note()` is the note `view` prints. `ops_from_dict()` is deliberately uncapped (a human who writes a wide keep means it).
- **`edit`** is one exact patch on ONE speech line: `{"type": "edit", "lines": [n, n], "old": "<text as view shows it>", "new": "<replacement>"}` (`new` may be `""`). `parse_op` requires a single line, non-empty `old`, string `new`, neither holding `{{`, `}}`, `->`, `<`, `>` or a line break (`guided_edit.patch.forbidden_problem`). Single-line by decision: a patch cannot span lines and one `_edits.txt` line is one segment. The prose-`note` form is gone.

**Timing.** Lines are annotated with duration + gap-to-next (`3: text [4.2s, gap 0.8s]`; a gap that would render 0.0s is omitted) from the video's WhisperX-shaped JSON (`format_numbered_transcript_timed`, `director/transcript.py`); untimed when the JSON is absent or its segment count mismatches. `load_segment_transcript` prices each line's silence as **what the `intervals` stage drops from it with no op** (`director.load.source_drops()` → `intervals.keep.dropped_ranges()`, the same chain `run_intervals` runs, with the project's `intervals:` section), so speech + silence is the line's footage and the speech figure is exactly what plays at 1x (`[12.9s speech, 62.9s silence]`). Every figure downstream — brackets, `speech_seconds`/`line_seconds`, default runtime, the preview and `timeline_runs()` — reads that one `silences` list.

**Context.** When `summary` is enabled, `director.context.project_context_block()` renders the overall summary and one line per `[k]` source with its whole-video summary — facts only. Described gaps from `gap_context` are anchored to the timed transcript and rendered as indented, un-numbered `    [silent gap: description]` lines (`DisplayView.render()`), so an op can never reference one — unless a numbered silence line claims them. **A source with no speech** (`segments: []` in the transcript JSON) is one block of one silence line — its leading silence, `0.0` to the media end (`edit_lines.with_media_end()` from its `_cuts.txt`), carrying its gap descriptions (anchor 0, passed through though `seg_times` is empty); with no recorded media end the block has no line and renders `(no lines)`. Nothing on the view/dump/preview/write/checkpoint path assumes a source has a line.

**Playback preview** (`director/preview.py`, pure, no LLM): `preview_segment()` takes one segment's inputs (edit lines, `seg_times`, silences, segment-relative anchored gaps, `first_line`, the parsed ops and the parser's `drops`, optionally `elsewhere_seconds`) and states, per op, what it will play: range as sent and as clipped (with which op took the lost lines); footage seconds → on-screen seconds beside the default seconds for the same lines (what a span *buys*, not only what it costs — showing only costs has pushed this stage off timelapses before); for keep/timelapse both boundary gaps (by `timing.gap_shown`) as "outside this op — dropped" with any `[silent gap: …]` describing that gap; for any span at `UNINTELLIGIBLE_FACTOR` (2.0x, the one threshold) or faster every line inside with its bracket figure and first 24 characters, the total speech as unintelligible, and the footage split into speech / gaps between lines / silence within lines; caption seconds (`timelapse.caption_duration`) and any other caption on screen at the same time (one Blender channel); for a cut the lines and seconds removed; then **what the segment looks like in order** (`timeline_runs()`): consecutive display lines grouped into runs by their fate — `1x`, `1x+silence` (a keep), `timelapse`/`speed` with the factor and caption, `cut` — each with its on-screen seconds and line range, a run ending wherever the governing OP changes, so the island of 1x footage between two timelapses is a run of its own. The runs sum to the runtime the footer prints (a test pins it). An overlay changes no runtime and rides the run it starts in. Then the parser's drops and `default → with these ops` for the segment. Overlap resolution is **not** re-modelled: `resolve_placements()` runs the real `expand_timelapse_ops()` and `guided_edit.apply.resolve_span_ops()` — the same `span_op_order()` (cuts last) and `place_span_op()` that `apply_ops()` calls — and a table test asserts both give identical ranges. A line is quoted with its bracket figure (`transcript.line_seconds()` / `timing.bracket_seconds()`, which fold a sub-second silence back in), while segment runtimes sum `speech_seconds()`, since the audio_silence cut removes that silence regardless. `director_tools.sh preview` prints it (below); it writes nothing. Markers already in the edit lines steer the clipping but are not played back.

- **Inputs:** none (it only tells whether director files exist yet); the checks on resume read `director/order.json`, every `{stem}_director.json` and the same view the tools load
- **Outputs:** none; the director files are the skill's

### director skill — Directing With a Coding Agent

A general coding agent plays the director with the user via the skill `skills/.apm/skills/nagare-director/` (`SKILL.md` — the workflow, tools, edit format and enforced rules — plus `references/editorial-rules.md`, the reasoning behind each op and rule (what a keep is for, why no `speed`, why timelapse ≥ 4.0x, cut vs timelapse, order, pacing), and `references/reading-the-view.md`, how to read the tools' output), working **in the video directory** (config paths are relative to it; the same `--config` files as the pipeline, in order) and calling deterministic tools — `<nagare-clip>/scripts/director_tools.sh {view,dump,preview,transcript,write} --config <yml>` (the script keeps the CWD; the skill asks the user for the checkout path when the conversation, the project's wrapper or the environment do not give it) (`python -m nagare_clip.director.tools`, `src/nagare_clip/director/tools/`, no LLM call):

- `view [--lines A-B] [--no-context]` — the brief (`brief.format_brief`), `ops.keep_limit_note`, `project_context_block`, a source index with each source's default runtime or `unknown — <why>` (`project.runtime_status()`; never omitted silently) + the whole-video total, `load.VIEW_HEADER`, `DisplayView.render(first, last)`; when the director files on disk no longer map (`current_state().unmapped`) it says so on stderr, as `dump`/`preview` do. `dump` — `director/` as an edit in display numbering. `preview [--edit F|-]` — `edit_state()` over the files or a candidate, writes nothing, a candidate's problems → exit 1; `--summary [--min-run S]` prints `preview.edit_summary()` (per-source and whole-video runtime, the runs in playback order, captions — a screenful), and `--lines A-B`/`--source k` narrow the detail via the optional `only`/`focus` parameters of `edit_state()`/`preview_segment()`; totals, captions and problems stay whole-video. The header reads as "this edit" (`STATE_HEADER`, `PLAN_HEADER`). `transcript [--edit F|-] [--source k]` — `director.played.played_transcript()`: the edit as the viewer hears it, in playback order — every line that plays with its `edit` ops applied and its captions under it, a cut as one `✂` marker and a timelapse (or any speed ≥ `UNINTELLIGIBLE_FACTOR`) as one `⏩` marker where their lines were, a silence line only inside a keep — so the agent can read the finished video top to bottom and catch a cut that breaks the story; built on `segment_preview()`'s runs (nothing re-modelled), writes nothing, a candidate's problems → exit 1. `write --edit F|-` — all problems at once and nothing written, else every `{stem}_director.json` and `order.json` always, `plan.md` when given (through `director.files`).
- Edit file: `{"plan"?, "order"?, "ops"}` in **display numbering**, the complete state (writing it twice gives the same files): `ops` replaces every op; an absent `order` is shooting order and `order.json` is always written; the timelapse/break checks use the edit's order, never the one on disk; `dump` carries the order in force, so revising from it keeps a reorder. An absent `plan` keeps `plan.md` (required while it has none).
- **Validation** (`tools/validate.py::validate_edit`, pure, every problem at once): `ops.parse_op`, `off_menu_problem`, `keep_cap_problem`; `checks.order_problems` (via `read_order`), `join_problem` (every crossing refused), `break_problem`, `silence_ref_problem`; a `cut` may not overlap any other op; an `edit` op names a speech line and its `old` occurs **exactly once** in that line as `view` shows it (`clean_for_display`: patches applied, tags stripped) without cutting through a marker or patch (`guided_edit.patch.edit_problem`, which maps visible characters back to raw indices and refuses markup it cannot map); keeps that touch or overlap (within one source) count as ONE keep against `max_keep_lines` — the skill covers an on-screen event with ONE keep and, for one wider than the cap, asks the user to raise `max_keep_lines` or considers a `timelapse`.
- Shared code lives in public modules: `director/ops.py` (op contract + per-op checks), `director/checks.py` (order, joins, breaks), `director/transcript.py` (clean/numbered/timed transcript; `summary` reuses it), `director/load.py` (`SegmentInputs`, `load_segment_transcript`, `source_drops`, `VIEW_HEADER`, `project_context`), `director/files.py` (the director files' I/O, `director_inputs`, `missing_files`), `nagare_clip/llm_json.py` (`FENCE_RE`, `strip_code_fence`; summary/publish).
- APM: `skills/` is ONE package for the skills used while editing a video (`skills/apm.yml`, `includes: [.apm/]`, each skill under `skills/.apm/skills/<name>/`; today only `nagare-director`). It is installed into a **video directory**, never deployed in this repo (no root `apm.yml`, `.apm/`, `.claude/skills/` or `.agents/skills/`, so working on nagare-clip never triggers it): `apm install <nagare-clip checkout>/skills --target claude,codex` (recommended — skill text and scripts are the same version) or `apm install diginatu/nagare-clip/skills#main --target claude,codex`. `make apm` smoke-installs it into a temp directory, diffs the deployed copies against the source and runs `apm audit --ci` there (needs the apm CLI; not in CI); `tests/test_apm_skill.py` checks the package shape, the frontmatter, the tool commands the skill names, and that nothing is deployed at the repo root.

See [`docs/stages/director_skill.md`](docs/stages/director_skill.md).

### guided_edit — Apply Director Ops (no LLM)

First checks the director files against the current transcripts (`_check_director_files()`, above). Applies each director op into `_edits.txt`, deterministically: **no LLM call and no settings** (any `guided_edit.*` key fails config loading naming its path; an empty `guided_edit:` is accepted). No ops — an empty or missing `_director.json` — means no change; the silence lines are still written. A `timelapse` op is desugared before anything is applied (`guided_edit.timelapse.expand_timelapse_ops`, called from `run_guided_edit`): it becomes an `overlay` point op at its first line followed by `speed` and `keep` span ops over its whole range, which yields `<keep><speed factor="F"><overlay …/>` on the first line and `</speed></keep>` on the last. The caption's duration is `round((end_of_last_line − start_of_first_line) / factor, 2)` — exact, because the derived `keep` preserves the whole span. It is deliberately unclamped: all overlays share one Blender channel, so a padded caption would collide with the next consecutive timelapse's. When segment times are unavailable the span ops are still emitted and only the caption is dropped (logged). `apply_ops` reports an unexpanded `timelapse` op as unapplied rather than raising.

**Span ops** (`cut`/`speed`/`keep`) are a pure whole-line-range wrap: `apply.apply_span_op()` deterministically prepends the open tag to the first boundary line and appends the close tag to the last (both on one line for single-line ranges), **with no LLM call**. Existing markers/patches on a line are left intact (nested inside). Because the director reads tag-stripped text (and may overlap its own ops), `apply.clip_range()`/`blocked_lines()` clip a span op to the largest contiguous run of lines it may touch (ties → earliest). An `overlay` op is a **point**: `apply.apply_point_op()` prepends the self-closing marker to the first free line of its range (escaping its text), so no closing tag exists to be misplaced. An op is blocked by a **same-type** span (nesting is rejected by the intervals extractors) and by a **`<cut>`** span; a `cut` op is blocked by ANY existing tag (cut deletes whatever it wraps) and cut ops are applied **after** all other span ops so protections land first and the cut clips around them (e.g. `cut [81,96]` + `keep [96,96]` resolves to `cut [81,95]`). A clip is logged; an op whose whole range is blocked is dropped (logged). keep/speed/overlay never block each other, but a line already carrying an overlay marker blocks another overlay op (two captions at one point would stack on screen).

An **`edit` op** is spliced as its exact patch (`guided_edit/patch.py::apply_edit`): the raw characters `old` reads as are wrapped as `{{old->new}}`, refused (unapplied, logged) when `old` is absent, repeated, or would cut through a marker or patch, or the op names more than one line. `reconcile.verify_op()` checks every op: the underlying text is unaltered (`clean_old()` before/after) and the op landed — for span ops, the **opening** tag on the first boundary line and the **closing** on the last; for an overlay, the marker on the op's own line; for an edit, a change on its line. A failing op is left out and logged. A final `check_edits` pass (the orchestrator always passes `json_path`) logs residual problems, including an embedded newline.

**Silence lines.** guided_edit first writes the director's silence lines into its `_edits.txt` (`run.with_silence_lines()`, whole source, waits ≥ `director.silence_line_min`, the leading one above line 1, text from `SilenceLine.body()` — the same renderer as the director's view; it takes `cuts_txt` for a speechless source's media end, and writes an empty file — never a lone `"\n"` — when there is no line at all). Every op is mapped to physical lines (`apply.to_physical()`): a `"n~"` edge is the silence line after `n`, so every op lands as a marker. A `"n~"` with no silence line, or an `edit` op on one, is reported unapplied; a clip never leaves an edge on a silence line the op did not address; `verify_op` refuses any change to the silence lines. The director preview places ops the same way.

- **Inputs:** `{stem}_edits.txt` (text_filter), `{stem}_director.json`, `{stem}.json`, `{stem}_gaps.json` (as `gaps_path`)
- **Outputs:** augmented `{stem}_edits.txt`

### intervals — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into the transcript JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. The audio_silence `_cuts.txt` ranges are unioned into the exclude set (via `cuts_txt`) before inversion, and `<keep>` ranges are subtracted from the excludes so the wrapped audio survives both silence sources. `<speed>` spans do **not** force-keep audio: they are written verbatim to a top-level `speed_ranges` array, and the blender stage splits keep intervals at their boundaries. `<overlay/>` markers go to a top-level `overlays` array as `{start, duration, text}` (no end time anywhere in the contract), with the start snapped once the keep intervals are final, and their text bunsetsu-spaced (reusing `caption.bunsetu_separator`) because Blender's TEXT strip only wraps at spaces. A final `merge_close_intervals` pass (`intervals.min_cut`, default 0.4s, `0` disables) absorbs any remaining gap between adjacent keep intervals shorter than the threshold — the only pass constraining gaps rather than intervals. Runs per source in-process. See [`docs/stages/intervals.md`](docs/stages/intervals.md) for the markers, margins and captions.

`intervals` is also the **single conversion point from lines to seconds**: it resolves the order (`director/order.json`, else shooting order) and writes `output/intervals/timeline.json` (`{"segments": [{stem, lines?, start, end}]}`, source seconds, playback order). Each segment owns the silent gap *preceding* its first line — unless the segment before ends `"b~"`, which puts that boundary at `max(end(b), start(b+1) − keep_pre_margin)` — and an unresolvable boundary degrades the **whole** manifest to shooting order. **The authority rule:** the director is the authority on the order up to and including `intervals`; `intervals/timeline.json` is the authority after it; nothing downstream of `intervals` re-derives a time from a line number, and nothing upstream reasons in seconds. See [`docs/stages/order.md`](docs/stages/order.md).

**Silence lines** (`edit_lines.py`): every `_edits.txt` reader goes through `parse_edit_lines()` (speech lines + silence lines identified by position; the `[silent …]` body is opaque to marker/patch scans). A tag on the silence line after `n` resolves to that silence's edge (opener → start, closer → end, from the pre-sync JSON); `<cut>` deletes only the speech it wraps and drops every silence it covers. The **leading silence line** (the wait from `0.0` to line 1's first word, `edit_lines.leading_span()`, `gap_spans()` key `0`) sits above line 1 and is `"0~"`; only a source's true start has one. A source with no speech is that one line, `0.0` to the media end (`with_media_end()` from `cuts_txt`, onto the JSON's `duration`; nothing recorded → no line, never a guess), dropped by default (`compute_keep_intervals` excludes the whole source) and kept under a `<keep>`. A missing, moved, duplicated or text-carrying silence line is refused — except a missing leading one, which moves no marker and is what every older file looks like; a file with none is the legacy shape. `check_edits --cuts-txt` supplies the media end. See [`docs/stages/intervals.md`](docs/stages/intervals.md#silence-lines).

- **Inputs:** `{stem}_edits.txt`, `{stem}.json` (transcription original), `{stem}_cuts.txt` (audio_silence)
- **Outputs:** `{stem}_intervals.json` (keep intervals + captions); `timeline.json` (the ordered manifest)

### blender — Blender VSE Layout

Auto-assembles the rough cut in headless Blender. References original media in-place (no re-encoding). Concatenates the **segments** of `intervals/timeline.json` onto a single timeline, in playback order (`--manifest`; absent → the per-source loop). Each segment is that source's intervals JSON sliced to its time window by `blender/frames.py::slice_intervals_data`. The pure placement helpers live in `frames.py` (no `bpy`) because `publish.timeline` and `cut_report.metrics` reuse them and must not hold a second copy of the arithmetic. Strip copies are grown by **doubling** (`allocate_pairs()`), because per-interval operator calls made placement quadratic. The cursor advances by the duration Blender **really built**, not a predicted one (a cursor one frame short shunts the next strip onto the caption/badge channels); `build_timeline_map()` must still predict, so both share `frames.retimed_frame_count()`, which rounds a half frame away from zero as Blender does. The stage records its own WARNING lines to `output/blender/blender_warnings.json` (`blender/warnings_file.py`, always written, in a `finally`) for `cut_report` to read.

**Proxies** are built by the pipeline, not by Blender (`blender/proxy.py`, `stages._build_proxies`, before Blender starts; `blender.proxy_build`, default true while `use_proxy`): host ffmpeg writes Blender 5.2's own proxy file — `<source dir>/BL_proxy/<name>/proxy_<N>.avi`, an MP4/H.264 file despite the name, keyint 10, no B-frames, no audio, unrotated with the rotation as metadata — decoding with `blender.proxy_decode` (`cuda` NVDEC + `scale_cuda`, default, or `cpu`) and encoding with `blender.proxy_encoder` (`libx264` with Blender's own settings `veryfast`+`fastdecode`, default — measured the same file as Blender's, 1.4x faster to build; or `h264_nvenc`, a bit faster to build but ~1.5x heavier to scrub). Fresh proxies are skipped, a failing attempt retries as cpu + libx264, and a failure only warns. `test_proxy_matches_blender.py` compares a real Blender build against ours. `set_preview_proxy_size()` points the preview at `PROXY_<proxy_size>`.

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
- **Thumbnail frame candidates** come from the director's ops (`thumbs.select_candidates()`), capped by `publish.max_frames` (`cap_candidates()`), extracted in one `docker compose run` via `build_snapshot_batch_cmd`; a Docker-level failure of that batch stops the run (`PipelineError`). Images are embedded per `general.image_markup` via `markdown.embed_image()`. **`publish` composites nothing** — that is `render`; `publish.json`'s `thumbnail_copy` is the hand-editable contract for the look.
- **Pairing** (`publish/pairing.py`, `publish.pairing:`, enabled by default) is a second, text-only call that picks each set's frame **by index** (resolved to a path by `apply_pairing()`) and its look in ImageMagick's vocabulary. It is separate from the copy call because frame descriptions in front of the copy call anchor it to captioning the pictures. Values are not validated here; `render` falls back per key to a preset. It uses `publish:`'s provider/model and sampling settings (`_pairing_cfg()`); its `temperature`/`max_retries`/`retry_temp_step`/`retry_temp_cap` default to `None` = **inherit**, because a hardcoded default beside an inherited model can be one that model rejects. `llm_retry.cfg_for_attempt()` bounds only a rise (`min(base + step*attempt, max(cap, base))`), so a retry never drops below the configured base, and `retry_temp_step: 0` pins the temperature.

Every input is optional — a missing `summary.json`/`plan.md`/`_director.json`/`_intervals.json` degrades only the part that needed it. Disabled → `publish.json` holds the full shape with nothing in it, no LLM or Docker call.

See [`docs/stages/publish.md`](docs/stages/publish.md) for the timeline mapping, the chapter rules, the frame descriptions, the pairing and the frame shortlist in detail.

- **Inputs:** `output/summary/summary.json`; optionally the director's `output/director/plan.md` (as `plan`, context for the copy), each source's `{stem}_intervals.json` (in blender's concatenation order, as `intervals_paths`), `{stem}_director.json` and the sentence_split `{stem}.json` (for the frame shortlist and the caption list)
- **Outputs:** `output/publish/publish.md` (reviewable), `output/publish/publish.json` (the hand-editable look contract), `output/publish/frames.json` (the described shortlist), `output/publish/frames/{stem}/{t}.jpg`

### render — Compositing the Thumbnails (no LLM call, ever)

The **last** stage, and the only one that never calls a model under any circumstances. It reads `output/publish/publish.json` and composites one image per copy set. `render/thumbnail.py` validates every model-authored style value against an allowlist (unknown key dropped, bad value falls back **per key** to a `preset_for()` preset); a line naming no known font slot falls back to the **first face listed in `render.fonts`** (`fallback_font()`), since ImageMagick's default face draws a CJK character as nothing at all. Every `magick` invocation is an argument list, never a shell string. Line *positions* stay code's: `layout_lines()` stacks lines by **measured** height and shrinks an over-wide line. The background is **per set** (`thumbnail_copy[i].background`, resolved by `set_background()` against the publish stage dir; any path and aspect ratio works); a set naming nothing falls back to the first shortlist candidate on disk, and a set naming a missing path is **dropped** with a warning. Every skip carries a **reason**, recorded in `render.json` and printed in `render.md` as `**Not rendered:** <reason>`.

It is a stage rather than a flag so the loop "edit `publish.json`, `--from-stage render --to-stage render`, read `render.md`" costs **zero** calls and keeps the copy being judged; there is deliberately no second path (no standalone thumbnail CLI, no project-wide background). Zero calls is a property of the **code**: nothing under `src/nagare_clip/render/` names `llm_client`/`call_llm`, guarded statically and in a fresh interpreter. See [`docs/stages/render.md`](docs/stages/render.md).

- **Inputs:** `output/publish/publish.json` (missing/unreadable → an empty contact sheet, never a traceback)
- **Outputs:** `output/render/thumbnails/set{N}.jpg`, `output/render/render.json`, `output/render/render.md`

### Human Editing Workflow

1. Run audio_silence–text_filter → audio_silence produces `{stem}_vad.json` and `{stem}_cuts.txt`, transcription produces `{stem}.json`/`.txt` (Haas), text_filter produces `{stem}_edits.txt`
2. Human edits `_cuts.txt` (delete/adjust silent spans) and `_edits.txt` (`{{old->new}}` patches; optional `<keep>`, `<speed factor="N.N">`, `<overlay text="..." duration="N.N"/>`)
3. Resume with `--from-stage intervals` → unions cuts, applies patches, syncs JSON, carves out `<keep>` ranges (not `<speed>`, which only annotates playback speed), computes intervals, runs Blender (with Speed Control effects for `<speed>` regions)

## Hard Constraints

- Dependency management uses uv + pyproject.toml.
- LiteLLM is the LLM transport dependency: all provider access (OpenAI/Gemini/Anthropic/Ollama) goes through `nagare_clip.llm_client.call_llm` — do not add provider-specific HTTP clients.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Route media tooling (ffmpeg) through the existing whisperx Docker image (`gap_context` and `publish` still use it); do not add host binaries or new Python audio deps. Exception: the `transcribe/` worker subproject decodes audio with the **host** `ffmpeg` (`ffmpeg -nostdin -v error -i SRC -ac 1 -ar 16000 -f f32le -`) for Silero VAD and Haas; its heavy dependencies (torch, NeMo, haas-asr, silero-vad) live only in that uv project and the main package never imports them. The Blender proxies (`blender/proxy.py`) are a second exception: host `ffmpeg`/`ffprobe`, because they need NVDEC/NVENC (absent from the whisperx service) and must sit beside each original source. ImageMagick (`magick`, `render/thumbnail.py`) is a deliberate, documented exception: it runs on the **host**, like the `blender` stage already does, because the whisperx image has neither ImageMagick nor CJK fonts, and font slots resolve through host fontconfig (which is what makes a CJK font slot work at all).
- Preserve the interval JSON (`intervals/` package) as the human-editable contract for the Blender stage.
- `_edits.txt` is the single human-editable record of every edit. `intervals` applies only what is in it (plus the audio_silence `_cuts.txt`, a detection result the human prunes). No stage may feed `intervals` an edit from another source; an edit the file cannot express is a format change to `_edits.txt`, not a side channel. Guarded by `tests/intervals/test_single_edit_record.py`.
- The Blender stage must reference original media; do not re-encode/copy source media.
- Commit straight to `main` by default. The exception is parallel agent work: a coordinating session may dispatch agents into separate worktrees, each on a short-lived branch, and those branches land on `main` by fast-forward once reviewed. An agent never pushes or merges on its own — the coordinator or the user does that.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  llm_retry.py                # Shared bounded-retry helpers for the LLM stages: retry_attempts(), cfg_for_attempt()
  llm_report.py               # Structured per-call LLM report: Recorder + rebuild_index (index.md + per-call <stage>/<unit>.md)
  llm_client.py               # Unified LiteLLM transport: call_llm(messages, cfg) -> str (OpenAI/Gemini/Anthropic/Ollama)
  markdown.py                 # embed_image(): the one image embed shared by publish.md and render.md
  index_page.py               # output/index.md: the one page at the top of the output dir (no LLM)
  order.py                    # Segment/TimelineSegment: the playback order, identity, coverage contract, manifest
  order_note.py               # format_order_note(): says plainly when the order is not shooting order (no LLM)
  brief.py                    # project: editorial brief -> format_brief()/apply_brief() (summary/text_filter/publish prompts; the director's view)
  edit_lines.py               # _edits.txt line contract: silence_body() renderer + parse_edit_lines()
  timing.py                   # Pure timing helpers: segment_times(), span_silence(), segment_silences(), format_dur_gap() (director duration context)
  __main__.py                 # python -m nagare_clip support (re-aliased to the pipeline CLI)
  pipeline/                   # pipeline orchestrator (replaces bash run_pipeline.sh)
    errors.py                 # PipelineError — user-facing orchestration failure
    sources.py                # SourceMedia; discover/resolve/stage source videos for Docker
    external.py               # transcribe-worker/docker/blender command builders + run_command() — the only subprocesses
    runner.py                 # Stage dataclass, PipelineContext, resolve_window(), run_stages() (windowing/skip-validation)
    stages.py                 # STAGE_NAMES + one adapter per stage + STAGES registry (owns per-stage LLM-report recorder lifecycle)
    cli.py                    # pipeline CLI: same flags as the old bash script; CLI > YAML > defaults via get_effective_config
    __main__.py                # python -m nagare_clip.pipeline entry point
  audio_silence/              # audio_silence stage (Silero VAD; runs first)
    vad.py                    # read_vad(), silences_from_speech() (pure)
    cuts_file.py              # write_cuts() / read_cuts() — editable cut-list format
    run.py                    # run_audio_silence() — consumes the `{stem}_vad.json` result
  transcription/              # transcription stage (Haas-ja; main-package side, no torch)
    chunks.py                 # pack_regions(): VAD regions -> chunks of at most max_chunk_seconds (pure)
    whisperx_json.py          # build_whisperx_json()/transcript_text(): Haas tokens -> WhisperX-shaped JSON (pure)
    run.py                    # asr_request() / finish_transcription(): worker request in, {stem}.json/.txt out
  whisper/                    # whisper stage (faster-whisper hints for text_filter)
    worker.py                 # standalone worker run in the whisperx image (stdin; never imports nagare_clip)
    hints.py                  # load_line_hints()/line_hints(): one Whisper hint per transcript line (pure)
  sentence_split/             # sentence_split stage (LLM splitting of long lines)
    segment.py                # pure: char/word map, rebuild_segments, verbatim check
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
  llm_json.py                 # FENCE_RE/strip_code_fence: reading a model's fenced JSON reply (summary, publish)
  director/                   # the director's contract, view and tools (the director itself is the agent skill)
    ops.py                    # DirectorOp, parse_op/ops_from_dict/ops_to_dict, per-op checks, keep cap + note
    checks.py                 # the order (read_order/order_problems/breaks/segments) and join/break/"n~" checks (pure)
    transcript.py             # clean_for_display, numbered/timed transcript, speech/line seconds (summary reuses it)
    load.py                   # SegmentInputs, load_segment_transcript(), source_drops(), VIEW_HEADER, project_context
    files.py                  # director/ file I/O, director_inputs(), missing_files() (what a resume needs)
    context.py                # project_context_block(): the summaries -> the view
    display.py                # DisplayView: the whole video under one numbering, and back (pure)
    preview.py                # preview_segment()/edit_state(): what an edit will play (pure, no LLM)
    played.py                 # played_transcript(): the transcript as the finished video plays it (pure, no LLM)
    tools/                    # agent-skill tools (no LLM): view/dump/preview/write in display numbering
      project.py              # load_project(): the director's view off disk; current_state() of director/
      validate.py             # validate_edit(): every problem at once, via the shared director checks
      cli.py                  # python -m nagare_clip.director.tools {view,dump,preview,write}
  guided_edit/                # guided_edit stage: apply director ops (no LLM)
    apply.py                  # apply_ops(): every op deterministically, verified; place_span_op()/resolve_span_ops() (shared with the director preview)
    patch.py                  # the edit op: locate "old" as view shows it, splice {{old->new}}; edit_problem() for write
    reconcile.py              # verify_op(): verbatim-safety + op-reflection checks
    timelapse.py              # expand_timelapse_ops(): one timelapse op -> keep+speed+overlay
    run.py                    # run_guided_edit() (writes augmented _edits.txt)
  intervals/                  # intervals stage modules (patch application + intervals)
    manifest.py               # build_manifest(): the order in lines -> timeline.json in seconds
    run.py                    # run_intervals() typed entry point (patch + intervals; cuts_txt)
    keep.py                   # compute_keep_intervals() (the whole keep chain) + dropped_ranges() (what the render drops; the director's brackets)
    check_edits.py            # Standalone _edits.txt integrity checker (reports ALL problems at once)
    sync_json.py              # Sync corrected text back into the transcript JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py                # Caption chunking
    io.py                     # Source file inference
  blender/                    # blender stage modules (Blender VSE)
    blender_cli.py            # Blender-stage CLI (separate process, runs inside Blender)
    scene.py                  # Blender scene setup
    timeline.py               # Strip and caption placement
    proxy.py                  # Blender's proxy file, written by host ffmpeg (GPU) before Blender runs (no bpy)
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
transcribe/                   # separate uv subproject: Haas-ja ASR + Silero VAD worker (torch/NeMo pins); `python -m nagare_transcribe {vad,asr}`
  src/nagare_transcribe/      # audio.py (host ffmpeg decode), vad.py, asr.py, __main__.py — a thin worker, no shaping logic
  tests/                      # run with `make test-transcribe` (needs `uv sync --project transcribe`)
scripts/
  run_pipeline.sh             # Shim: exec uv run python -m nagare_clip.pipeline "$@"
  director_tools.sh           # Shim: the agent-skill director tools (python -m nagare_clip.director.tools)
skills/                       # APM package of the skills for editing a video (installed into a VIDEO dir)
  apm.yml                     # package manifest (includes: [.apm/])
  .apm/skills/nagare-director/  # the director skill: SKILL.md + references/
docs/
  stages/                     # Deep per-stage runtime notes (loaded on demand)
Makefile                      # Canonical dev commands (make help / check / validate / test)
.github/workflows/ci.yml      # CI: ruff lint + format check, shell syntax, pytest
tests/
  test_config.py              # Config module unit tests + example-config sync lint
  pipeline/                   # pipeline orchestrator tests: cli/external/runner/sources/stages
  audio_silence/              # audio_silence (vad / cuts_file / run) unit tests
  transcription/              # transcription (chunks / whisperx_json / run) unit tests
  sentence_split/             # sentence_split unit + run() tests
  gap_context/                # gap_context (snapshot / gaps / describe / context / run) unit tests
  whisper/                    # whisper hints / worker unit tests
  text_filter/                # text-editing checkpoint unit tests
  summary/                    # summary segment/build + run() tests
  director/                   # director ops/checks/view/preview + agent-skill tools tests
  guided_edit/                # guided_edit apply/patch/reconcile + run() tests
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
- `get_effective_config(config_path, cli_overrides)` merges defaults ← files ←
  CLI, then **validates**. `config_path` is `None`, one path, or a sequence
  (`--config` is repeatable): files are deep-merged left to right with
  `deep_merge` — mappings recurse, lists and scalars are replaced whole, never
  concatenated — and `_reject_removed_keys` plus validation run **once, on the
  merged result**. Relative paths inside a file resolve against the CWD, not
  the file's directory. Validation: unknown or wrongly-typed keys raise `ValidationError`
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

**Priority order (highest first):** CLI flags > later `--config` file > earlier `--config` file > model defaults.

The `project:` section is the project-wide **editorial brief** (audience,
purpose, target_duration, tone, story_so_far, previous_summary — all free text,
all empty by default). `nagare_clip.brief.apply_brief()` appends the rendered
brief to the system prompts of `summary` (both `prompt` and `overall_prompt`),
`text_filter` and `publish`, called from those stages' `run.py`
so no LLM module needed a new parameter; the director's `view` prints it
(`format_brief`). `previous_summary` is a path to a previous
project's `summary.json`; its overall summary joins the brief (a missing/unreadable
file drops only that line). Every field empty → `apply_brief` returns the same
dict and prompts are byte-identical to a run without the section — regression-tested
per stage. `gap_context`/`sentence_split` are deliberately not briefed
(mechanical stages). See [`docs/stages/project_brief.md`](docs/stages/project_brief.md).

All LLM stages (`sentence_split`, `gap_context`, `summary`, `text_filter`, `publish`) route through `nagare_clip.llm_client.call_llm` (LiteLLM). Each block selects its backend with a `provider` key (default `ollama_chat`); the model id sent to LiteLLM is `"<provider>/<model>"`. An empty `api_base` falls back to `http://localhost:11434` for an ollama provider, or is omitted for a cloud provider. `api_key` is forwarded when set (or use the provider's env var). `response_format: "json"` maps to a JSON-object request; `reasoning_effort` is passed to LiteLLM as `reasoning_effort` **unchanged** — the user reads the config key as "this goes straight to LiteLLM", so there is no translation and no per-provider special case (what a value means for a model is LiteLLM's contract; working around its behaviour is not this pipeline's job). Unset (`null`, the default) sends nothing, so the model runs at its own default. The old `thinking` key (which *was* translated: `false` → off, `true` → `"high"`) is **removed, not aliased**: `config._reject_removed_keys()` fails `get_effective_config` on a `thinking` key anywhere in the merged config, before validation, with a message naming every such path and saying to use `reasoning_effort` — a silent alias would hand the old values a different meaning. Keys of a removed feature fail the same way, every path named at once (`config.REMOVED_KEYS`): every `guided_edit.*` key (the section has no settings; an empty one is accepted), the LLM director's `director.*` keys (`enabled`, `provider`, `api_base`, `model`, `api_key`, `temperature`, `reasoning_effort`, `timeout`, `response_format`, `max_retries`, `retry_temp_step`, `retry_temp_cap`, `chunk_lines`, `pause_after_plan`, `prompt`), `audio_silence.noise`/`.enabled` (always on), `transcription.compute_type`/`.language`/`.align_model` (Haas-ja only) and `sentence_split.window_segments`/`.force_split`/`.force_split_min_silence` (the stage splits only long lines).

Only `blender/blender_cli.py` still takes a `--config <path>` flag on its command line — it runs as a separate Blender subprocess. It is given exactly **one** file: `output/effective_config.yml`, the snapshot `pipeline.cli` writes after merging (`config.write_effective_config()`: the validated cfg, CLI overrides included, `api_key` values blanked, a header listing the source files). `PipelineContext.config_path` holds that snapshot's path, never a user file, so Blender sees what the pipeline validated however many `--config` files built it. The other `--config` reader (`director_tools.sh` via `director/tools/cli.py`) accepts `--config` repeatedly with the same merge. Every other stage receives the already-merged `cfg` dict in-process (no subprocess, no re-parsing of `--config`).

## Current Runtime Quirks

Deep per-stage implementation detail — edge cases, invariants, and Blender-bug
workarounds — lives in [`docs/stages/`](docs/stages/), loaded on demand. **Always
read the relevant file first when you need to touch a stage, and keep every
`docs/stages/` file up to date whenever you change that stage's behavior** (see
the [Documentation Policy](#documentation-policy)):

- project brief (`project:` config → summary/text_filter/publish prompts, the director's `view`) → [`docs/stages/project_brief.md`](docs/stages/project_brief.md)
- audio_silence → [`docs/stages/audio_silence.md`](docs/stages/audio_silence.md)
- whisper (second recognizer, hints for text_filter) → [`docs/stages/whisper.md`](docs/stages/whisper.md)
- transcription (Haas chunking, token timestamps, the WhisperX-shaped JSON) → [`docs/stages/transcription.md`](docs/stages/transcription.md)
- sentence_split (splitting lines longer than `max_line_seconds`) → [`docs/stages/sentence_split.md`](docs/stages/sentence_split.md)
- gap_context (gap selection, frame sampling/extraction, vision-call contract, summary/director consumption) → [`docs/stages/gap_context.md`](docs/stages/gap_context.md)
- text_filter (+ summary-stage filter context) → [`docs/stages/text_filter.md`](docs/stages/text_filter.md)
- intervals (`<keep>`/`<speed>`/`<overlay/>`/`<cut>` markers, margins, captions) → [`docs/stages/intervals.md`](docs/stages/intervals.md)
- blender (VSE layout, text styling, retiming) → [`docs/stages/blender.md`](docs/stages/blender.md)
- publish (finished-timeline mapping, chapter rules, thumbnail material) → [`docs/stages/publish.md`](docs/stages/publish.md)
- render (style validation, layout/escaping, the zero-call loop) → [`docs/stages/render.md`](docs/stages/render.md)
- cut_report (finished-cut metrics + checks, Blender-warning capture) → [`docs/stages/cut_report.md`](docs/stages/cut_report.md)
- segment order (the director's `order`; the `intervals/timeline.json` manifest; the order note) → [`docs/stages/order.md`](docs/stages/order.md)
- director skill (agent-run director, its tools, the `edit` op and the APM package); the director checkpoint → pipeline.md → [`docs/stages/director_skill.md`](docs/stages/director_skill.md)
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
- `make validate` — fast structural checks: `docker compose config`, `python -m py_compile` on every module under `src/nagare_clip/`, and `bash -n` on every `scripts/*.sh` (one file at a time: `bash -n a b` checks only `a`).
- `make lint` / `make format` — ruff lint / auto-format.
- `make apm` — smoke-install the `skills/` package into a temp video directory, diff the deployed copies against the source and run `apm audit --ci` there (needs the apm CLI; not part of CI).
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
- this `AGENTS.md` (agent guardrails)
- the stage's [`docs/stages/<stage>.md`](docs/stages/) deep-dive — keep it in
  sync with the code; the concise overview lives here in `AGENTS.md`, the
  function-level detail lives in the stage doc.
