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
