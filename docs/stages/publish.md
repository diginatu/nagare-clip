# publish — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#publish--title-description-chapters-and-thumbnail-material).

## Purpose

The pipeline used to end at `blender`, and everything needed to actually
publish the video was then written by hand — even though the pipeline already
holds the material: `summary.json` has the whole-video, per-video and per-part
summaries; `plan.json` has the editorial shape; the `project:` brief states
audience, purpose and tone; `intervals` knows exactly where every kept span
lands; the director's ops mark the payoff moments.

`publish` runs once project-wide **after `blender`** and collects that into one
reviewable file pair. It does not upload anything, and it does not composite a
thumbnail — fonts, colours, shadows and layout are taste and change per video,
so that stays a project-level script. What the stage owes such a script is the
**copy** and the **frame shortlist**, which is exactly what `publish.json`
carries.

Disabled by default (`publish.enabled: false`): writes the empty artifact,
makes no LLM and no Docker call.

## Why chapters can only be produced here

A chapter timestamp is a position on the **finished** timeline. That mapping —
source seconds to output seconds — exists only in the intervals stage's keep
spans plus the blender stage's `speed_factor`-aware placement, and the blender
stage runs inside Blender and emits only a `.blend`.

`publish/timeline.py` reproduces the placement arithmetic on the host:

- keep intervals are split at speed-range boundaries with
  `split_intervals_by_speed` — the *same function* the blender stage uses. It
  was moved to `blender/frames.py` (which has no `bpy` import) and re-exported
  from `blender/timeline.py`, so there is one implementation, not two.
- sources are concatenated in the order they were passed (the order
  `blender_cli` receives its `--source`/`--intervals` pairs).
- each placed span occupies `(src_end - src_start) / speed` seconds of output.

The one deliberate difference from `build_timeline_map`: blender rounds every
span to whole frames, this stays in seconds. A mapped position therefore drifts
from the rendered one by well under a frame per placed span. Chapter timestamps
are whole seconds merged at a 10-second floor, so that is immaterial — and a
seconds-based map keeps `publish` free of fps and `bpy`.

`to_timeline()` returns `None` for a source time on cut footage.
`first_surviving(placed, stem, start, end)` returns the first
`(source_time, timeline_time)` at or after `start` that survived, or `None`
when nothing between `start` and `end` did — which is how a part that was cut
in its entirety drops out of the chapter list.

## Chapters (`chapters.py`)

YouTube only renders a description's timestamp list as chapters when ALL of:

1. the first timestamp is exactly `0:00`
2. there are at least three of them
3. they are in ascending order
4. each chapter runs at least 10 seconds

The stage **satisfies** these rather than hoping the mapping lands correctly:

- **First forced to `0:00`.** A part's opening seconds are routinely cut, so
  the first part rarely starts at zero on the finished timeline.
- **Short chapters merged.** A chapter shorter than `publish.min_chapter`
  (default 10.0s) is absorbed by the chapter *before* it, which keeps its title
  and simply runs longer. The first chapter has no predecessor, so it is
  absorbed by the one *after* it, which inherits the earlier start — and since
  the first entry is forced to `0:00` anyway, that is the same thing. The pass
  repeats until stable; dropping an entry only ever lengthens its neighbour, so
  it terminates. A part compressed inside a timelapse hits this floor easily
  (0.4 min of source at 8x is 3s of output).

The list is **always written**, even when it cannot qualify. YouTube auto-links
timestamps in a description regardless, so a list of two still lets a viewer
jump; it simply does not draw a segmented progress bar. `chapter_issues()`
reports every unmet condition, `publish.json` carries them in
`chapter_issues`, and `publish.md` prints them as a blockquote above the
still-present list. Treat the conditions as an upgrade to aim for, not a gate
to suppress the output behind.

Timestamps render `M:SS`, switching to `H:MM:SS` past an hour.

Titles start as the part summaries and are replaced by the LLM's short forms
via `apply_titles(chapters, {position: title})` — a missing or empty entry
keeps the summary, so a failed LLM call still yields a usable list. Each
chapter keeps `stem` and `part_index` (the 1-based `summary.json` part it came
from) in the JSON, so a human can trace a chapter back to its source.

## The LLM call (`publish_llm.py`)

ONE call produces every piece of copy: `titles`, `lead`, `chapters` (titles
only, index-mapped like the plan stage), `thumbnail_copy`.

- **Titles are a list, not a title.** Hook quality varies a lot between
  attempts and picking from a list is cheap; committing to a single generated
  title is not.
- **Timing is not the LLM's business.** Chapters are anchored and merged before
  the call; the prompt states the timestamps are FINAL and asks only for names.
- **Thumbnail copy is a set of 1-3 lines**, each carrying its role
  (`tag` / `hook` / `subtitle`), so the layout follows the copy rather than the
  copy being padded to fill a three-line template. A punchier video may want
  just a hook. More than three lines is truncated (logged); an unknown or
  missing role normalises to `hook` — a label is not worth losing copy over.
- `publish.title_count` / `publish.thumbnail_sets` are stated at runtime via
  `counts_note()` rather than baked into `publish.prompt`, so the numbers the
  LLM is asked for are always the numbers the config says.
- The user message carries the overall summary, each video's summary and parts
  (with the plan's direction where there is one), the **captions the edit
  already places on screen** (the director's `overlay` texts — the punchiest
  copy in the project, and exactly what a thumbnail wants), the finished
  length, and the chapter skeleton.
- The `project:` brief is appended to the prompt via `apply_brief`, like
  `summary`/`plan`/`director`/`text_filter`.

Parsing is lenient in the house style: invalid JSON or no `titles` array is a
hard failure that retries (`publish.max_retries`, temperature nudged per
attempt); everything else degrades field by field with malformed entries
dropped and recorded in the LLM report. All attempts failing yields empty copy
— and the chapters, computed without the LLM, are still written.

## Thumbnail frame candidates (`thumbs.py`)

A thumbnail wants the frame the video is *about*, and the director already said
where those are:

| op | moment(s) | why |
|---|---|---|
| `overlay` | start of its line | the caption names the payoff |
| `keep` | midpoint of its range | the middle is the event; the edges are the speech around it |
| `timelapse` | start, and end minus 0.2s | before and after: the "what changed" pair |

Every moment is snapped onto surviving footage with `first_surviving` within
its own op span — guided_edit drops blocked ops and silence detection cuts
around them, so an unsnapped moment could point at a frame that is not in the
finished video at all. An op with no surviving footage drops out.

The end inset (0.2s) exists because a span's end is exclusive on the timeline
(a keep interval ending at 40.0s places no frame *at* 40.0s) and the boundary
frame shows whatever comes next — the same reason `gap_context` insets its
frames.

Two candidates within 2.0s of each other **on the finished timeline** are the
same shot; the collision is resolved by rank (overlay < keep < timelapse
boundary) rather than by order, because inside a timelapse a 0.2s source inset
is 0.025s of finished video, so an overlay and a timelapse end really do
collide — and the caption is the reason the human would pick that frame.

`limit_candidates` thins an over-long list to `publish.max_frames` (default 12,
`0` = no limit) by **even sampling**, not truncation: truncating would hand the
human every payoff from the first source and none from the last.

Extraction is the pipeline adapter's job (`_extract_thumb_frames`): every
shortlisted still is pulled in ONE `docker compose run` via
`build_snapshot_batch_cmd`, the same batching gap_context uses (a container
costs ~0.8s of init; the ffmpeg seek is ~30ms). Width is
`publish.frame_width` (default 1920 — a thumbnail source, unlike gap_context's
960px vision-LLM input). A failed batch logs and never aborts the run; a
missing frame logs per candidate. Frames land in
`output/publish/frames/{stem}/{t:.3f}.jpg`.

## Outputs

`output/publish/publish.json` — the contract a project-level script reads:

```json
{
  "titles": ["…"],
  "lead": "…",
  "description": "lead\n\n0:00 …\n1:35 …",
  "chapters": [{"time": "0:00", "seconds": 0.0, "title": "…", "stem": "a", "part_index": 1}],
  "chapter_issues": [],
  "thumbnail_copy": [{"lines": [{"role": "tag", "text": "水槽DIY"}]}],
  "thumbnail_frames": [{"path": "frames/a/12.500.jpg", "stem": "a", "source_time": 12.5,
                        "timeline_time": 8.0, "reason": "overlay: 水浸し！"}],
  "duration_sec": 600.0
}
```

`description` is lead + blank line + the timestamp list, ready to paste — the
chapter conditions are properties of the description exactly as written, so the
stage renders the whole thing rather than leaving assembly to the human.

`output/publish/publish.md` — the same content laid out for review: title
candidates, the description in a fenced block, the chapter warning (if any),
the thumbnail copy sets, and a table of frame candidates with the reason each
was shortlisted.

A disabled run writes both files with every key present and nothing in them
(`empty_publish_data()`), and `publish.md` says the stage is disabled.

## Replacing a hand-written `make_thumb.sh`

The previous project's thumbnail script hardcoded its three annotate lines and
its source photo. The replacement takes them as parameters:

```bash
jq -r '.thumbnail_copy[0].lines[] | "\(.role)\t\(.text)"' output/publish/publish.json
jq -r '.thumbnail_frames[].path' output/publish/publish.json
```

The number of lines varies (1-3) and each carries its role, so the script's
layout can branch on what the copy actually is instead of assuming a tag, a
hook and a subtitle.
