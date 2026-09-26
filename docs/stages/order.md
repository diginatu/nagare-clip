# Segment order — the shape of the finished video

The finished video is a sequence of **segments**, not of source files. A segment
is one stretch of one source, and the **director** chooses both the segments and
their sequence, starting from shooting order
(`docs/superpowers/specs/2026-09-26-director-decided-order-design.md`).

Before this existed, the finished video was the sources concatenated in name
order and nothing could change that: a device demonstration filmed in the middle
of the last clip played seven minutes after the thread it belonged to, and an
editorial instruction asking for a different arrangement had to be deleted from
the project brief because the pipeline could not carry it out.

## The value

```python
# src/nagare_clip/order.py — stdlib only, importable from Blender's Python
@dataclass(frozen=True)
class Segment:
    stem: str
    lines: tuple[int, int] | None      # None = the whole source
    gap_end: bool = False              # ends on the silence AFTER lines[1]
```

`gap_end` is spelled `"83~"` on disk, as `_director.json` spells a silence edge:
`{"stem": "a", "lines": [31, "83~"]}`. Without it the silence after a segment's
last line belongs to whatever plays the next line; with it, to this segment. A
start never needs the flag — the silence before a segment's first line is its
own unless the segment before it claimed it. `gap_end` on a source's last line
means nothing and `normalise` drops it (which also lets `[1, "N~"]` collapse to
the whole source). Coverage is still counted in lines: a gap is not a line.

**`lines=None` means the whole source**, and that is what makes the fallback
free: shooting order is `identity_segments(project_stems(input_dir))`, which
needs no transcript and no summary. Every consumer's `None` branch *is*
the code path the pipeline had before segments existed.

`Segment(stem, (1, N))` and `Segment(stem, None)` mean the same thing, so
`normalise(segments, line_counts)` collapses the former into the latter wherever
the line count is known. Without it, "an identity order behaves like today"
would hold only when the `order` key is *absent* — and the prompt asks for
explicit ranges, so present-and-identity is the common case. Uncanonicalised it
would leak: the `llm_report` unit would read `{stem}_1-127` instead of `{stem}`,
and the director's context label `stem [1-127]` instead of `stem`.

## The contract

```python
validate_segments(segments, line_counts) -> list[str]   # problems; never raises
```

Per stem, the segments must partition `1..N` exactly: no gap, no overlap,
nothing outside the range, and no source without a segment. **Coverage is the
contract; sequence is free.**

Deleting footage is the director's job. An order that could drop lines by
omitting them would make a missing scene indistinguishable from an editorial
decision, which is why the check is coverage rather than "a subset".

`line_counts` is `len(text_filter/{stem}_edits.txt)` — the file the director
slices, pinned to the JSON segment count by `intervals/check_edits.py`. It is
collected for the whole **project**, not the run, so `--source X` validates the
same order a full run would.

**On any problem the fallback is shooting order for the whole project** — never
a partial repair, which would be a video with a scene silently moved. Validation
runs twice on purpose:

- at **parse time** in the director's loop (`loop._read_order`, in display
  numbers), so a model's order is refused before it is ever written;
- at **resolve time** in `pipeline/stages._resolve_order`, because
  `director/order.json` is hand-editable and `sentence_split`/`summary` can
  re-run underneath it.

## The director decides it

The director's view is **always shooting order** — one `[k]` block per source,
numbered once — whatever order is in force. The order is conversation state
(`LoopState.order`, display ranges), not the shape of the transcript, so a
display number names one line for the whole conversation and the cached system
message never changes.

- **A reply may carry `order`**: display ranges in playback order, replacing the
  order in force whole, on any turn — alone (`{"order": …}`, which changes
  nothing else) or beside a range's ops. It must tile `1..N`; a problem refuses
  the order only (the ops still land) and the previous order stays.
- **A range may start or end on a silence line.** A silence plays with the range
  it is in: ending on the silence after `b` becomes `b~`; starting on it starts
  at `b+1`, which already owns that silence. A range of only a silence line is
  refused.
- **A range over a source boundary** is two segments that happen to be adjacent
  (`loop.order_segments` splits it).
- **Timelapses**: an order break inside one source would cut a timelapse's
  speed-up in two with its caption on one side. An order whose break falls
  inside any timelapse left standing by the reply is refused, and a new
  timelapse across a break of the order in force is refused. `cut`/`keep` may
  cross a break — on disk they stay one op and `intervals` computes them once.
  A break is a range end whose next range does not start on the next line
  (`loop.order_breaks`).
- **The start** is `director/order.json` when the directory already holds one
  (the director resumes from its directory), converted by `loop.order_ranges`;
  otherwise shooting order.
- **What the model sees**: when the order moves anything, `edit_state` adds the
  video as it plays — `timeline_runs()` takes the breaks as forced run ends, so
  every run lies inside one range and is listed there — with both sides of every
  seam quoted. Captions are listed in playback order. The runtime does not
  depend on the order.

The stage writes `director/order.json` (`{"order": [...]}`) on every run — the
seed when the model never sent one, a disabled director included, and before a
failing conversation raises.

## Where the authority lives

> **The director is the authority on the order up to and including
> `intervals`. `intervals/timeline.json` is the authority after it. `intervals`
> is the single conversion point from lines to seconds.**

`pipeline/stages._resolve_order` reads `director/order.json`, else shooting
order. The manifest builder and the order note read it
(through `_timeline_segments`); `director_preview` does too, per segment in
playback order. `blender`, `publish` and `cut_report` read the manifest and
never an order. Nothing downstream of `intervals` re-derives a time from a line
number, and nothing upstream reasons in seconds.

A hand-edited `order.json` is checked for coverage but not against timelapses:
a human who splits one means it.

That rule is also why there is no staleness guard on `timeline.json`:
hand-editing `order.json` and re-running `--from-stage blender` gets the old
order, but `intervals` is already an upstream dependency of `blender`, and with
exactly one conversion point the two can lag, never disagree.

`pipeline/sources.py::project_stems()` does **not** go away. It stops being what
stages consult for "what plays before this" and becomes the producer of the
identity value — remove it and the fallback goes with it.

## Lines to seconds

`intervals/manifest.py::build_manifest` writes `output/intervals/timeline.json`:

```json
{"segments": [
  {"stem": "PXL_20260328_082352713", "start": 0.0, "end": 1507.7},
  {"stem": "PXL_20260502_085157585", "lines": [31, 83], "start": 223.9, "end": 669.2}
]}
```

A segment `[a, b]` runs from **the end of line `a-1`** to the end of line `b`,
with a source's first segment starting at `0.0` and its last ending at that
source's `duration_sec`. Ending the previous segment where its last line ends
gives each segment the silent gap that *precedes* its first line, so
`keep_pre_margin` stays with the speech it belongs to and a moved segment takes
its own run-up with it rather than leaving it behind.

A segment ending `b~` keeps the silence after `b` instead: that boundary sits at
`max(end(b), start(b+1) − intervals.keep_pre_margin)`, so the silence stays with
the segment it ends and line `b+1` still keeps its own run-up. Both segments of
the boundary read the same rule (`build_manifest(..., pre_margin=)`), so they
cannot disagree, and without the flag the output is unchanged.
The manifest entry keeps the spelling (`"lines": [31, "83~"]`, `TimelineSegment.gap_end`) so it reads like `order.json`; the seconds already account for it, and nothing downstream reads `lines`.

An unresolvable boundary degrades the **whole** manifest to shooting order: a
source that cannot be split would otherwise collapse into one entry and silently
change the order. A source with no readable intervals JSON is skipped, the way
the cut report already skips one.

The manifest is deliberately **thin** — it names sources and time ranges and
carries no pre-sliced intervals. `{stem}_intervals.json` is the documented
human-editable contract for the Blender stage, and a fat manifest would make a
hand edit to it ineffective.

## Slicing

`blender/frames.py` holds `slice_intervals_data`, `placement_order` and
`ordered_sources` for the same reason it holds `split_intervals_by_speed`: the
placement loop, `publish/timeline.py` and `cut_report/metrics.py` all turn one
manifest plus each source's intervals JSON into the same list of placeable
segments, and two versions of that arithmetic would be two versions of the
finished video.

- **keep intervals, speed ranges**: clipped to the window, with a boundary
  introduced only where it falls strictly inside — so a whole-source window
  returns the input unchanged, which is what makes the identity path provably
  the previous behaviour.
- **captions, overlays**: assigned whole to the window containing their
  **start**, not by overlap. By overlap, a caption straddling a boundary would
  render in both segments; `place_captions` already clamps one against whatever
  timeline map it is given.
- `duration_sec` and `source_file` are carried through — they describe the
  source, not the slice.

## The order note

`llm_report/notes/order.md` (`order_note.py::format_order_note`), inlined into
`index.md` beside the cut report. No LLM call — the same spirit: the machine
states what happened and the
human decides whether it was right.

It is written when the resolved order differs from shooting order (each
segment's position, and where it was), and when an order was **rejected** (the
problems, and that the pipeline fell back). Nothing is written when the resolved
order *is* shooting order, including an `order.json` that states shooting
order explicitly — that is not a reorder. A stale note is deleted.

`write_order_note(ctx)` is called from the `director` adapter and again from
`intervals`, the same idempotent double-write `write_cut_report` uses, so a
`--to-stage director` review run sees it rather than only a full run.

Relying on a model to mention a reorder is not enough. A
reorder changes the shape of the finished video more than any other single
decision, and the failure mode to avoid is a human noticing it only while
watching the result.

## What the prompt says, and what it deliberately does not

`DIRECTOR_PROMPT`'s Order paragraph gives the `order` key, the coverage contract
as a **rule**, and what a silence line and a timelapse do at a range edge.

It carries **no worked reorder**. The editorial call belongs in the `project:`
brief — which on the project this was built for asks for the device thread first
and the fish thread second, and says the sources need not stay in shooting
order. A prompt that also argued for reordering would compete with the brief,
and this project has repeatedly watched the brief lose that competition; a
worked example anchors harder than the instruction around it (improvement 11).

(The order used to be the `plan` stage's to state and `plan_revise`'s to
restate; both stages were removed when the director took the order, its own
plan and the human conversation over.)
