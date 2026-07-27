# nagare-clip — Implementation Status

## Python Pipeline Orchestration

**Status: complete** (2026-07-07; branch `worktree-python-orchestration`; design in `docs/superpowers/` sdd task specs).

`scripts/run_pipeline.sh` (634 lines of bash) was ported to a Python package,
`src/nagare_clip/pipeline/` — `errors.py` (`PipelineError`), `sources.py`
(source discovery/resolution/staging), `external.py` (docker/blender command
builders + `run_command()`, the only remaining subprocesses), `runner.py`
(`Stage`, `PipelineContext`, `resolve_window()`, `run_stages()` — windowing and
skip-validation), `stages.py` (`STAGE_NAMES` + one adapter per stage +
`STAGES` registry; each LLM-stage adapter owns its `llm_report.Recorder`
lifecycle), and `cli.py` (same flags as the old bash script; CLI values become
validated config overrides via `get_effective_config`, so precedence is
CLI > YAML > defaults). `scripts/run_pipeline.sh` is now a 6-line shim that
execs `uv run python -m nagare_clip.pipeline "$@"`, so user-facing usage is
unchanged. Every stage exposes a typed `run()` in `src/nagare_clip/<stage>/run.py`;
the eight per-stage `cli.py` files (and the `nagare-clip-intervals` console
script) were deleted — `python -m nagare_clip` is re-aliased from the
intervals stage to the pipeline CLI. Single-stage runs now use
`--from-stage X --to-stage X` on the pipeline CLI. Unknown stage names and
inverted `--from-stage`/`--to-stage` ranges raise a friendly `PipelineError`
(the old bash `stage_index` set-e quirk is gone). See
[`docs/stages/pipeline.md`](docs/stages/pipeline.md) for full runtime detail.

## sentence_split Stage

**Status: complete** (branch `sentence-split-stage`, Tasks 1–7; design in `docs/superpowers/specs/2026-06-29-sentence-split-stage-design.md`).

The `sentence_split` stage sits between `audio_silence` and `text_filter` and is disabled by default (byte-identical copy-through when `sentence_split.enabled: false`). When enabled, it rewrites the WhisperX `{stem}.json` + `{stem}.txt` into one-sentence-per-line units using a **bunsetsu-index-range approach**: the LLM receives a numbered list of GiNZA bunsetsu units and returns contiguous index ranges (`{"sentences":[[a,b],…]}`), which the stage maps back to whole-word boundaries via `char2word` and uses to reassemble segments from the original word list. Words are only reassigned, never edited, so word timings are verbatim by construction. A final `concat_word_text` check guards the verbatim invariant. Processing is windowed (`window_segments`, default 20 — the batch size) for long transcripts; each window degrades independently on LLM failure. Windows carry their trailing (possibly incomplete) sentence into the next window so a sentence straddling a window boundary is re-grouped rather than split at the seam; a single-sentence window emits as-is and resets the carry (run-on guard), and on degrade the carried sentence is flushed before falling back to the original segments. When `force_split` is enabled (default), long silences from the `audio_silence` stage's `{stem}_cuts.txt` (spans ≥ `force_split_min_silence`, default 3.0s) become hard sentence boundaries: every emitted segment — LLM-rebuilt, carry-flush, or degrade-fallback — is post-split (`split_segment_at_silences`) before the first word past the silence midpoint, but only when the previous word's end is also ≤ the midpoint (a genuine inter-word gap corroborates the silence — WhisperX often stretches one word across a pause, and splitting beside a stretched word chops the sentence's first characters, so ambiguous silences are skipped). This is pure post-enforcement: window contents and the LLM call count are unchanged, and the forced split survives total LLM failure. All downstream stages read from `output/sentence_split/`.

## Langfuse LLM Tracing

**Status: complete** (branch `feat/langfuse-tracing`, Tasks 1–9 committed; Task 10 docs).

Langfuse tracing was implemented at the single LLM chokepoint — `nagare_clip.llm_client.call_llm` — via LiteLLM's `langfuse_otel` OTEL callback (`litellm.callbacks = ["langfuse_otel"]`). The callback is registered once per process by `_ensure_tracing()` (idempotent) and flushed on exit by an `atexit` hook (`flush_traces()`, a workaround for short-lived CLI processes). Tracing is env-gated: enabled only when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set AND `NAGARE_LANGFUSE != "0"` AND `general.langfuse` (config, default `true`) is not false; when disabled the provider call is byte-identical to the pre-tracing behaviour. Each call carries `generation_name="<stage>/<unit>"` and `tags=["stage:<stage>","stem:<unit>"]`; `session_id` is set to `NAGARE_RUN_ID` (one timestamp per `run_pipeline.sh` invocation, so all calls in a run group under one Langfuse session). Metadata is carried via `with_trace_meta(cfg, stage=..., unit=...)` under a reserved `cfg["_trace"]` key that `call_llm` pops before forwarding kwargs to the provider. `run_pipeline.sh` exports `NAGARE_RUN_ID` and maps `general.langfuse: false` to `NAGARE_LANGFUSE=0` for all stage subprocesses. The existing markdown `llm_report` is untouched and runs alongside as an independent sink. Documented fallback: if OTEL flushing proves unreliable, switch to the langfuse-SDK callback (`["langfuse"]`) with explicit `langfuse.flush()` in `flush_traces`. This is observability-only and decoupled from any future LangGraph migration.

## summary/text_filter Merge

**Status: complete.**

The `summary` stage moved before `text_filter` (order: …, sentence_split, summary,
text_filter, plan, …) and now reads the sentence_split `{stem}.txt` transcripts — safe
because text_filter is line-preserving, so part line-ranges stay valid downstream. Its
per-video LLM call also returns `"keywords"` (misspelling-prone words), stored as
`summary.json`'s top-level `keywords: {stem: [...]}`. text_filter's built-in summary LLM
(`text_filter/summary_llm.py`, config `text_filter.summary_llm`) was removed; the filter
prompt is now primed from `summary.json` (+ the constant `text_filter.keywords` list) via
`text_filter/context.build_enhanced_prompt`. Config break: `text_filter.summary_llm` now
fails validation (migration: move `keywords` up; enable `summary.enabled`).

## Per-Video Summaries in the summary Stage

**Status: complete** (design in `docs/superpowers/specs/2026-07-12-summary-video-summaries-design.md`,
plan in `docs/superpowers/plans/2026-07-12-summary-video-summaries.md`, Tasks 1-8).

`summarize.segment_video()` now requires a mandatory whole-video summary in its LLM
response (`"video_summary"`); missing/non-string/empty is a hard parse failure that
retries, same as a missing `parts` array, and the function returns a `(parts, keywords,
video_summary)` 3-tuple. `build_summary()` collects these into `ProjectSummary.video_summaries`
(`{stem: video_summary}`), persisted as a new top-level `video_summaries` key in
`summary.json` (shape now `{summary, parts, keywords, video_summaries}`); `summary_from_dict`
reads it leniently and stays backward-compatible with older files lacking it (absent →
`{}`). The reduce call (`generate_project_summary`) now receives its per-part input grouped
per video — a `## <stem> — <video_summary>` header per video with its parts nested beneath,
global 1-based part numbering preserved. `plan`, `director`, and `text_filter` each surface
the relevant video's whole-video summary in their prompts/context (a `Video "<stem>": ...`
header in `plan`, a `Summary: ...` line under `director`'s `This video (...)` block plus
sibling one-liners preferring their own video summary, and a `Video summary: ...` line in
`text_filter`'s enhanced prompt) — all byte-identical to before when `video_summaries` is
empty (stage disabled or an older `summary.json`).

## gap_context Stage

**Status: complete** (branch `gap-context`; design in `docs/superpowers/specs/2026-07-13-gap-context-design.md`, plan in `docs/superpowers/plans/2026-07-13-gap-context.md`).

The `gap_context` stage sits between `sentence_split` and `summary` and is disabled by default (writes `{"gaps": []}`, no Docker calls, both consumers byte-identical to before). When enabled, `snapshot.select_gaps()` picks audio_silence `{stem}_cuts.txt` spans at least `min_gap` seconds long (default 3.0s — deleting a cuts-file line both keeps that audio and drops it from snapshotting, same file sentence_split's force-split reads), `snapshot.frame_times()` samples up to 3 timestamps per gap (start+0.2s, midpoint, end-0.2s; a span too short to inset collapses to a single midpoint frame), and `pipeline.external.build_snapshot_batch_cmd()` extracts one JPEG per timestamp via ffmpeg inside the whisperx Docker image — every frame of every gap of every source runs in ONE container for the whole stage (not one container per frame: container-startup overhead, ~0.82s/run, otherwise dwarfs the ~30ms of actual ffmpeg work per frame; measured 2.47s -> 0.85s for 3 frames), no host ffmpeg dependency. `gap_context.describe.describe_gap()` makes one vision-LLM call per gap — frames as base64 `image_url` parts, a plain-text (non-JSON) response, empty response is a retry-then-drop failure — and the LLM report records frame **paths**, never base64 payloads. The neighbouring transcript lines quoted to the vision LLM are `gap_context.context_lines` per side (default 1 = the original fixed one-per-side wording, byte-identical; 0 omits them), collected by `run._neighbour_lines()` and rendered singular or as a chronological bullet list by `describe._neighbour_text()`. Output `{stem}_gaps.json` (`{"gaps":[{start,end,frames,description,static}]}`, purely time-based, no line numbers baked in) is a hand-editable intermediate; reading is lenient (`gaps.gaps_from_dict`/`load_gaps`), a malformed entry is dropped rather than raised.

`gap_context/context.py` is shared, pure rendering logic consumed by both downstream stages, so summary and director share one anchoring rule and one annotation format without importing each other. `anchor_gaps()` attaches each gap to the 1-based transcript line it follows (last qualifying line wins, not first; `0` = before line 1; `len(lines)` = after the final line), using an epsilon to absorb float rounding at the boundary. `summary` (`run_summary(..., gaps_paths=...)`) renders a `## Silent gaps (visual context)` block appended to each video's `segment_video()` prompt via `format_gap_block()`. `director` (`run_director(..., gaps=...)`) inserts indented, un-numbered `[silent gap N.Ns: description]` lines into the timed numbered transcript via `annotate_numbered_transcript()` — the default `DIRECTOR_PROMPT` documents this rendering and instructs the director to rescue a gap worth watching with a `keep` op over `[N, N+1]`, and never to reference an annotation line as an op line. Both consumers degrade to byte-identical prompts when gaps are absent/empty (stage disabled, or a video with no long gaps).

## Real-run review fixes (water_pump_3, 2026-07-16)

**Status: complete.** A review of a real 3-video project run surfaced a pipeline crash and several robustness/observability gaps; all fixed with regression tests:

- **`<cut>` whitespace crash (intervals):** the text_filter LLM had left a trailing space after a `{{old->}}` patch; a director `cut` spanning that line folded the space *inside* the desugared deletion patch (`sync_json._expand_cut_tags`), whose old side then no longer matched the stripped original segment — `sync_text_to_json` raised `ValueError: Segment 93 ...` and the run died at intervals. Edge whitespace now stays outside the emitted `{{old->}}`; whitespace-only wrapped text emits no patch.
- **`check_edits` parity guard:** the checker had said "no problems found" on the exact file the intervals stage crashed on (its per-line checks can't reproduce `_expand_cut_tags`'s cross-line state). After all itemised checks pass, it now dry-runs the real `sync_text_to_json` and reports a rejection as a line-numbered Problem.
- **cut-op overlap guard (guided_edit):** the director emitted `cut [81,96]` + `keep [96,96]` ("keep only the conclusion at 96"); both applied, and the cut swallowed the keep. Span ops now apply cut-last; a `cut` op clips against lines under ANY existing tag and every op clips against existing `<cut>` spans (`apply.blocked_lines`) — the real-world pair now resolves to `cut [81,95]` + intact keep. `DIRECTOR_PROMPT` gained an explicit no-overlap rule for cut ranges.
- **gap_context static-gap filter:** ~75% of a tripod-footage run's vision descriptions were "static, nothing changes", bloating the summary/director prompts. The vision LLM now prefixes `ACTION:`/`STATIC:`; the marker becomes a lenient `static` boolean in `{stem}_gaps.json` (absent/non-boolean → false) and `anchor_gaps()` skips static gaps for both consumers. Hand-flip `"static": false` to force one back in.
- **`intervals.min_cut` (micro jump cuts):** nothing constrained the *gaps* between keep intervals, only the intervals themselves — so keep/caption margins eating an exclude gap from both sides left millisecond cuts that jump the picture and save nothing (43 of 341 gaps under 0.3s in one real source). A final `merge_close_intervals` pass (after `min_keep`, the only position where the measured gap is the final gap) absorbs any gap below `intervals.min_cut`, default 0.4s, `0` disables. Replaying the three real sources: 80 cuts absorbed for +15.9s across 90min of kept footage.
- **timing brackets:** `format_dur_gap` omits a gap that would render `0.0s` (the plan input showed `gap 0.0s` on every contiguous part); both default prompts document the omission.
- **LLM report:** `duration_ms` was always 0 (start time was taken at first `attempt()`, i.e. after the call) — `Recorder.begin(unit)` now marks the real start in every stage; `text_filter` report units are stem-prefixed so multi-video runs no longer overwrite each other's `lines_N-M` report files.

## water_pump_3 review improvements, batch 2 (2026-07-27)

**Status: complete** (design/plan in `.superpowers/sdd/2026-07-17-water-pump-3-review-improvements/`, Tasks 1–10). `intervals.min_cut` (Task 1) shipped earlier and is documented above; the remaining eight tasks:

- **text_filter prompt (Task 2):** the default prompt gained an explicit repeated-phrase rule ("keep the later occurrence, delete the earlier one with a marker, never rewrite bare") plus a worked example (`{{映ってる->}}映ってるね`) — real-project runs showed the LLM otherwise deduplicating repeated phrases as an unmarked rewrite, which the marker safety check then has to discard wholesale.
- **`llm_report` honest rendering (Task 3):** guided_edit's span ops (`cut`/`speed`/`overlay`/`keep`) are applied deterministically with no LLM call; their report entries now render as `deterministic — <outcome>` instead of a fabricated "Attempt 1/1 — temperature 0.1" line, and contribute no model name to the unit's front-matter.
- **Blender tail-clamp logging (Task 4):** the pre-existing 1-frame tail overshoot from sec→frame rounding (harmless, always clamped) now logs at DEBUG instead of WARNING, via a new pure helper `blender/frames.py::clamp_frames` — stops routine runs from reporting a false-alarm warning on every clip.
- **Quieter LiteLLM/httpx logging (Task 5):** `setup_logging` now caps the `LiteLLM*`/`httpx` loggers at WARNING and drops the known-noise "Proxy Server is not installed" log record, unless the root logger is set to DEBUG (`general.log_level: DEBUG` restores full chatter).
- **`timing.py` internal-silence helpers (Task 6):** new pure helpers `span_silence()`/`segment_silences()` compute how much of a `[start,end]` span is covered by audio_silence cut ranges; `format_dur_gap()` gained an optional third `silence` parameter that renders a speech/silence split bracket, e.g. `[13.0s speech, 62.9s silence]`, instead of the plain duration.
- **`director` speech/silence brackets (Task 7):** the orchestrator now passes the audio_silence `{stem}_cuts.txt` to `run_director` as `cuts_txt`; when present, each numbered transcript line's duration bracket splits into speech vs. silence via `timing.segment_silences`, so the director judges pacing from actual speech time rather than a span that's mostly already-cut silence. Absent `cuts_txt` leaves brackets byte-identical to before.
- **`summary`/`plan` speech/silence brackets (Task 8):** `run_summary` now also takes `cuts_paths` (per-source `{stem}_cuts.txt`); `build_summary()` computes each part's silence coverage (`timing.span_silence`) and persists it as an optional `"silence"` field on that part in `summary.json` (only set when > 0). `plan`'s per-part context bracket switches to the speech/silence split form when a part carries this field. Older `summary.json` files without `silence` still load and render the plain bracket.
- **`gap_context.static_ssim` prefilter (Task 9):** a real production run showed ~40% of gap_context's vision calls returning `STATIC:` — wasted LLM cost on footage where the picture never changes. Since the gap's frames are already extracted, one ffmpeg SSIM comparison of the first vs. last frame (appended to the same batch container that does frame extraction) now runs first; a gap scoring at least `gap_context.static_ssim` is written straight to `{stem}_gaps.json` as `static: true` with a placeholder description, and the vision LLM is never called for it. Calibrated against real water_pump_3 footage: the initial proposed default (0.99) turned out inert (max observed static-gap score was 0.9763, so nothing ever crossed 0.99); shipped default is **0.95** (comfortably above the max observed action-gap score of 0.9416). `static_ssim: 0` disables the prefilter end-to-end, including its ffmpeg job planning (not just its consumption).

## gap_context SSIM prefilter: three-frame comparison (2026-07-28)

**Status: complete.** Task 9's prefilter only compared `frame_entries[0]` vs `frame_entries[-1]` (first vs. last), ignoring the already-extracted, already-paid-for middle frame — a camera that pans away and returns to the same framing scores high on first-vs-last and gets wrongly skipped as static, despite real on-screen action the middle frame would have shown. Fixed by comparing every **consecutive** frame pair (`zip(frame_entries, frame_entries[1:])`; a 3-frame gap yields 2 pairs, a 2-frame gap yields 1 — identical to the old behavior when there is no middle frame, a 1-frame gap yields none) and taking the **min** across a gap's pair scores: `min(a,b) >= T` is exactly `(a>=T) AND (b>=T)`, so a gap is judged static only if it holds still throughout; the cost asymmetry (a false negative just spends one vision call, a false positive silently drops context) also favors `min` over `max`. `snapshot.ssim_relpath` gained a `pair_index` arg to disambiguate the now-multiple stats files per gap; `_extract_gap_frames` tracks `ssim_hosts: list[Path]` (was a single optional path) and reads back every stats file that exists, dropping unparseable/missing ones and taking the min of survivors (never fatal on a partial batch failure). Re-calibrated against the same water_pump_3 corpus (108 pair comparisons across 54 gaps, all 3-frame): the classes still don't cleanly separate on the dominant handheld source (static as low as 0.6975, action as high as 0.9445), so the new default is **`0.96`** — a ~0.015 margin above the measured action max, more conservative than Task 9's thinner ~0.008 margin given the noisier per-pair metric. See `docs/stages/gap_context.md`'s calibration note for the full numbers.
