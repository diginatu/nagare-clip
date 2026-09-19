# gap_context — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#gap_context--silent-gap-visual-context).

## Purpose

Transcript-based stages (`summary`, `director`) only ever see spoken text, so a
long silent span — a demo running, code being typed, a result appearing, or
just dead air — is invisible to them. `gap_context` runs once per video,
between `sentence_split` and `summary`, and snapshots+describes those long
silences with a vision LLM so the two consumers can act on what's *shown*, not
just what's *said*. Disabled by default (`gap_context.enabled: false`):
writes `{"gaps": []}` per source, no Docker calls, and both consumers render
byte-identical to before this stage existed.

## Gap selection (`snapshot.select_gaps`)

`select_gaps(ranges, min_gap)` keeps only silent spans at least `min_gap`
seconds long (config `gap_context.min_gap`, default 3.0s), sorted by start.
The pipeline adapter (`pipeline/stages.py::_gap_context_run`) feeds it
`read_cuts(audio_silence/{stem}_cuts.txt)` — the same human-editable cut list
`sentence_split`'s force-split reads. Because it's the same file, **deleting a
line from `_cuts.txt` both keeps that span's audio *and* removes it from
gap-context snapshotting** — there is nothing separate to edit.

## Frame sampling (`snapshot.frame_times`)

`frame_times(start, end)` returns up to 3 timestamps inside the span, in
chronological order: `start + 0.2s`, the midpoint, and `end - 0.2s`. The
0.2s inset (`_INSET`) keeps frames off the exact boundary, where the
previous/next spoken word may still be on screen (and WhisperX often
stretches a word across the pause edge).

- Times are rounded to milliseconds and de-duplicated in order, so a
  near-instant span collapses to one or two frames instead of three
  duplicates.
- **Early-return for spans too short to inset:** if the inset start
  (`start + 0.2`) would land *after* the inset end (`end - 0.2`) — i.e. the
  span is under ~0.4s — the function returns `[mid]` only, skipping the inset
  computation and the general candidate/dedup loop entirely. This guarantees
  every gap gets at least one frame even when it's barely long enough to
  qualify past `min_gap` after the cuts file has been hand-edited.
- Otherwise the three candidates are filtered to `start <= t <= end`,
  de-duplicated preserving first-seen order, then returned sorted (they're
  already chronological in practice, since `start_inset < mid < end_inset`
  once the early-return doesn't fire).

`frame_relpath(stem, t)` builds the on-disk-relative path
`frames/{stem}/{t:.3f}.jpg` (three decimal places, matching the millisecond
rounding above) — this is both the frame's location under the stage dir and
the string recorded in `{stem}_gaps.json`'s `frames` array.

## Extraction (`pipeline/stages.py::_extract_gap_frames`, `pipeline/external.py::build_snapshot_batch_cmd`)

**One `docker compose run` for the ENTIRE stage** — every frame of every gap
of every source in the whole pipeline invocation, not one container per
frame and not even one per source. This mirrors the transcription stage's
"single Docker container for all source files" precedent.

Why: a `docker compose run` pays ~0.82s of container + nvidia-runtime init
(`docker-compose.yml` reserves a GPU) regardless of how little work it does
inside; the actual ffmpeg snapshot is ~30ms. Measured on real media: 3
frames as 3 separate containers took 2.47s; the same 3 frames batched into
ONE container took 0.85s, producing byte-identical JPEGs. Under the old
one-container-per-frame design, a 40-gap video (120 frames) burned ~98s in
pure container startup before this change.

`_extract_gap_frames(ctx, gaps_by_source)` takes **every** source's selected
gaps at once (`gaps_by_source: list[tuple[SourceMedia, list[tuple[float,
float]]]]`) and:

1. Builds one `(relative, time_s, out_container_path)` job per `frame_times()`
   timestamp across all sources/gaps, `mkdir`-ing every frame's parent
   directory up front.
2. If there is at least one job, issues **exactly one** `run_command()` call
   — `build_snapshot_batch_cmd()` renders every job as its own `ffmpeg ...
   || true` line in a shell script, run via `--entrypoint sh whisperx -c
   "<script>"` inside the whisperx image. Each line keeps the same ffmpeg
   flags as before (input-side `-ss` fast seek, `-frames:v 1`,
   `scale={width}:-2`, `-q:v 4`, writing to
   `/output/gap_context/frames/{stem}/{t:.3f}.jpg`), plus `-nostdin` since
   many ffmpeg invocations now share one shell. **Zero jobs means zero
   docker calls** — an empty batch never runs an empty/no-op container.
   The `|| true` on each line means one bad seek can't take down the rest of
   the batch; the whole `run_command()` call is still wrapped in a broad
   `except Exception` (a missing/failed batch — docker gone, image gone —
   must never abort the run; every gap in that case simply gets zero
   frames).
3. Regroups the single batch's results back into per-source, per-gap
   `GapFrames`, checking `host_path.is_file()` for each job the same way the
   old per-frame code did — a frame that never landed (bad seek, or the
   whole batch call raised) is skipped, never fatal. A gap that ends up with
   zero readable frames is dropped entirely (logged) before it ever reaches
   `describe_gap`.

The result is `dict[str, list[GapFrames]]` keyed by source stem; `frame_relpath`
already namespaces every path by stem (`frames/{stem}/{t:.3f}.jpg`), so
batching multiple sources' frames into one container run cannot collide them.
`_gap_context_run()` computes `select_gaps()` for every source first, calls
`_extract_gap_frames()` once, then loops sources calling `run_gap_context()`
with `frames_by_stem.get(src.stem, [])` — unchanged from before except that
the frame extraction itself now happens once, up front, instead of once per
source inside the loop.

## Pixel-static prefilter (`static_ssim`)

A real production run showed 23/54 vision calls returning `STATIC:` — nearly
40% of the stage's LLM cost spent describing scenes where nothing visually
changes. Since the frames already exist on disk by the time `describe_gap`
would be called, cheap ffmpeg SSIM comparisons between a gap's extracted
frames can catch the pixel-static subset of those and skip the vision call
entirely — **in the same batch container** as frame extraction, per the
"one `docker compose run` for the whole stage" invariant above.

The comparison is over every **consecutive** pair of a gap's extracted
frames, not just the first and last. An earlier version compared only
first-vs-last, which false-positives on a camera that pans away and returns
to the same framing: both endpoints look alike, so the pair scores high,
even though the middle frame — already extracted, already paid for — shows
real on-screen action in between. Taking the **minimum** across a gap's
consecutive-pair scores fixes this: `min(a, b) >= T` is exactly `(a >= T)
AND (b >= T)`, i.e. the gap is judged static only if it holds still
*throughout*, not just at the two ends. The cost asymmetry also favors
`min` over `max` (which would be an OR, wrongly skipping a gap whose first
half is frozen and second half has action): a false negative merely spends
one extra vision call, while a false positive silently drops visual context
the director/summary stages never see.

- `snapshot.ssim_relpath(stem, start, end, pair_index)` names the stats-file
  path for one pair (`frames/{stem}/ssim_{start:.3f}-{end:.3f}_{pair_index}.txt`,
  relative to the stage dir, mirroring `frame_relpath`'s `{stem}` namespacing
  so two sources' stats files can never collide). The `pair_index` arg
  disambiguates the multiple stats files a single gap can now have (a
  3-frame gap has 2 consecutive pairs: 0 = first-vs-mid, 1 = mid-vs-last).
- `_extract_gap_frames` plans one SSIM job per **consecutive pair** of a
  gap's extracted frame entries (`zip(frame_entries, frame_entries[1:])`) —
  a gap that collapsed to a single frame (see `frame_times`'s short-span
  case) has no pair to compare at all, so no job is planned and
  `GapFrames.ssim` stays `None`; a 2-frame gap gets exactly 1 job (identical
  to the old first-vs-last behavior, since there IS no middle frame); a
  3-frame gap gets 2 jobs — **and only when `gap_context.static_ssim` is
  above `0`** — `_extract_gap_frames` reads its own `ssim_threshold =
  float(ctx.cfg["gap_context"].get("static_ssim", 0.0))`; unlike
  `frame_width` (`ctx.cfg["gap_context"]["frame_width"]`) and `min_gap`
  (`g["min_gap"]`), which are read via direct subscript, `static_ssim` is
  the only gap_context key read with `.get`-and-default — and gates
  planning on `ssim_threshold > 0.0`, so `static_ssim: 0` genuinely disables
  the prefilter end-to-end: no ssim job, no extra ffmpeg line in the batch
  script, no stats file ever written. (Previously only `run_gap_context`'s
  consumption-side threshold check gated the *result*, so the ffmpeg
  comparison and stats-file write still happened even at `static_ssim: 0` —
  fixed in the Task 9 review round.) Each job is `(frame_a_container_path,
  frame_b_container_path, stats_container_path)`, appended to `ssim_jobs`
  and passed to `build_snapshot_batch_cmd(..., ssim_jobs=...)`. The per-gap
  stats paths are tracked as `ssim_hosts: list[Path]` (one per pair), not a
  single optional path.
- `build_snapshot_batch_cmd` appends one `ffmpeg ... -filter_complex
  ssim=stats_file=<path> -f null - || true` line per pair **after all
  extraction lines** for the whole batch — ordering matters, since a
  comparison line needs both its frames to already exist on disk. The
  builder itself is generic over `ssim_jobs`' tuples and needed no logic
  change for the multi-pair-per-gap case. `ssim_jobs=()` (the default)
  leaves the command byte-identical to before this feature
  (regression-guarded in `tests/pipeline/test_external.py`).
- Back on the host, `_extract_gap_frames`'s result loop reads every stats
  file for the gap that exists (`ssim_host.is_file()`), parses each with
  `snapshot.parse_ssim_stats` (extracts the `All:` score from ffmpeg's
  `ssim` filter stats-file line, `n:1 Y:... U:... V:... All:0.996132
  (24.12)`), drops any missing/unparseable one, and sets `GapFrames.ssim` to
  the `min` of whatever survived (`None` if nothing did). A partially-failed
  batch (one pair's comparison failed, `|| true` swallowed it, but the
  other's stats file is fine) degrades gracefully to the min of the
  survivors — never an exception.
- `run_gap_context` hoists `threshold = float(gc_cfg.get("static_ssim",
  0.0))` once per source, then for each gap: if `threshold > 0.0 and
  gf.ssim is not None and gf.ssim >= threshold`, the gap is written straight
  to `{stem}_gaps.json` as `static: true` with a description containing
  `"prefilter"` (e.g. `"static (prefilter: frames nearly identical, ssim
  0.996)"`) — **no vision call is made**. The report still gets a unit
  (`recorder.begin()` + `recorder.flush_unit(..., outcome=OK, reason="static
  prefilter (ssim 0.9961)")` with zero attempts recorded), so a skipped gap
  is visible in the LLM report/index rather than silently absent.
  `static_ssim: 0` (or omitted from an old config) disables the prefilter
  entirely — `threshold > 0.0` is false, so every gap falls through to the
  normal `describe_gap` call, byte-identical to before this feature.
- Pixel-static (SSIM-measured) is a **subset** of the vision LLM's semantic
  `STATIC:` judgment (a scene can visually hold still while something
  narratively "happens" off-camera, or vice versa) — below-threshold gaps
  still go through the normal ACTION:/STATIC: marker path unchanged. Hand-
  flipping `"static": false` in `{stem}_gaps.json` forces a prefiltered gap
  back into the summary/director prompts exactly as it does for an
  LLM-judged static gap.
- The pan-away-and-return false positive described above is now
  **mitigated** by the consecutive-pair min (previously an open risk when
  the comparison was first-vs-last only): a low-scoring first-vs-mid pair
  drags the gap's overall score down even when mid-vs-last looks nearly
  identical (camera settled back to the same framing). The prefilter can
  still miss action that happens strictly *between* two adjacent sampled
  frames without ever showing at a sample point. Note that the
  consecutive-pair min does **not** strictly dominate the old
  endpoints-only comparison: the two miss different things. Endpoints-only
  catches slow continuous drift (a gradual pan moves the endpoints apart)
  but misses pan-away-and-return; the consecutive-pair min is the reverse,
  since each adjacent step of a gradual pan looks similar. The calibration
  below measures this directly — the action-gap maximum *rose* from 0.9416
  (endpoints-only) to 0.9445 (consecutive-pair min), i.e. at least one
  action gap became easier to misclassify as static under the new metric.
  Taking the min over all three pairs (including first-vs-last) would
  dominate both schemes at the cost of one more comparison per gap; it was
  deliberately not adopted, and the `static_ssim` margin absorbs the
  difference.
- **Calibration note (re-derived for the three-frame/min-of-pairs metric):**
  measured against the same water_pump_3 corpus's real `_gaps.json`
  static/action verdicts (handheld phone-camera footage), 108 consecutive-
  frame-pair SSIM comparisons across 54 gaps (32 static, 22 action, all with
  3 extracted frames) were computed and reduced to a per-gap min. The two
  classes still do **not** cleanly separate on the dominant (handheld)
  source — camera micro-motion depresses SSIM regardless of narrative
  content, so sampled `static: true` gaps scored as low as 0.6975 and
  sampled `static: false` (action) gaps scored as high as 0.9445 on that
  source; a second, steadier source in the same corpus separated far more
  cleanly (action max 0.9139, static min 0.9609). The measured max/min
  across ALL sampled gaps: action max = **0.9445**, static max = 0.9772,
  static min = 0.6975. No true action gap scored above 0.9445, so the
  default is **`0.96`**: a real margin (~0.015) above the sampled action
  max — a bit more conservative than the previous first-vs-last default's
  thin ~0.008 margin, since a razor-thin margin risks flipping on
  measurement noise given the classes don't separate on this corpus's
  dominant source. At `0.96`, zero sampled action gaps false-positive and
  11/32 (34%) of sampled static gaps are correctly caught (vs. 16/32 at a
  thinner `0.95` margin, or 18/32 at `0.945` where the margin all but
  vanishes). This prefilter mainly pays off on steadier/locked-off footage;
  on heavily handheld footage most true statics simply fall through to the
  normal (safe, just costs one vision call) `describe_gap` path. See the
  three-frame-comparison follow-up report for the full measured numbers.

## Vision call (`describe.py`)

One LLM call per gap (`describe_gap()`), not per frame:

- `build_messages(gf, cfg, before=, after=)` is the **single** place the
  system+user message list is assembled, and `describe_gap` calls it directly
  (it used to duplicate the construction inline, so `build_messages`'s own
  tests never exercised production — fixed). It returns `(messages,
  relpaths)`: the relpaths are the subset of `gf.relpaths` whose frame
  actually made it into the user message (see below), which `describe_gap`
  needs to record on the returned `Gap` and in the LLM report.
- Each surviving frame is read from disk and base64-encoded into a
  `data:<mime>;base64,...` `image_url` content part (`_image_part()`); a frame
  that can no longer be read at this point (e.g. deleted between extraction
  and description) is dropped from that call, not fatal to the gap unless
  every frame drops (`describe_gap` returns `None` if `build_messages` yields
  an empty user-content list — logged, zero LLM calls made).
- The user message is one multimodal turn: a text header
  (`Silent gap: {start}s - {end}s ({duration}s long).`, frame count, and the
  optional neighbour-line block described below when the caller supplies
  neighbour text) followed by the image parts, in chronological order.
- `before`/`after` are **lists** of neighbour lines in transcript order.
  `_neighbour_text()` renders one line per side as the singular
  `Spoken line before the gap: <text>` — byte-identical to the wording from
  before `context_lines` existed, which is what the default `context_lines: 1`
  emits — and several as a plural chronological bullet list:

  ```
  Spoken lines before the gap:
  - 前2
  - 前1
  ```

  An empty list omits that side's block entirely (`context_lines: 0` omits
  both).
- The system prompt is `gap_context.prompt` (default `GAP_CONTEXT_PROMPT` in
  `config.py`) — plain English, asks for one or two sentences of plain text,
  explicitly no JSON/markdown, and to say so plainly if nothing is happening.
  Models do not always comply (a multi-line or markdown-bullet reply is
  realistic), so the response is never trusted verbatim — see the whitespace
  rule below.
- The response is **plain text**, not JSON — there is no schema to fail
  parsing. The default prompt asks the model to start the reply with
  `ACTION: ` (something meaningful happens) or `STATIC: ` (essentially dead
  air); `describe_gap` strips that leading marker (case-insensitive,
  full-width colon tolerated, `_MARKER_RE`) into the `Gap.static` boolean — a
  missing marker is treated as ACTION, so only an explicit STATIC excludes the
  gap downstream. `describe_gap` collapses the reply with
  `" ".join(response.split())`
  (strips *and* folds every internal whitespace run, including newlines, to a
  single space) before treating it as the description; an empty/whitespace-only
  result after collapsing (including a bare `STATIC:` with nothing after it)
  counts as a failure and is retried
  (`llm_report.UNPARSEABLE`), same retry ladder as every other LLM stage
  (`llm_retry.retry_attempts`/`cfg_for_attempt`, config
  `gap_context.max_retries`/`retry_temp_step`/`retry_temp_cap`). All attempts
  failing drops the gap (logged), not fatal to the run. The collapse matters
  beyond cosmetics: an un-collapsed multi-line reply could inject a line
  starting with `N: ` into the director's numbered transcript once spliced in
  by `annotate_numbered_transcript`, which would look like a real transcript
  line to the director.
- **The LLM report records frame PATHS, never base64.** `_report_messages()`
  flattens the recorded user message to the same header text plus a
  `Frames:\n- <relpath>` list — the base64 data-URI payloads never reach
  `output/llm_report/`, keeping report files small and diffable
  (`tests/gap_context/test_describe.py::test_describe_gap_recorder_excludes_base64_payloads`
  pins this).

`run_gap_context()` (`run.py`) is the per-video entry point: for each
`GapFrames` it looks up the last `gap_context.context_lines` WhisperX segments
ending at/before the gap and the first `context_lines` starting at/after it
(`_neighbour_lines()` — tolerant of missing/`None` `start`/`end`, non-dict
segments, non-string/blank `text`, and a `context_lines` larger than the number
of segments actually there) and passes their text to `describe_gap()` as
`before`/`after`, when the sentence_split `{stem}.json` (`json_path`) is
available. `context_lines` defaults to **1** (the pre-existing behaviour: one
line per side); `0` skips neighbour lookup entirely. Raising it costs prompt
tokens on every gap of every video, so it is a knob, not a default. Note the
same-named `guided_edit.context_lines` is an unrelated knob for a different
stage. Disabled →
`{"gaps": []}` with no Docker calls: the pipeline adapter (`_gap_context_run`)
only calls `_extract_gap_frames` when `gap_context.enabled` is true, and
`run_gap_context` itself independently checks `gc_cfg.get("enabled", False)`
before describing anything it's handed — so even a caller that passes a
non-empty `gap_frames` list with `enabled: false` still gets `{"gaps": []}`
and zero LLM calls (belt-and-braces; pinned by
`tests/gap_context/test_run.py::test_disabled_ignores_a_nonempty_gap_frames_list_and_makes_zero_llm_calls`,
which uses a call counter rather than a raising fake — `describe_gap`'s broad
`except Exception` would otherwise swallow the raise).

## Output contract (`gaps.py`)

`{stem}_gaps.json`: `{"gaps": [{"start", "end", "frames": [...], "description", "static"}]}` —
purely time-based, no line numbers baked in (consumers anchor to line numbers
themselves, since the transcript can change between stages). `gaps_to_dict`/
`gaps_from_dict` round-trip it; reading is lenient by design so a
hand-edited file can never break the pipeline:

- A non-dict entry, or `"gaps"` not a list, is dropped/ignored (logged).
- `start`/`end` must coerce to `float` — `bool` is explicitly rejected (not
  silently coerced to `1.0`/`0.0`) — and `end > start` strictly, else the
  entry is dropped.
- `description` must be a non-empty (after `.strip()`) string, else dropped.
  This is the field a human is expected to hand-author/correct, so a blank
  one is treated as "not really described yet." A description that survives
  is then **whitespace-collapsed** (`" ".join(description.split())`, same
  rule `describe_gap` applies to the raw LLM response) — a hand-edit that adds
  a line break or a markdown bullet inside the description is silently
  normalized to one line rather than being allowed to inject a line into a
  downstream numbered transcript that looks like a real `N: ...` transcript
  line (`context.annotate_numbered_transcript`'s un-numbered-annotation
  invariant depends on this).
- `frames` is optional (defaults to `[]`) and any non-string entries in it are
  filtered out silently, rather than dropping the whole gap.
- `static` reads leniently: only JSON `true` counts; absent (pre-static
  files) or any non-boolean value → `false`. This is the second field a human
  is expected to hand-edit — flipping `"static": false` forces a gap the
  vision LLM judged static back into the summary/director prompts.

`load_gaps(path)` wraps `gaps_from_dict` with file I/O: a missing path (or
`None`), unreadable file, or invalid JSON all degrade to `[]` (logged), never
raise.

## Consumption

`context.py` is shared, pure rendering logic — it lives outside both `summary`
and `director` so the two stages don't need to import each other and so there
is exactly one anchoring rule and one annotation format.

**Anchoring (`anchor_gaps`)** attaches each `Gap` to the 1-based transcript
line it immediately follows, given that video's `seg_times` (from
`timing.segment_times`). Gaps with `static: true` are **skipped entirely
here** — anchoring is the single entry point both consumers share, so one
filter keeps "camera pointed at nothing changing" scenes out of both the
summary block and the director annotation (in a real tripod-footage run,
roughly three quarters of described gaps were static noise). Details:

- For each gap, scan every line's `(start, end)`; whenever a line's `end` is
  not `None` and `end <= gap.start + _EPS` (`_EPS = 0.01`), advance the anchor
  to that line's 1-based index. Because the scan runs over **every** line
  rather than stopping at the first match, the anchor ends up as the **last**
  qualifying line, not the first — a regression the tests pin explicitly
  (`test_anchor_gaps_picks_the_last_qualifying_line_not_first`).
- The `_EPS` epsilon absorbs float rounding at the boundary — a line ending
  at `10.005` still anchors a gap starting at `10.0`.
- `anchor == 0` means the gap precedes every line ("before line 1"); this is
  also what an empty `seg_times` list produces (no line ever qualifies).
- `anchor == len(lines)` means the gap follows the transcript's final line
  (trailing silence) — `annotate_numbered_transcript` appends it after the
  last line rather than dropping it.

**`summary`'s `## Silent gaps (visual context)` block (`format_gap_block`)**:
one bullet per anchored gap, `- after line N (Xs-Ys, Ds): description` (or
`- before line 1 (...)` for anchor 0); an empty list renders as `""` (not
even the header), so the block is omitted from the prompt entirely rather
than appended empty. `summary/run.py` builds one block per stem
(`gap_blocks_by_stem`) from that source's `{stem}_gaps.json` (via the
orchestrator's `gaps_paths` argument) and `summarize.segment_video()` appends
it to that video's user content — `if gap_block:` — so an absent/empty block
(disabled stage, or a video with no long gaps) leaves the prompt
byte-identical to before this feature existed.

**`director`'s indented, un-numbered annotation (`annotate_numbered_transcript`)**:
inserts `    [silent gap {duration:.1f}s: {description}]` lines (four-space
indent, `_INDENT`) into the numbered transcript, immediately after the
anchored line (or before line 1, for anchor 0; or after the last line, for
`anchor == len(lines)`). Annotation lines are **deliberately not numbered** —
the director's op `"lines"` references must stay unambiguous, so an
annotation can never itself become a line an op targets. Multiple gaps
anchored to the same line all appear, in order. An out-of-range anchor
(outside `0..len(lines)`) is silently ignored. An empty `anchored` list
returns the transcript **unchanged** (not even a no-op copy — same string
object semantics as byte-identical). `director/director_llm.py`'s
`generate_director_ops()` only calls this when **both** `seg_times` and
`gaps` are non-empty/present (gaps need anchor times, and the timed-transcript
path itself requires `seg_times` to match `clean_lines` in length) — so a
video without timing data, or with `gap_context` disabled (`gaps=[]` from
`load_gaps` on a missing/empty file), renders byte-identical to before this
feature. `director/run.py` wires this: `gaps=Path | None` →
`load_gaps(gaps)` (already `None`-safe) → `generate_director_ops(...,
gaps=gap_list)`.

The default `DIRECTOR_PROMPT` (`config.py`) documents the annotation's exact
rendering (`    [silent gap: a build runs and logs scroll past]`) and
the rule the director is meant to act on: such gaps are dropped by default
(same as any other non-speech gap), and if the visual content is worth
watching, the director should emit a `keep` op spanning the annotated line and
the next one — **a `keep` over lines `[N, N+1]` preserves the silence between
them** — with an explicit reminder that annotation lines are never valid `op`
line references. `tests/test_config.py::test_director_prompt_gap_example_matches_the_real_formatter`
pins that documented line to `annotate_numbered_transcript`'s actual output
for the equivalent `Gap`, not just to a substring match, so a rendering change
(indent width, decimal places, wording) fails loudly instead of the prompt
silently drifting from reality.

`[N, N+1]` is the shape for rescuing **one** gap, not the shape of every keep.
When the described action is still unfolding in the next annotation (or the
speech around it narrates the same event), the prompt asks for the whole run in
one `keep` instead of one narrow keep per gap — a real run split the best moment
in the project, water spilling and the cleanup after it, across two surviving
cuts because each gap was rescued separately.

A ceiling still applies: `director.max_keep_lines` (default 8) drops a wider
`keep` the LLM emits, because a `keep` restores every silent second in its range
and a keep stretched over *talking* inflates the finished runtime without adding
any speech (speech is never dropped by default). A continuous on-screen event
fits well inside the limit, precisely because nobody is talking through it.

## Config (`GapContextConfig` in `config.py`)

`enabled` (default `false`), `provider`/`api_base`/`model`
(default `qwen2.5vl:7b` — must be a **vision-capable** model)/`api_key`/
`temperature`/`thinking`/`timeout`/`max_retries`/`retry_temp_step`/
`retry_temp_cap` follow the same shape as every other LLM stage block.
Stage-specific: `min_gap` (seconds, default 3.0 — same default as
`sentence_split.force_split_min_silence`, though the two are independent
knobs over the same `_cuts.txt`), `frame_width` (px, default 960, passed
straight to ffmpeg's `scale` filter), and `static_ssim` (default `0.96`, `0`
disables — the pixel-static prefilter threshold; see the section above).
`prompt` is `_commented` (has a
sensible default, `GAP_CONTEXT_PROMPT`, documented rather than repeated in
`config.example.yml`).

## Degradation table

| Condition | Result |
|---|---|
| One frame fails to extract (ffmpeg error, or file missing after) | That frame is skipped; the gap proceeds with its remaining frames |
| A gap ends up with zero extracted/readable frames | The gap is dropped (logged); no LLM call is made for it |
| The vision LLM call errors, or returns empty text, on every retry | The gap is dropped (logged); `run_gap_context` continues with the next gap |
| `gap_context.enabled: false` | `{stem}_gaps.json` is `{"gaps": []}`; no Docker calls at all |
| `{stem}_gaps.json` absent, empty, or unreadable | `load_gaps` returns `[]`; both consumers render byte-identical to before this feature |
| A hand-edited gaps entry is malformed (bad start/end, blank description, non-string frames) | That entry is dropped (logged); the rest of the file still loads |
| The vision LLM marks a gap `STATIC:` (or a hand-edit sets `"static": true`) | The gap stays in `{stem}_gaps.json` but `anchor_gaps` skips it — invisible to summary/director |
| The vision LLM omits the ACTION:/STATIC: marker | Treated as ACTION (`static: false`); the description is used as-is |
| The min SSIM across a gap's consecutive extracted-frame pairs is >= `static_ssim` (default 0.96) | Written as `static: true` with a `(prefilter...)` description; no vision call is made |
| One (but not all) of a gap's pair-wise SSIM comparisons fails or its stats file is missing/unparseable | That pair's score is dropped; `GapFrames.ssim` is the min of the surviving pair(s) |
| Every one of a gap's pair-wise SSIM comparisons fails or is missing/unparseable | `GapFrames.ssim` stays `None`; the prefilter is simply disabled for that gap (normal vision call proceeds) |
| `static_ssim: 0` | The prefilter is disabled entirely: `_extract_gap_frames` doesn't plan the SSIM job at all (no extra ffmpeg line, no stats file), and every gap goes through the normal vision call |
