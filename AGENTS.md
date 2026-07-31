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
7. plan — a larger LLM gives coarse, cross-video rough directions per part (project-wide)
8. director — a larger LLM proposes high-level edits (cut/speed/overlay/keep/edit) as a reviewable JSON op list (fed the summary/plan overview context)
9. guided_edit — a small LLM applies the director's ops into `_edits.txt`, deterministically verified
10. Patch application + keep-interval computation in Python (audio cuts unioned in)
11. Blender VSE auto-layout in headless mode

Final deliverable is a `.blend` project for human editing.

> **Naming convention:** Stages are identified only by their **functional /
> config-section name** — there are no stage numbers anywhere. The canonical
> identifiers are: `transcription:`, `audio_silence:`, `sentence_split:`,
> `gap_context:`, `summary:`, `text_filter:`, `plan:`, `director:`, `guided_edit:`,
> `intervals:`, `blender:`.
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

Runs ffmpeg `silencedetect` on the waveform inside the whisperx Docker image (mirrors the transcription stage — no host/Python ffmpeg dependency). The pipeline orchestrator captures stderr to a log file and passes it to `audio_silence.run.run_audio_silence()` as `raw_path`, which parses it into an editable `{stem}_cuts.txt`. Disabled (or no captured output) → header-only file, so the downstream union is a no-op.

- **Inputs:** source video file
- **Outputs:** `{stem}_cuts.txt` (one `START - END` silent span per line; delete a line to keep that span)

### sentence_split — LLM Sentence Re-Segmentation

An LLM (config `sentence_split:`, disabled by default) rewrites a WhisperX transcript into one-sentence-per-line units per source. Rather than emitting text, the LLM returns **bunsetsu-index ranges** (`{"sentences":[[a,b],…]}`) that the stage maps back to whole-word boundaries and reassembles from the original word list, so word timings are preserved and output text is verbatim by construction (guarded by a `concat_word_text` before/after check). Processing is windowed (`window_segments`, default 20 per LLM call) with a trailing-sentence carry over each window seam; each window degrades independently on LLM failure. When `force_split` is enabled (default), long silences from the audio_silence `{stem}_cuts.txt` (spans ≥ `force_split_min_silence`, default 3.0s) become hard sentence boundaries, post-enforced on every emitted segment so no output sentence spans a long pause — but only where the WhisperX word timings corroborate the silence with a real inter-word gap (WhisperX often stretches one word across a pause, and splitting beside a stretched word would chop the sentence's first characters off). Deleting a line from the human-editable cuts file both keeps that span's audio and stops it forcing a split. Disabled → byte-identical copy-through of `output/transcription/{stem}.{json,txt}`.

See [`docs/stages/sentence_split.md`](docs/stages/sentence_split.md) for the re-segmentation core, windowing/carry-over mechanics, and the full force-split corroboration rule.

- **Inputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text) from transcription; the audio_silence `{stem}_cuts.txt`, passed as `cuts_txt` (for force-split silences)
- **Outputs:** re-segmented `{stem}.json` + `{stem}.txt` in `output/sentence_split/`

### gap_context — Silent-Gap Visual Context

A vision LLM (config `gap_context:`, disabled by default) runs once per video, between `sentence_split` and `summary`. Long silent spans (`gap_context.min_gap`, default 3.0s) from the audio_silence `{stem}_cuts.txt` are selected (`snapshot.select_gaps()`), snapshotted at up to 3 timestamps each — start+0.2s, midpoint, end-0.2s (`snapshot.frame_times()`) — via ffmpeg inside the whisperx Docker image, then described by one vision-LLM call per gap (`gap_context.describe.describe_gap()`): frames as base64 `image_url` parts, a plain-text (non-JSON) response, empty response = failure/retry. Frame extraction for every gap of every source runs in **one** `docker compose run` for the whole stage (`pipeline.external.build_snapshot_batch_cmd()`, one `ffmpeg ... || true` line per frame in a single shell script) rather than one container per frame — container-startup overhead (~0.82s/run) otherwise dwarfs the ~30ms of actual ffmpeg work per frame (measured: 3 frames as 3 containers = 2.47s vs. 1 container = 0.85s, byte-identical output). The LLM report records frame **paths**, never base64 payloads. Before the vision call, a cheap ffmpeg SSIM comparison runs over every consecutive pair of a gap's extracted frames (a 3-frame gap yields 2: first-vs-mid, mid-vs-last — appended to the same batch container script, `snapshot.ssim_relpath`/`parse_ssim_stats`) and the **minimum** across a gap's pair scores skips the LLM entirely when it is at least `gap_context.static_ssim` (default `0.96`, `0` disables — and `0` disables the SSIM planning itself in `pipeline/stages.py`, not just its consumption in `run.py`): the gap is written straight to `{stem}_gaps.json` as `static: true` with a `(prefilter...)` description, cutting real-run vision-call cost by catching pixel-static gaps up front. Comparing only first-vs-last (as an earlier version did) would miss a camera pan-away-and-return, since the middle frame — already extracted, already paid for — is the one that would reveal the on-screen action; `min(a,b) >= T` is exactly `(a>=T) AND (b>=T)`, i.e. static *throughout*, not just at the endpoints. Deleting a line from the human-editable `_cuts.txt` both keeps that span's audio (as for sentence_split's force-split) *and* removes it from gap-context snapshotting. Output `{stem}_gaps.json` (`{"gaps":[{start,end,frames,description,static}]}`, purely time-based) is a hand-editable intermediate; `gaps.gaps_from_dict`/`load_gaps` are lenient — a malformed entry is dropped, never raised, and a surviving `description`'s whitespace is collapsed to single spaces (the raw vision-LLM response gets the same collapse in `describe_gap`), so neither a non-compliant multi-line reply nor a hand-edit can inject a fake `N: ...`-looking line into a downstream numbered transcript. The default prompt makes the vision LLM start its reply with `ACTION:` or `STATIC:`; `describe_gap` strips the marker into a `static` boolean (missing marker → `false`), and `context.anchor_gaps()` skips static gaps entirely so scenes with no on-screen change never bloat the summary/director prompts (hand-flip `"static": false` in the gaps file to force one back in; absent/non-boolean reads as `false` for older files). Disabled → `{"gaps": []}` no-op, no Docker calls.

- **Inputs:** the audio_silence `{stem}_cuts.txt`; the source video file; optionally the sentence_split `{stem}.json`, passed as `json_path` (for the before/after neighbour lines given to the vision LLM — `gap_context.context_lines`, default 1, sets how many per side; 0 omits them)
- **Outputs:** `output/gap_context/{stem}_gaps.json`; frames under `output/gap_context/frames/{stem}/`

See [`docs/stages/gap_context.md`](docs/stages/gap_context.md) for gap selection, frame sampling (including the too-short-to-inset early return), the vision-call contract, the `{stem}_gaps.json` contract, and how `summary`/`director` anchor and render described gaps.

### summary — Project-Wide Summaries

A larger LLM (config `summary:`, disabled by default) runs **once project-wide, between sentence_split and text_filter**. For each video it maps the numbered transcript into line-range **parts** with a one-sentence summary each, plus misspelling-prone keywords and a mandatory whole-video summary (`summarize.segment_video()`, `{"parts":[{"lines":[a,b],"summary":...}],"keywords":[...],"video_summary":"..."}` → `(parts, keywords, video_summary)`; a missing/non-string/empty `video_summary` is a hard parse failure that retries, same as a missing `parts` array), then reduces all parts into one all-videos summary (`generate_project_summary()`); `build_summary()` is the map-then-reduce entry point and also collects each video's summary into `ProjectSummary.video_summaries` (`{stem: video_summary}`). The reduce call's input is grouped per video — a `## <stem> — <video_summary>` header per video with its parts nested beneath, global 1-based part numbering preserved across the whole document (`_format_parts_doc()`). Each video's `segment_video()` response also carries `"keywords"` (misspelling-prone words, per video, coerced leniently — non-string/empty entries dropped, empty list on absence). Reuses `director_llm`'s transcript-formatting helpers and `llm_retry`; any failure degrades gracefully to empty parts/summary/keywords/video_summaries. `summary.json` (`{summary, parts:[{stem,lines,summary,start?,end?,silence?}], keywords:{stem:[...]}, video_summaries:{stem:"..."}}`) is human-reviewable and feeds `text_filter`/`plan`/`director`; `summary_from_dict` reads `video_summaries` leniently and stays backward-compatible with older files that lack it (absent → `{}`). Each part's optional `start`/`end` (seconds, via `timing.segment_times`) lets `plan` render per-part duration/gap; omitted when timing is unavailable. The orchestrator passes each source's sentence_split `{stem}.json` via `run_summary(..., json_paths=...)` to derive those times. Each part also optionally carries `silence` (seconds of that part's `[start, end]` span covered by the audio_silence `{stem}_cuts.txt` ranges, via `timing.span_silence`) — the orchestrator passes each source's `{stem}_cuts.txt` via `run_summary(..., cuts_paths=...)`, `build_summary(..., cuts_by_stem=...)` computes it per part (only set when > 0.0), and `plan` renders it as a speech/silence split bracket. When `gap_context` produced described gaps for a video, `run_summary` anchors them to that video's transcript lines (`gap_context.context.anchor_gaps()`) and renders a `## Silent gaps (visual context)` block (`format_gap_block()`) appended to that video's `segment_video()` user content — an absent/empty gaps file (stage disabled, or no long gaps) leaves the prompt byte-identical to before this feature. Disabled → `{"summary":"","parts":[],"keywords":{},"video_summaries":{}}` no-op.

- **Inputs:** every sentence_split `{stem}.txt`, passed as `txts` (stem derived from basename); optionally the sentence_split `{stem}.json` per source, passed as `json_paths` (for part `start`/`end` times); optionally each source's gap_context `{stem}_gaps.json`, passed as `gaps_paths` (for the `## Silent gaps` block); optionally each source's audio_silence `{stem}_cuts.txt`, passed as `cuts_paths` (for per-part `silence`)
- **Outputs:** `output/summary/summary.json`

### text_filter — Text Editing Checkpoint (mandatory)

Produces `{stem}_edits.txt` for human review. When `text_filter.use_llm` is `false` (default), copies the transcription `.txt` as-is. When enabled, runs LLM filter and writes output with `{{old->new}}` markers preserved. When `use_llm` is enabled, `run_text_filter` reads the summary stage's `summary.json` (passed as `summary_json`); this video's whole-video summary, part summaries, and keywords — merged with the constant `text_filter.keywords` list — are appended to the filter LLM's system prompt via `text_filter/context.build_enhanced_prompt`; missing/empty/malformed `summary.json` (or an empty/absent `video_summaries` entry) degrades to the base prompt, byte-identical to before.

Humans may wrap a span in `<keep>...</keep>` to force-preserve its audio in the intervals stage. It may open on one line and close on a later one, spanning multiple WhisperX segments (and the silences between them). Added by the human *after* the LLM filter runs — the LLM never sees it.

`<speed factor="N.N">...</speed>` plays the region at the given speed in the blender stage. Unlike `<keep>`, it does **not** force-preserve audio — silence inside a `<speed>` span is still cut by silence detection, and the speed applies only to the surviving spoken parts (its span is emitted verbatim in `speed_ranges` regardless, and any portion on cut content is ignored downstream). To keep the audio **and** speed it, nest the tags: `<keep><speed factor="N.N">…</speed></keep>` (the extractors parse each tag type independently and strip the other's tags when counting positions). Multi-line/nesting/authorship rules follow `<keep>`.

`<overlay text="..." duration="N.N"/>` places an on-screen TEXT strip in the blender stage. It is a **self-closing point marker**, not a wrapping tag: where it sits is where the text appears, and `duration` states how many seconds it stays on screen — measured on the **edited** timeline (the blender stage converts it to output frames, so cuts and speed ranges inside the window can't shorten the reading time). There is no maximum clamp and no wrapping form: an end time derived from a closing tag's position is exactly the bug this replaced (a single-line director op once produced a 75.8s caption because the closer landed elsewhere). Attribute order is fixed (`text` then `duration`); a non-positive duration or empty text is skipped with a warning. Like `<speed>` (and unlike `<keep>`) it does **not** force-preserve audio, so its anchor can land on cut footage — a line's opening seconds are routinely cut while keep margins don't reach back far enough (a real run lost the series-recap caption by 1.99s). The intervals stage therefore **snaps** an off-keep anchor forward to the first surviving moment of the marker's own line (`intervals.snap_overlay_starts`, run against the final keep intervals); `duration` is never clipped to that line's surviving footage, since it is stated reading time and a caption is allowed to run on over what follows. The overlay is skipped (with the blender stage's warning) only when its line has no surviving footage at all. Quotes inside `text="..."` are unsupported (regex uses `[^"]*`). A multi-line caption **is** supported, but the marker must stay on one `_edits.txt` line (line N maps to segment N), so a line break travels escaped as the two characters `\n` — `escape_overlay_text()`/`unescape_overlay_text()` in `intervals/sync_json.py` own that encoding (only `\\` and `\n` are recognised; other backslash sequences pass through). `director_llm` normalises CR/CRLF in an overlay op's `text` to `\n` at the parse boundary and drops an op whose text is blank; `guided_edit.apply_point_op()` escapes on write; `check_edits()` reports any embedded newline in an edit line, so guided_edit's pre-write check catches it instead of the intervals stage crashing a stage later.

`<cut>...</cut>` is a deletion shorthand: it desugars to `{{wrapped->}}` deletion patches (`sync_json._expand_cut_tags`), so the words vanish from the JSON and the resulting gap is cut by the interval stage's word-gap silence detection — meant for deletions longer than `intervals.silence_threshold`, and immune to caption re-expansion since deleted text has no caption. Balance/nesting rules follow `<keep>`; don't overlap it with `<keep>/<speed>/<overlay>` on the same span.

- **Inputs:** `{stem}.txt`; `output/summary/summary.json` (as `summary_json`)
- **Outputs:** `{stem}_edits.txt`

Validate a hand-edited `_edits.txt` before resuming with `python -m nagare_clip.intervals.check_edits --edits-txt <file> --json <file>` (`src/nagare_clip/intervals/check_edits.py`). Unlike the interval stage's fail-fast `ValueError`, it collects **every** problem at once (line-numbered, exit 1 if any): line count vs. JSON segments, `{{old->new}}` syntax, decomposition integrity, and tag balance/validity for all four markers. When every itemised check passes, a final **parity guard** runs the real `sync_text_to_json` and reports any rejection as a Problem — the checker can never bless a file the intervals stage would crash on. Pure `check_edits(edit_lines, json_data) -> list[Problem]`, never raises.

### plan — Cross-Video Rough Directions

A larger LLM (config `plan:`, disabled by default) runs once project-wide after `summary`, reading all per-part summaries plus the overall summary to emit a coarse **cross-video** direction per part (`plan_llm.generate_plan()`, `{"directions":[{"index":N,"direction":...}]}` mapped back by 1-based index) — e.g. flagging a part that repeats an earlier video as "remove". Context lines render each part's duration + gap-to-next (`N: stem [a-b] [12.4s, gap 1.5s] — summary`, gap shown only within the same video and omitted when negligible/would render 0.0s; no JSON is read at this stage, times come from `summary.json`); when the part's `summary.json` entry also carries `silence` (audio_silence overlap inside the part, computed at summary time), the bracket splits into a speech/silence form instead — `[12.9s speech, 62.9s silence]`, duration now the speech-only figure — the default `plan.prompt` documents both bracket forms (a test pins the documented examples to `timing.format_dur_gap`'s output). `_format_parts_for_plan()` also prints a `Video "<stem>": <video_summary>` header above each video's first part when that video has a `summary.json` `video_summaries` entry; byte-identical to before when absent. The plan vocabulary deliberately avoids the word **keep**: `keep` is a director *op* with a mechanical cost (it restores every silence in its range), and `plan.json` is fed to the director as context, so the default `plan.prompt` offers `feature`/`retain`/`emphasise` instead and carries an explicit rule forbidding `keep` in a direction (a test asserts every occurrence of the word in the prompt is that rule, and that no example direction verb is a `director_llm.VALID_TYPES` op name). Out-of-range/malformed entries are dropped (logged); parse/LLM failure retries via `llm_retry`, then degrades to empty. `plan.json` (`{directions:[{stem,lines,direction}]}`, self-contained) feeds `director`. Disabled → `{"directions":[]}` no-op.

- **Inputs:** `output/summary/summary.json`
- **Outputs:** `output/plan/plan.json`

### director — LLM High-Level Edit Operations (Pass A)

A larger LLM (config `director:`, disabled by default) reads the numbered transcript and emits `{stem}_director.json`: `{"ops": [{type, lines:[a,b], factor?, text?, duration?, note}]}`, `type ∈ {cut, speed, overlay, keep, edit}`, lines 1-based. An `overlay` op must carry a positive `duration` (on-screen seconds) alongside its `text` — without it the op is dropped, since an overlay's on-screen time is stated, never derived from where a tag landed; its `lines` only say **where** the caption appears (it is applied at the first line of the range). The default `director.prompt` asks for a duration proportional to reading length (a short label ~2s, a full sentence 4–6s), and a test parses the prompt's own overlay example through `parse_director_response` so a stale example fails loudly. It **never re-outputs transcript text**, avoiding whole-file-editing's format-breakage/modification failure modes. Lines are annotated with duration + gap-to-next (e.g. `3: text [4.2s, gap 0.8s]`; a negligible gap that would render 0.0s is omitted) from the video's WhisperX JSON, passed by the orchestrator as `json_path` (`format_numbered_transcript_timed`); falls back to the byte-identical untimed transcript if `json_path` is absent or its segment count mismatches; the default `director.prompt` documents that bracket notation (a test pins the documented example to `timing.format_dur_gap`'s output) and forbids a `cut` range from overlapping any other op's range. When the orchestrator's audio_silence `{stem}_cuts.txt` is passed as `cuts_txt`, `run_director` computes per-line silence overlap (`timing.segment_silences`) and a line with long internal silence renders `[12.9s speech, 62.9s silence]` instead of the plain duration, so the director judges pacing from speech time rather than a span that is mostly already-dropped silence; absent/missing `cuts_txt` leaves brackets byte-identical to before. A `keep` op's width **follows what is on screen**: the default prompt asks for the narrowest range covering a single silent gap (normally `[N, N+1]`), but tells the director to span the **whole event** in one keep when a continuous action is playing out across several gaps (an accident and the cleanup after it, a demo running, a result arriving) — rescuing each gap separately chops the payoff into jump cuts, which is how a real run lost the water-spill cleanup it had explicitly recognised. Widening a keep over **talking** stays forbidden (speech is never dropped by default, so such a keep only restores its pauses — a real run turned a 22.3min cut into 53.9min this way), and the prompt still states that a `feature`/`retain` direction from the project context is editorial emphasis and NOT a keep request. `director.max_keep_lines` (default 8, `0` = no limit) **drops** any wider keep the LLM emits — a continuous event fits well inside that because nobody is talking through it, whereas a range wide enough to cover talking is marking speech as important — dropping degrades to the default (speech kept, silence cut), whereas clipping would restore a silence nobody chose. The cap applies to the LLM response only; `ops_from_dict()` is deliberately uncapped so a hand-edited `_director.json` (a human-editable intermediate) keeps a wide keep the human wrote. The enforced number is stated in the system prompt at runtime via `director_llm.keep_limit_note()`, not baked into `director.prompt`. `parse_director_response()`/`ops_from_dict()` drop malformed/out-of-range ops individually (logged). Retried (config `director.max_retries`, default 2) on connection error or hard parse failure only — a valid empty `{"ops": []}` is accepted without retry; each retry nudges temperature up via `llm_retry.cfg_for_attempt()`. All attempts failing → empty op list. When `summary`/`plan` are enabled, `director.context.build_director_context()` appends a cross-video overview (global summary + this video's parts + one-line sibling entries) to the system prompt; disabled/empty → prompt is byte-identical to before (regression-guarded). This video's `summary.json` `video_summaries` entry, when present, renders as a `Summary: <video_summary>` line under the `This video ("<stem>")` header; each sibling's one-liner also prefers its own video summary over its first part's summary (falling back to the latter when absent). Lives in `director/context.py`, not `director_llm.py`, so `summary` can import `director_llm` without a cycle. When gap_context produced described gaps for this video, they are anchored to the timed transcript (`gap_context.context.anchor_gaps()`) and inserted as indented, un-numbered `    [silent gap N.Ns: description]` lines (`annotate_numbered_transcript()`) — the default `director.prompt` documents this rendering and instructs the director to emit a `keep` op spanning the annotated line and the next one (`[N, N+1]`) to rescue a gap worth watching — or the whole run in ONE keep when the described action continues across several gaps — and never to reference an annotation line as an op line. Only applies when `seg_times` is also present (the annotation needs anchor times); an absent/empty `gaps` file leaves the prompt byte-identical to before this feature. The default `director.prompt` frames its goal as tighten-AND-stage rather than pure trimming: it prefers `speed` over `cut` for repetition that builds toward a payoff (reserving `cut` for spans that leave the throughline entirely), gives `overlay` a when-to-use rule (turning points, conclusions, failures, mishaps) plus a loose ~1-per-3-5-minutes density target, and reads an ordinary long timing-gap as a possible keep candidate — not just a `gap_context`-described one — when the speech immediately before/after it announces an accident, cleanup, or other event, with "dead air" as the fallback reading rather than the only one.

- **Inputs:** `{stem}_edits.txt` (from text_filter); optionally the sentence_split `{stem}.json`, passed as `json_path` (for per-line `[dur, gap]` timing), `output/summary/summary.json`, `output/plan/plan.json`, the source stem, passed as `stem`, the gap_context `{stem}_gaps.json`, passed as `gaps`, and the audio_silence `{stem}_cuts.txt`, passed as `cuts_txt` (for per-line speech/silence split)
- **Outputs:** `{stem}_director.json`

### guided_edit — Apply Director Ops (Pass B2)

Applies each director op into `_edits.txt`. **Span ops** (`cut`/`speed`/`keep`) are a pure whole-line-range wrap — the director already fixed the boundaries and the op granularity is whole-line, so `apply.apply_span_op()` deterministically prepends the open tag to the first boundary line and appends the close tag to the last (both on one line for single-line ranges), **with no LLM call**. Existing markers/patches on a line are left intact (nested inside), so the underlying text is unchanged. Because the director reads tag-stripped text (and may overlap its own ops), a span op's range can collide with tags already in the file (human-authored or applied by an earlier op); `apply.clip_range()`/`blocked_lines()` clip the op to the largest contiguous run of lines it may touch (ties → earliest). An `overlay` op is instead a **point**: `apply.apply_point_op()` prepends the self-closing `<overlay text="..." duration="N.N"/>` marker to the first free line of its range and touches nothing else, so no closing tag exists to be misplaced. An op is blocked by a **same-type** span (nesting is rejected by the intervals extractors) and by a **`<cut>`** span; a `cut` op is blocked by ANY existing tag (cut deletes whatever it wraps, so a keep/speed/overlay caught inside would be silently swallowed) and cut ops are applied **after** all other span ops so protections land first and the cut clips around them (e.g. the director's `cut [81,96]` + `keep [96,96]` resolves to `cut [81,95]`). A clip is logged; an op whose whole range is blocked is dropped (logged and recorded in the LLM report). keep/speed/overlay never block each other (each type is parsed independently; keep+speed overlap is by design), but a line already carrying an overlay marker blocks another overlay op (two captions at one point would stack on screen). Only **`edit` ops** (a within-line `{{old->new}}` the director described only in prose, no explicit old/new) use the small local LLM (config `guided_edit:`, disabled by default), with **one call over just the op's boundary line(s)** (wide ranges show only first/last line with an omission marker). `reconcile.verify_op()` checks both that the underlying text is unaltered (`clean_old()` before/after) and that the op landed — for span ops, the **opening** tag must be on the first boundary line and **closing** on the last (not just present somewhere in range), and for an overlay op the marker must be on the op's own line; deterministic application satisfies this by construction, and verification is kept as a guard against a splice bug. A failing `edit` op retries (config `guided_edit.max_retries`, default 2), nudging temperature up via `llm_retry.cfg_for_attempt()`; after all retries fail (or a span op fails verification), the op is reverted and logged (and recorded in the LLM report as `dropped-items`) with the failure reason. Disabled → copies `_edits.txt` through unchanged. A final `check_edits` pass (when `json_path` is given — the orchestrator always passes it) logs residual problems.

- **Inputs:** `{stem}_edits.txt` (text_filter), `{stem}_director.json`, `{stem}.json`
- **Outputs:** augmented `{stem}_edits.txt`

### intervals — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. The audio_silence `_cuts.txt` ranges are unioned into the exclude set (via the `cuts_txt` argument) before inversion. Any `<keep>...</keep>` ranges from `_edits.txt` are then subtracted from the unioned excludes so the wrapped audio survives both silence sources. `<speed factor="...">...</speed>` spans do **not** force-keep audio — they are written verbatim to a top-level `speed_ranges` array in the output JSON (independent of `keep_intervals`) but are not subtracted from the excludes, so silence inside a bare `<speed>` span is still cut; the blender stage splits keep intervals at those boundaries. `<overlay .../>` markers are likewise independent: `extract_overlay_marks()` emits `{start, duration, text}` entries into a top-level `overlays` array (a start time plus edited-timeline seconds — no end time anywhere in the contract); once the keep intervals are final, `snap_overlay_starts()` moves any anchor that fell on cut footage forward to the first surviving moment of its own line (logged), leaving it in place only when that line survived nowhere. All existing caption/min_keep/margin safeguards still apply. A final `merge_close_intervals` pass (config `intervals.min_cut`, default 0.4s, `0` disables) then absorbs any remaining gap **between** adjacent keep intervals that is shorter than the threshold — the only pass constraining gaps rather than intervals, so margin arithmetic can no longer leave millisecond cuts that jump the picture without saving runtime. Runs per source in-process within the pipeline.

- **Inputs:** `{stem}_edits.txt`, `{stem}.json` (transcription original), `{stem}_cuts.txt` (audio_silence)
- **Outputs:** `{stem}_intervals.json` (keep intervals + captions)

### blender — Blender VSE Layout

Auto-assembles the rough cut in headless Blender. References original media in-place (no re-encoding). Concatenates all sources onto a single timeline. Strip placement opens the source once as a template video+sound pair, connects that pair **once** (every duplicate inherits a connection to its own audio copy), and grows the copies by **doubling** (`allocate_pairs()`, `ceil(log2(N)) + 1` duplicate ops instead of one per interval) — `sequencer.duplicate`/`connect` both cost more per call the fuller the scene gets, so per-interval operator calls made placement quadratic (1600 intervals: 41.8s → 2.6s; 4000: >10min → 15.3s). Everything else per interval is plain property assignment.

- **Inputs:** source video files, `{stem}_intervals.json` for each source
- **Outputs:** `{stem}_edited.blend` — ready for human editing

### Human Editing Workflow

1. Run transcription–text_filter → audio_silence produces `{stem}_cuts.txt`, text_filter produces `{stem}_edits.txt`
2. Human edits `_cuts.txt` (delete/adjust silent spans) and `_edits.txt` (`{{old->new}}` patches; optional `<keep>`, `<speed factor="N.N">`, `<overlay text="..." duration="N.N"/>`)
3. Resume with `--from-stage intervals` → unions cuts, applies patches, syncs JSON, carves out `<keep>` ranges (not `<speed>`, which only annotates playback speed), computes intervals, runs Blender (with Speed Control effects for `<speed>` regions)

## Hard Constraints

- Dependency management uses uv + pyproject.toml.
- LiteLLM is the LLM transport dependency: all provider access (OpenAI/Gemini/Anthropic/Ollama) goes through `nagare_clip.llm_client.call_llm` — do not add provider-specific HTTP clients.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Route media tooling (ffmpeg) through the existing whisperx Docker image; do not add host binaries or new Python audio deps.
- Preserve the interval JSON (`intervals/` package) as the human-editable contract for the Blender stage.
- The Blender stage must reference original media; do not re-encode/copy source media.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  llm_retry.py                # Shared bounded-retry helpers (director/guided_edit): retry_attempts(), cfg_for_attempt()
  llm_report.py               # Structured per-call LLM report: Recorder + rebuild_index (index.md + per-call <stage>/<unit>.md)
  llm_client.py               # Unified LiteLLM transport: call_llm(messages, cfg) -> str (OpenAI/Gemini/Anthropic/Ollama)
  brief.py                    # project: editorial brief -> format_brief()/apply_brief() (summary/plan/director/text_filter prompts)
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
    detect.py                 # parse_silencedetect_output(), build_ffmpeg_args() (pure)
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
    context.py                 # anchor_gaps(), format_gap_block(), annotate_numbered_transcript() (shared by summary/director)
    run.py                     # run_gap_context() (writes {stem}_gaps.json; no-op when disabled)
  text_filter/                # text_filter stage modules (text editing checkpoint)
    run.py                    # run_text_filter() typed entry point
    llm_filter.py             # LLM API calls, {{old->new}} patch parsing, apply_patches_to_lines()
    context.py                # build_enhanced_prompt(): summary.json context -> filter prompt
  summary/                    # summary stage (project-wide): per-part + all-videos summaries
    summarize.py              # PartSummary/ProjectSummary(+keywords), segment_video(), build_summary()
    run.py                    # run_summary() (repeated sentence_split txt/json paths -> summary.json)
  plan/                       # plan stage (project-wide): cross-video rough directions
    plan_llm.py               # PartDirection, generate_plan(), plan_to/from_dict()
    run.py                    # run_plan() (summary.json -> plan.json)
  director/                   # director stage (Pass A): high-level edit ops
    director_llm.py           # DirectorOp, parse/validate JSON ops, generate via LLM
    context.py                # build_director_context(): summary+plan -> prompt overview block
    run.py                    # run_director() (writes _director.json; summary/plan/stem/json_path)
  guided_edit/                # guided_edit stage (Pass B2): apply director ops
    apply.py                  # per-op LLM call + splice + revert-on-failure
    reconcile.py              # verify_op(): verbatim-safety + op-reflection checks
    run.py                    # run_guided_edit() (writes augmented _edits.txt)
  intervals/                  # intervals stage modules (patch application + intervals)
    run.py                    # run_intervals() typed entry point (patch + intervals; cuts_txt)
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
    frames.py                 # Pure frame-range clamp helper (no bpy import, host-testable)
scripts/
  run_pipeline.sh             # Shim: exec uv run python -m nagare_clip.pipeline "$@"
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
  plan/                       # plan generate/parse + run() tests
  director/                   # director op parsing/generation + context + run() tests
  guided_edit/                # guided_edit apply/reconcile + run() tests
  intervals/                  # interval-stage unit tests (incl. <keep>/<cut> markers, cuts_txt union)
  blender/                    # Blender-stage tests
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
  Blender TextStrip pass-throughs (any RNA attribute incl. `font`).
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
`plan`, `director` and `text_filter`, called from those four stages' `run.py` so
no LLM module needed a new parameter. `previous_summary` is a path to a previous
project's `summary.json`; its overall summary joins the brief (a missing/unreadable
file drops only that line). Every field empty → `apply_brief` returns the same
dict and prompts are byte-identical to a run without the section — regression-tested
per stage. `gap_context`/`sentence_split`/`guided_edit` are deliberately not briefed
(mechanical stages). See [`docs/stages/project_brief.md`](docs/stages/project_brief.md).

All LLM stages (`sentence_split`, `gap_context`, `summary`, `text_filter`, `plan`, `director`, `guided_edit`) route through `nagare_clip.llm_client.call_llm` (LiteLLM). Each block selects its backend with a `provider` key (default `ollama_chat`); the model id sent to LiteLLM is `"<provider>/<model>"`. An empty `api_base` falls back to `http://localhost:11434` for an ollama provider, or is omitted for a cloud provider. `api_key` is forwarded when set (or use the provider's env var). `response_format: "json"` maps to a JSON-object request; `thinking` maps to LiteLLM `reasoning_effort` (best-effort per provider).

Only `blender/blender_cli.py` still takes a `--config <path>` flag on its command line — it runs as a separate Blender subprocess, so the pipeline CLI (`nagare_clip.pipeline.cli`) passes its resolved `config_path` through explicitly. Every other stage receives the already-merged `cfg` dict in-process (no subprocess, no re-parsing of `--config`).

## Current Runtime Quirks

Deep per-stage implementation detail — edge cases, invariants, and Blender-bug
workarounds — lives in [`docs/stages/`](docs/stages/), loaded on demand. **Always
read the relevant file first when you need to touch a stage, and keep every
`docs/stages/` file up to date whenever you change that stage's behavior** (see
the [Documentation Policy](#documentation-policy)):

- project brief (`project:` config → summary/plan/director/text_filter prompts) → [`docs/stages/project_brief.md`](docs/stages/project_brief.md)
- audio_silence → [`docs/stages/audio_silence.md`](docs/stages/audio_silence.md)
- sentence_split (re-segmentation, windowing/carry-over, force-split) → [`docs/stages/sentence_split.md`](docs/stages/sentence_split.md)
- gap_context (gap selection, frame sampling/extraction, vision-call contract, summary/director consumption) → [`docs/stages/gap_context.md`](docs/stages/gap_context.md)
- text_filter (+ summary-stage filter context) → [`docs/stages/text_filter.md`](docs/stages/text_filter.md)
- intervals (`<keep>`/`<speed>`/`<overlay/>`/`<cut>` markers, margins, captions) → [`docs/stages/intervals.md`](docs/stages/intervals.md)
- blender (VSE layout, text styling, retiming) → [`docs/stages/blender.md`](docs/stages/blender.md)
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
