# Per-Video Summaries in the summary Stage — Design

**Date:** 2026-07-12
**Status:** Approved (pending spec review)

## Problem

The `summary` stage's reduce step (`generate_project_summary`, trace
`summary/overall`) receives only the flat numbered per-part summary list.
Video boundaries are implied solely by stem changes in the part lines, so the
overall-summary LLM has no explicit whole-video view, and downstream stages
(plan/director/text_filter) have no per-video summary to show either.

Motivating trace: Langfuse `9c1243d11111ad78c5d863cd3d21db90` (session
`20260712-192122`) — 11 parts across 3 videos fed as one flat list.

## Decision Summary

- Extend the per-video map call (`segment_video`) to also return one
  whole-video summary: a new **mandatory** `"video_summary"` field in its JSON
  response.
- Feed those per-video summaries to the overall call by restructuring its
  input into per-video groups (video header + summary, parts nested under it).
- Persist them as a new top-level `"video_summaries": {stem: "..."}` field in
  `summary.json`, and expose them to the plan, director, and text_filter
  context builders.

## Map step: `segment_video`

Response schema becomes:

```json
{"parts": [{"lines": [a, b], "summary": "..."}],
 "keywords": ["..."],
 "video_summary": "..."}
```

- The default `summary.prompt` (in `config.py`) is extended to request a
  one-to-two-sentence summary of the whole video as `video_summary`.
- **Mandatory at parse level:** `_parse_parts_response` returns `None` (hard
  parse failure → retry via `llm_retry`, same as a missing `"parts"` array)
  when `video_summary` is missing, non-string, or empty/whitespace-only. This
  keeps one validation style; the accepted trade-off is that a response with
  good parts but no `video_summary` is discarded and retried, and if all
  attempts fail the video degrades to no parts (consistent with the existing
  all-attempts-failed path).
- `segment_video` returns `(parts, keywords, video_summary)`.

## Reduce step: `generate_project_summary`

The user message changes from the flat numbered list to per-video groups.
Global 1-based part numbering is kept (stable, and costs nothing):

```
## PXL_20260324_092107933 — <video_summary>
1: [1-12] — part summary
2: [13-44] — part summary
## PXL_20260324_114817329 — <video_summary>
3: [1-20] — part summary
```

Because `video_summary` is mandatory per video, the header line always has a
summary — no empty-branch formatting. The default `overall_prompt` is updated
to mention that each `##` header carries that video's own summary.

A stem that produced parts always has a video summary (mandatory parse), so
the formatter may assume presence; a defensive fallback (header without the
`— summary` suffix) is acceptable but not a designed state.

## Schema: `summary.json` / `ProjectSummary`

- `ProjectSummary` gains `video_summaries: dict[str, str]` (default `{}`).
- `summary_to_dict` writes a top-level `"video_summaries": {stem: "..."}`.
- `summary_from_dict` reads it leniently: absent / wrong type / non-string
  values → dropped, old files → `{}`. This is backward compatibility, not
  parse leniency — old `summary.json` files must keep loading.
- Disabled stage no-op becomes
  `{"summary": "", "parts": [], "keywords": {}, "video_summaries": {}}`.

## Downstream exposure

Invariant used below: **a stem with parts always has a video summary**, but
`video_summaries` as a whole may be `{}` (stage disabled, old file). Each
builder therefore keeps a byte-identical-output guard for the empty/absent
case (regression-tested), and needs no per-video empty branch.

- **plan** (`plan_llm.py` context lines): each video's parts get a header
  line carrying the video summary above them; `video_summaries == {}` →
  context byte-identical to today.
- **director** (`director/context.py` `build_director_context()`): the
  current video's summary is added under its parts section; sibling one-line
  entries use the sibling's video summary when available (falling back to the
  existing first-part line when `video_summaries` is empty). Empty →
  byte-identical prompt.
- **text_filter** (`text_filter/context.py` `build_enhanced_prompt()`): the
  current video's summary is added above its part list. Empty →
  byte-identical prompt.

## Failure modes

| Case | Behavior |
| --- | --- |
| LLM omits/empties `video_summary` | Parse failure → retry; all attempts fail → no parts for that video (existing degrade path) |
| Old `summary.json` without the field | Loads with `video_summaries = {}`; downstream prompts byte-identical to today |
| summary stage disabled | No-op JSON includes `"video_summaries": {}` |

## Testing (TDD)

- Parse: valid `video_summary` accepted; missing / non-string / empty →
  `None` (retry), verified to fail against a lenient implementation.
- Reduce input formatting: per-video grouping, global numbering, header line.
- `summary_to_dict`/`summary_from_dict` round-trip incl. old-file absence.
- Byte-identical regression tests for plan / director / text_filter context
  builders with `video_summaries == {}`.
- Director sibling one-liners: video summary used when present.

## Documentation updates

`AGENTS.md` (summary/plan/director/text_filter overviews), `README.md`,
`plan.md`, `docs/stages/text_filter.md`.
