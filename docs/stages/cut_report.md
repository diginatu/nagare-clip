# Finished-cut report (`cut_report`)

Deterministic metrics and checks on the finished cut. **No LLM call.** Writes
`llm_report/notes/cut_report.md`, which `llm_report.rebuild_index()` inlines
into `llm_report/index.md` next to the plan/director divergence section.

Not a stage: it has no `output/cut_report/` dir, no place in `STAGE_NAMES`, and
nothing depends on its output. It is a report, like `plan/divergence.py`.

## Why

The pipeline states numeric intentions in several places and never measured any
of them:

- the editorial brief («30分が上限», «テロップは3〜5分に1枚»)
- `DIRECTOR_PROMPT`: a timelapse should run "about a minute on screen", overlays
  "roughly one per 3-5 minutes of finished video"

On a real 7-source run this cost two defects that a human found only by reading
JSON for twenty minutes:

- seven captions authored at 20-34 chars/s inside an 8x timelapse, i.e. **0.21s
  each on screen**. Correctly *placed*, merely unreadable. `docs/operator-prompt.md`
  in the project repo already records this exact failure as a cautionary tale
  from an earlier run, so it has now happened at least twice.
(An earlier draft also flagged a 92.8s timelapse at factor 6.0 — 15.5s on
screen — for falling short of the prompt's "about a minute". That check was
**removed**: see below.)

## When it runs

After `intervals`, and again after `blender`.

`intervals` owns every number: the strip count is *derived* from the intervals
JSON the same way `blender_cli` derives it (`split_intervals_by_speed` per
source), so it needs no Blender to compute, and a `--to-stage intervals` run
still gets a full report. The second pass exists only to pick up Blender's own
warnings, which do need Blender to have run.

The report describes `ctx.stems` — the sources this run actually concatenated,
which is what the `.blend` contains. A `--source one.mp4` run reports that one
source, not the whole project.

## Two kinds of content, and the distinction matters

**Measurements print every run**, breach or no breach. They are the per-run
regression table the operator prompt asks a human to keep by hand; prompt
changes regress about as often as they improve, and this block is how a human
notices. A report that only speaks up on failure cannot serve that purpose.

```
source              65.5 min
finished            20.8 min   (32% of source)
  at 1x             17.5 min   (84%)
  under timelapse    3.3 min   (16%)
keep intervals           140
blender strips           146   across 7 source(s)
captions                 660   (210 start inside a speed range)
overlays                  18   (0.87 per finished minute)
keep gaps                133   min 0.50s, median 1.76s, below intervals.min_cut (0.50s): 0
keep fragments           140   min 1.70s, median 4.41s, below 1.00s: 0
```

plus one table row per `speed_range` (span, factor, on-screen seconds).

**Findings print only on a breach**, each carrying the threshold it breached so
the number is arguable rather than hidden — the rule the divergence note follows.

| kind | condition | threshold |
|---|---|---|
| `caption-compressed` | a caption at/above the reading rate whose start falls inside a `speed_range` | `cut_report.caption_chars_per_sec` (18.0) |
| `caption-fast` | the same rate, outside any `speed_range` | same |
| `timelapse-long` | a `speed_range` playing for more than the ceiling on screen | `cut_report.timelapse_max_screen` (180.0s) |
| `keep-gap` | an inter-keep gap shorter than the merge threshold | `intervals.min_cut` (0.4s) |
| `keep-fragment` | a keep interval shorter than the floor | `cut_report.min_keep_fragment` (1.0s) |
| `blender-warning` | one of Blender's own WARNING lines | — |

Ordered most-severe first (`checks.KIND_ORDER`): a caption nobody can read is a
defect, a timelapse over too soon is a judgement call, Blender's notices last.

### Why the caption check reads the *authored* rate, not the on-screen one

148 perfectly readable captions rode inside that same 8x range. Flagging a
caption merely for being inside a speed range would flag all of them, and a
check that flags everything is the same as no check. The trigger is therefore
the caption's own density (`len(text) / (end - start)` in **source** seconds);
the *report* then states what the factor did to it (`0.21s on screen,
272.9 chars/s`), which is what makes the defect legible. The aggregate — how
many captions start inside a speed range at all — is a measurement, not a flag.

### Why a timelapse's on-screen time counts only kept footage

`screen = kept / factor`, where `kept` is the source seconds of keep intervals
inside the range — not `span / factor`. A range half of which was cut plays for
half as long, and "about a minute on screen" is about what plays.

### Why there is no floor on a timelapse's on-screen time

There deliberately is none — not a configurable one either, so nothing can
switch it back on by accident.

The check exists for the long side only, and `docs/operator-prompt.md` names
the failure it catches: "A sustained mild fast-forward (1.3-2.0x) over a large
share of the video is a failure mode this pipeline hits repeatedly." A factor
picked too **low** turns a long span into a long fast-forward instead of a real
timelapse. That is what "about a minute on screen" is guarding.

The short side has no such failure. A 6x fast-forward running 15.5s is ordinary
vlog grammar; the viewer loses 93 seconds of audio and nothing else. A 30s floor
caught exactly one span on the run this was built from and that span was fine —
i.e. it was a flag that never fires on a real defect, which is the same
mistake in the other direction as a flag that fires on everything.

How long each timelapse plays for is still reported, as a **measurement**: the
on-screen table prints every `speed_range`, flagged or not.

### Deliberately out of scope

Comparing against `project.target_duration` or the brief's caption-density
sentence. Those are free text by design and parsing them would be guessing; a
wrong parse is worse than none. The finished duration and the overlay density
are stated plainly and the human compares.

## Blender's warnings

`blender.timeline`'s clamp and build-length warnings are the only notice that a
requested interval did not fit, or that Blender built a strip a different length
than was predicted — and they print into the same stream as Blender's unrelated
`bl_pkg`/`cattrs` extension tracebacks that the operator is told to ignore, so
in practice they scroll past unread.

`blender/warnings_file.py` attaches a WARNING-level handler to the root logger
for the whole build (`capture_warnings()`) and writes
`output/blender/blender_warnings.json` (`{"warnings": [...]}`) in a `finally`,
so a failed build still leaves what preceded it. The list is written **even when
empty**: a stale file would otherwise report warnings the current scene never
produced. `checks.read_blender_warnings()` reads it back; a missing or unreadable
file simply means no warnings to surface.

## Failure modes

Everything degrades and nothing here can fail a run:

- a missing or unparseable `{stem}_intervals.json` → that source is skipped (logged)
- **no** readable intervals at all → a stale note is deleted, nothing written
- `cut_report.enabled: false` → same: nothing written, a stale note removed
- an un-writable note → logged warning, the stage that produced the cut still succeeds

## Modules

| file | holds |
|---|---|
| `cut_report/metrics.py` | `CutMetrics`/`SpeedSpan`/`SpanStats`, `measure()` — pure, reuses `publish.timeline.build_placements` |
| `cut_report/checks.py` | `Finding`, `find_issues()`, `read_blender_warnings()` |
| `cut_report/report.py` | `format_cut_report()` and `build_cut_report(sources, cfg)` |
| `blender/warnings_file.py` | `capture_warnings()` / `write_warnings()` — no bpy import |
| `pipeline/stages.py` | `write_cut_report(ctx)`, called from the `intervals` and `blender` adapters |

The finished-timeline arithmetic is **not** a second copy: `measure()` calls
`publish.timeline.build_placements`, which already reproduces the blender
concatenation in seconds precisely so the two cannot disagree.

## Segments

The report measures the finished video as the manifest orders it. Inter-keep
gaps and keep fragments are grouped **per segment**: the seam between two
segments is a concatenation boundary, not a cut, whether they belong to
different sources or are two stretches of the same one. A source split across
segments is counted once and its length counted once, with the segment count
reported beside the source count.

See [`order.md`](order.md).
