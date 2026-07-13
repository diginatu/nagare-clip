# gap_context stage — visual context for long silent gaps

Date: 2026-07-13
Status: draft for review

## Problem

Long silences are invisible to the text-only pipeline. They often contain
on-screen action the transcript is blind to (gameplay, demos, screen work).
Today the pipeline's only options are to cut them (audio_silence) or for a
human to rescue them manually. The summary and director LLMs have no way to
know that a 12-second silence is "a build running with logs scrolling" versus
dead air.

## Goal

A new pipeline stage extracts snapshot frames from each long silent gap, asks
a vision-capable LLM what is happening, and writes per-gap descriptions that
the `summary` and `director` stages inject into their prompts — so the
director can deliberately keep (or speed) a visually interesting gap instead
of blindly cutting it, and summaries account for silent action.

Decisions made during brainstorming:

- **Purpose:** describe silent on-screen action (keep-vs-cut signal), not
  scene-change detection or general grounding.
- **Gap source:** audio_silence `{stem}_cuts.txt` spans ≥ a configurable
  minimum (the human-editable, silencedetect-backed source of truth).
- **Frames:** up to 3 per gap (start / mid / end), all in one vision call.
- **Director use:** inline transcript annotation + explicit prompt guidance
  that a `keep` op spanning the adjacent lines rescues the gap.
- **Architecture:** standalone stage (approach A), not folded into
  audio_silence or summary.

## Stage identity

- Canonical name: `gap_context`, inserted between `sentence_split` and
  `summary` in `STAGE_NAMES` (context-building LLM stage, adjacent to its
  consumers). No stage numbers anywhere, per the naming convention.
- Package: `src/nagare_clip/gap_context/`; output: `output/gap_context/`.
- Config section `gap_context:` (pydantic model like other LLM stages):
  - `enabled: false` (default), `provider`, `model`, `api_base`, `api_key`,
    `timeout`, `temperature`, `max_retries: 2`, `thinking`, `prompt`
  - `min_gap: 3.0` — only cut spans at least this long (seconds) get
    snapshots
  - `frame_width: 960` — downscale width for extracted JPEGs
  - `config.example.yml` regenerated via `make config-example`.

## Frame extraction (orchestrator-side, Docker)

`pipeline/external.py` stays the only subprocess site (audio_silence
pattern): the pipeline adapter, not the stage `run()`, extracts frames.

1. Read `{stem}_cuts.txt` with the existing `audio_silence.cuts_file.read_cuts()`.
2. `gap_context.snapshot.select_gaps(ranges, min_gap)` (pure) keeps spans
   ≥ `min_gap`.
3. `gap_context.snapshot.frame_times(start, end)` (pure) returns up to 3
   timestamps slightly inside the span: `start+0.2`, midpoint, `end−0.2`
   (deduplicated for short spans).
4. New pure builder `external.build_snapshot_cmd(project_root, relative,
   time_s, out_relative, width)`:

   ```
   docker compose run --rm --user 0:0 --entrypoint ffmpeg whisperx \
     -hide_banner -loglevel error -ss <t> -i <src-relative> \
     -frames:v 1 -vf scale=<width>:-2 -q:v 4 \
     /output/gap_context/frames/<stem>/<t>.jpg
   ```

   Input-side `-ss` (fast seek). One container run per frame (~1 s startup
   each) is acceptable at typical gap counts; revisit only if it becomes a
   bottleneck.
5. Failed frame → log + skip. Gap with zero frames → skip (logged).

This honours the hard constraint: all media tooling goes through the
whisperx Docker image; no host ffmpeg, no new Python audio/image deps.

## Vision LLM call (stage `run()`)

`gap_context/run.py: run_gap_context(gaps_with_frames, out_json, cfg, *,
stem, recorder, json_path=None)`:

- One LLM call per gap. System = `gap_context.prompt`. User content is a
  list of parts: a text header — gap time range + duration, plus (when
  `json_path`, the sentence_split `{stem}.json`, is given) the transcript
  line spoken just before and just after the gap — followed by the frames
  as base64 `image_url` data-URI parts.
- Response is **plain text** (prompt asks for a 1–2 sentence description);
  no JSON parsing to fail. Empty/whitespace response counts as a failure.
- Retries via shared `llm_retry` (`retry_attempts()` / `cfg_for_attempt()`
  temperature nudge). All attempts fail → gap dropped (logged + recorded in
  the LLM report). LLM report: one unit per gap (e.g. `<stem>_gap03`),
  recording frame *paths*, never base64 payloads.
- `llm_client.call_llm` change: widen `messages` typing to
  `list[dict[str, Any]]`. LiteLLM already converts multimodal content parts
  per provider — no transport change, no provider-specific client.

## Output contract

`output/gap_context/{stem}_gaps.json`:

```json
{"gaps": [{"start": 12.4, "end": 18.2,
           "frames": ["frames/<stem>/12.6.jpg", "..."],
           "description": "..."}]}
```

Purely time-based — no line numbers baked in; consumers anchor themselves.
`gaps_from_dict()` reads leniently (malformed entries dropped, logged).
Disabled stage → `{"gaps": []}` still written, so `--from-stage`
skip-validation passes.

## Consumers

### summary

- `run_summary()` gains `gaps_paths` (one per source, orchestrator-passed
  like `json_paths`).
- `segment_video()`'s user prompt gets a per-video block appended after the
  numbered transcript:

  ```
  ## Silent gaps (visual context)
  - after line 23 (12.4s–18.2s, 5.8s): <description>
  ```

  Anchoring: a gap attaches to the last line whose end ≤ gap start, via
  `timing.segment_times` on the already-passed `json_paths`.
- No `summary.json` schema change — the LLM weaves visual info into part /
  video summaries; `plan` benefits transitively.
- Missing/empty/malformed gaps file → prompt byte-identical to today
  (regression-guarded, same style as existing context guards).

### director

- `run_director()` gains a `gaps` path param (orchestrator-passed).
- In the timed transcript, a described gap renders as an indented,
  un-numbered annotation line directly after the line it follows:

  ```
  23: それでは実際にやってみます  [4.2s, gap 12.4s]
      [silent gap 12.4s: デモ画面でビルドが実行され、ログが流れている]
  ```

  Only `N:` lines carry numbers, so op line references stay unambiguous.
  Gap↔line matching is by time overlap with the inter-line gap window.
- Default `director.prompt` gains a paragraph: annotation lines describe
  what is visible during a silence; to preserve that moment, emit a `keep`
  op spanning the two adjacent lines (a keep spanning lines N..N+1 already
  force-preserves the silence between them — no new op type).
- Absent/empty gaps file → transcript byte-identical (regression-guarded).

## Error handling

Every failure degrades, never aborts the pipeline:

| Failure | Behaviour |
|---|---|
| frame extraction fails | skip frame (logged) |
| gap has zero frames | skip gap (logged) |
| vision call fails after retries | skip gap (logged + LLM report) |
| malformed `_gaps.json` entry | dropped leniently at read |
| stage disabled | `{"gaps": []}` written; consumers byte-identical |

## Testing (TDD)

Pure-function tests first, mutation-verified per the repo's TDD policy:

- `select_gaps` (threshold, ordering), `frame_times` (3 frames, short-span
  dedupe), `build_snapshot_cmd` (exact argv).
- Multimodal message construction (no network): header text, image part
  count/order, neighbour-line inclusion and omission.
- `gaps_from_dict` lenient parse.
- summary block formatting + line anchoring; director annotation injection;
  byte-identical-when-absent guards for both consumers.
- `run_gap_context()` with mocked `call_llm`: success, retry-then-success,
  degrade-to-drop, disabled no-op.
- Config: example-file sync test covers the new section automatically.

## Documentation updates

Per the documentation policy: `AGENTS.md` (stage overview + structure +
stage list), new `docs/stages/gap_context.md` deep-dive, `README.md`,
`plan.md`, regenerated `config.example.yml`.
