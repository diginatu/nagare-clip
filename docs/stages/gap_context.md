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

## Extraction (`pipeline/stages.py::_extract_gap_frames`, `pipeline/external.py::build_snapshot_cmd`)

One ffmpeg run **per frame** (not per gap) inside the whisperx Docker image —
the same container the transcription and audio_silence stages use, so no host
ffmpeg dependency is added. `build_snapshot_cmd()` seeks with `-ss` before
`-i` (fast, keyframe-adjacent seek), grabs exactly one frame
(`-frames:v 1`), scales to `gap_context.frame_width` px wide with the height
auto-computed (`scale={width}:-2`), and writes a JPEG (`-q:v 4`) to
`/output/gap_context/frames/{stem}/{t:.3f}.jpg` inside the container, which
lands at `output/gap_context/frames/{stem}/{t:.3f}.jpg` on the host.

`_extract_gap_frames()` runs `frame_times()` per selected gap and, for each
timestamp: runs the snapshot command, catching any exception (a failed
extraction must never abort the whole run) and logging+skipping that frame if
it raises or if the expected file doesn't exist afterward. A gap that ends up
with zero readable frames is dropped entirely (logged) before it ever reaches
`describe_gap`. The result is a list of `GapFrames` (start, end, the frames
that survived as absolute host `Path`s, and their `frames/{stem}/...`
relpaths in the same order).

## Vision call (`describe.py`)

One LLM call per gap (`describe_gap()`), not per frame:

- Each surviving frame is read from disk and base64-encoded into a
  `data:<mime>;base64,...` `image_url` content part (`_image_part()`); a frame
  that can no longer be read at this point (e.g. deleted between extraction
  and description) is dropped from that call, not fatal to the gap unless
  every frame drops (`describe_gap` returns `None` if `_content_parts` yields
  no images at all — logged, zero LLM calls made).
- The user message is one multimodal turn: a text header
  (`Silent gap: {start}s - {end}s ({duration}s long).`, frame count, and
  optional `Spoken line before/after the gap:` lines when the caller supplies
  neighbour text) followed by the image parts, in chronological order.
- The system prompt is `gap_context.prompt` (default `GAP_CONTEXT_PROMPT` in
  `config.py`) — plain English, asks for one or two sentences of plain text,
  explicitly no JSON/markdown, and to say so plainly if nothing is happening.
- The response is **plain text**, not JSON — there is no schema to fail
  parsing. An empty/whitespace-only response counts as a failure and is
  retried (`llm_report.UNPARSEABLE`), same retry ladder as every other LLM
  stage (`llm_retry.retry_attempts`/`cfg_for_attempt`, config
  `gap_context.max_retries`/`retry_temp_step`/`retry_temp_cap`). All attempts
  failing drops the gap (logged), not fatal to the run.
- **The LLM report records frame PATHS, never base64.** `_report_messages()`
  flattens the recorded user message to the same header text plus a
  `Frames:\n- <relpath>` list — the base64 data-URI payloads never reach
  `output/llm_report/`, keeping report files small and diffable
  (`tests/gap_context/test_describe.py::test_describe_gap_recorder_excludes_base64_payloads`
  pins this).

`run_gap_context()` (`run.py`) is the per-video entry point: for each
`GapFrames` it looks up the last WhisperX segment ending at/before the gap and
the first one starting at/after it (`_neighbour_lines()` — tolerant of
missing/`None` `start`/`end`, non-dict segments, and non-string `text`) and
passes their text to `describe_gap()` as `before`/`after`, when the
sentence_split `{stem}.json` (`json_path`) is available. Disabled →
`{"gaps": []}` with no Docker calls: the pipeline adapter (`_gap_context_run`)
only calls `_extract_gap_frames` when `gap_context.enabled` is true, and
`run_gap_context` itself independently checks `gc_cfg.get("enabled", False)`
before describing anything it's handed — so even a caller that passes a
non-empty `gap_frames` list with `enabled: false` still gets `{"gaps": []}`
and zero LLM calls (belt-and-braces; the disabled case itself is pinned by
`tests/gap_context/test_run.py::test_disabled_writes_an_empty_gap_list`).

## Output contract (`gaps.py`)

`{stem}_gaps.json`: `{"gaps": [{"start", "end", "frames": [...], "description"}]}` —
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
  one is treated as "not really described yet."
- `frames` is optional (defaults to `[]`) and any non-string entries in it are
  filtered out silently, rather than dropping the whole gap.

`load_gaps(path)` wraps `gaps_from_dict` with file I/O: a missing path (or
`None`), unreadable file, or invalid JSON all degrade to `[]` (logged), never
raise.

## Consumption

`context.py` is shared, pure rendering logic — it lives outside both `summary`
and `director` so the two stages don't need to import each other and so there
is exactly one anchoring rule and one annotation format.

**Anchoring (`anchor_gaps`)** attaches each `Gap` to the 1-based transcript
line it immediately follows, given that video's `seg_times` (from
`timing.segment_times`):

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
rendering (`    [silent gap 12.4s: a build runs and logs scroll past]`) and
the rule the director is meant to act on: such gaps are dropped by default
(same as any other non-speech gap), and if the visual content is worth
watching, the director should emit a `keep` op spanning the annotated line and
the next one — **a `keep` over lines `[N, N+1]` preserves the silence between
them** — with an explicit reminder that annotation lines are never valid `op`
line references.

## Config (`GapContextConfig` in `config.py`)

`enabled` (default `false`), `provider`/`api_base`/`model`
(default `qwen2.5vl:7b` — must be a **vision-capable** model)/`api_key`/
`temperature`/`thinking`/`timeout`/`max_retries`/`retry_temp_step`/
`retry_temp_cap` follow the same shape as every other LLM stage block.
Stage-specific: `min_gap` (seconds, default 3.0 — same default as
`sentence_split.force_split_min_silence`, though the two are independent
knobs over the same `_cuts.txt`) and `frame_width` (px, default 960, passed
straight to ffmpeg's `scale` filter). `prompt` is `_commented` (has a
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
