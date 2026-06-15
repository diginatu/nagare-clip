# Duration context for plan & director stages

**Date:** 2026-06-15
**Status:** Approved (pending spec review)

## Problem

The `plan` and `director` LLM stages decide editorial actions (cut / shorten /
speed / keep) but currently see no timing information. A part or a sentence that
reads short on the page may be long on the timeline, and long pauses between
parts/sentences are invisible to the LLM. We want to surface **calculated
durations** so the LLMs can reason about pacing:

- **plan stage context:** duration of each *part* and the *in-between* gap.
- **director stage context:** duration of each *sentence* (transcript line) and
  the *in-between* gap.

## Timing source

WhisperX `output/stage1/{stem}.json` holds `segments`, each with `start` / `end`
(seconds). Transcript line `N` (1-based) maps to `segments[N-1]` 1:1 (enforced by
`check_edits` line-count-vs-segments). All durations/gaps derive from these
segment times.

- **duration** of a line range `[a,b]`: `segments[b-1].end - segments[a-1].start`.
  A single sentence is `b == a`.
- **gap** ("in between"): `next.start - this.end`. Negative gaps (WhisperX
  segment overlap) are clamped to `0.0`.

## Decisions (confirmed with user)

- Director timings appear **inline in the numbered transcript** the director
  reads (its primary input), not in a separate block.
- Render format is a **compact bracket**: `[4.2s, gap 0.8s]`. The `gap` is
  omitted when there is no next item; the whole bracket is omitted when duration
  is unavailable.
- Plan in-between gaps are scoped **within the same video only**: a gap is shown
  only between consecutive parts of the same stem. The last part of a stem, and
  cross-video boundaries, show no gap.

## Architecture

**Precompute part times in `summary.json`.** The plan stage runs project-wide
and reads only `summary.json`; threading every video's JSON into it is awkward.
The summary stage already reads each video and defines the parts, so it computes
each part's `start`/`end` from the video's `{stem}.json` and stores them in
`summary.json`. Plan derives durations/gaps from those stored times with no JSON
access. The director runs per-video and already takes `--stem`, so it reads its
own `{stem}.json` directly for per-sentence timing.

### New module `src/nagare_clip/timing.py` (pure, no internal imports)

- `segment_times(json_data) -> list[tuple[Optional[float], Optional[float]]]` —
  extract `(start, end)` per segment.
- `format_dur_gap(dur: Optional[float], gap: Optional[float]) -> str` —
  - `dur is None` → `""` (no bracket).
  - `gap is None` → `"[4.2s]"`.
  - otherwise → `"[4.2s, gap 0.8s]"`.
  - negative `gap` clamped to `0.0` before formatting.
  - one decimal place (`{:.1f}`).

### Director

`src/nagare_clip/director/director_llm.py`:
- `format_numbered_transcript_timed(clean_lines, seg_times) -> str` — renders
  `"{i+1}: {text}  {bracket}"`. Per line: `dur = end - start`,
  `gap = seg_times[i+1].start - end` (last line → `gap=None`). When a segment's
  `start`/`end` is missing, that line's `dur`/`gap` is `None` (bracket degrades
  per `format_dur_gap`).
- `generate_director_ops(..., seg_times: Optional[List[Tuple]] = None)`: when
  `seg_times` is provided **and** length matches `clean_lines`, build the user
  message with the timed formatter; otherwise fall back to the existing
  `format_numbered_transcript` (**byte-identical** to today — regression
  guarded).

`src/nagare_clip/director/cli.py`:
- New `--json` arg (this video's `{stem}.json`). Load it, call
  `segment_times(...)`, pass to `generate_director_ops`. Missing/unreadable JSON
  or length mismatch → pass `None` (graceful, untimed).

### Plan

`src/nagare_clip/summary/summarize.py`:
- `PartSummary` gains `start: Optional[float] = None`, `end: Optional[float] = None`.
- `summary_to_dict`: include `"start"`/`"end"` only when not `None` (keeps output
  backward compatible / no churn when timing absent).
- `summary_from_dict`: read `start`/`end` when present (float/int), else `None`.
- `build_summary(..., seg_times_by_stem: Optional[Dict[str, List[Tuple]]] = None)`:
  after collecting parts, attach times via a helper
  `_attach_part_times(part, seg_times)` that sets
  `start = seg_times[a-1][0]`, `end = seg_times[b-1][1]` when indices are in range
  and values present. `segment_video` is left unchanged.

`src/nagare_clip/summary/cli.py`:
- New repeated `--json` arg, matched to stems by filename stem. Build
  `seg_times_by_stem` and pass to `build_summary`.

`src/nagare_clip/plan/plan_llm.py`:
- `_format_parts_for_plan`: append `format_dur_gap(dur, gap)` to each part line.
  `dur = end - start` when both present. `gap` computed only when the **next**
  part exists, has the same `stem`, and both endpoints are present; otherwise
  `gap=None`. Existing `"{i+1}: {stem} [{a}-{b}] — {summary}"` becomes
  `"{i+1}: {stem} [{a}-{b}] {bracket} — {summary}"` (bracket omitted when empty,
  no stray double space).

### Pipeline wiring (`scripts/run_pipeline.sh`)

- summary stage: add `--json "${STAGE1_DIR}/${STEM}.json"` per stem (alongside the
  existing per-stem `--edits-txt`).
- director stage: add `--json "${STAGE1_DIR}/${STEM}.json"`.
- plan stage: unchanged (reads times from `summary.json`).

## Backward compatibility

- `summary.json` without `start`/`end` loads fine (`None`); plan then shows no
  brackets — identical to today.
- Director with no `--json` (or mismatched/missing segments) produces the exact
  current untimed prompt.
- `summary_to_dict` omits `start`/`end` when `None`, so disabled/empty runs write
  the same artifact as before.

## Testing (TDD, with mutation-catch verification)

- `tests/test_timing.py`: `segment_times` extraction (incl. missing keys);
  `format_dur_gap` for all four branches + negative-gap clamp + decimal format.
- director: `format_numbered_transcript_timed` annotations (dur, gap, last-line
  no-gap, missing-time degradation); `generate_director_ops` uses timed format
  when `seg_times` given and falls back **byte-identical** when `None` /
  length-mismatch.
- plan: `_format_parts_for_plan` — same-stem gap, last-part-of-stem no gap,
  cross-video boundary no gap, missing times → no bracket.
- summary: `build_summary` attaches part `start`/`end` from `seg_times_by_stem`;
  out-of-range/missing handled; `summary_to_dict`/`summary_from_dict` round-trip
  including the `None` (omitted) case.
- CLI smoke: director `--json` and summary `--json` wiring (light).

Each new test is verified to fail against a deliberately broken implementation
(per repo TDD rule) before being declared green.

## Out of scope

- `build_director_context` (the cross-video overview parts list) is **not**
  changed. The task asks for sentence-level timing in the director, which lives
  in the numbered transcript. (Part times are now available in `summary.json` if
  we later want them there.)

## Documentation

Update `README.md`, `plan.md`, and `CLAUDE.md`/`AGENTS.md` per the project's
documentation policy when behavior changes.
