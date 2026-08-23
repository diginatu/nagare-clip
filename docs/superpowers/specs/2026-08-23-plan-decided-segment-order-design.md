# Let the plan decide the order of the finished video

Design for improvement 24 (`nagare_improve_24_reorder.md`).  It replaces the
ordering assumption improvements 19 and 22 were built on: the finished video
stops being "the sources concatenated in shooting order" and becomes "the
segments the plan chose, in the order it chose".

Improvement 24's proposal is the input, not the specification.  Where this
design differs from it, the difference is stated with its reason.

## The unit: a segment

```python
# src/nagare_clip/order.py  — stdlib only, no stage imports
@dataclass(frozen=True)
class Segment:
    stem: str
    lines: tuple[int, int] | None      # None = the whole source
```

`order.py` sits in the same tier as `timing.py` and `brief.py`: pure, importable
from anywhere, including Blender's own Python where the package is not
installed.

**`lines=None` means the whole source.**  The proposal implies every entry
carries an explicit range; making it optional is what lets the identity value be
constructed without reading a single transcript:

```python
identity_segments(stems) -> [Segment(stem, None) for stem in stems]
```

Every consumer's `None` branch *is* today's code path, so the fallback cannot
fail for want of an input.  It also lets a human hand-write an order as
`[{"stem": "a"}, {"stem": "b"}]`.

### Canonical identity

`Segment(stem, None)` and `Segment(stem, (1, N))` mean the same thing.  They must
not be distinguishable downstream, or "an identity order behaves exactly like
today" would hold only when the `order` key is *absent* — and the plan prompt
will ask for explicit ranges (a full-coverage contract is easier for a model to
satisfy when it always writes the range), so present-and-identity is the common
case, not the rare one.  Left uncanonicalised it would leak: the `llm_report`
unit would be `{stem}_1-127` instead of `{stem}`, and the director's context
label would read `stem [1-127]` instead of `stem`.

```python
def normalise(segments, line_counts: dict[str, int]) -> list[Segment]
```

collapses `(1, N)` to `None` wherever `line_counts[stem] == N`.  It runs at
resolve time (always) and at parse time in `plan`/`plan_revise` before the
artifact is written, so `plan.json` is canonical on disk too.  A stem with no
known line count is left as written.

## The coverage contract

```python
def validate_segments(segments, line_counts) -> list[str]   # problems; never raises
```

Per stem, the segments' ranges sorted must be exactly a contiguous,
non-overlapping partition of `[1..N]`.  Reported problems: unknown stem; a known
stem with no segment; `a > b`; out of range; a gap; an overlap.  `lines=None`
counts as the full range.

`line_counts` is `len(text_filter/{stem}_edits.txt)` — the file the director
actually slices, guaranteed equal to the JSON segment count by the line-count
check `intervals/check_edits.py` already performs.  It is read for the project's
stems, not the run's, so a `--source` run validates the same order a full run
would.  The orchestrator collects it and passes it in: `run_plan(...,
line_counts=...)`, `run_plan_revise(..., line_counts=...)`; a stem whose
`_edits.txt` is missing or unreadable is simply absent from the mapping.

Validation runs twice, deliberately:

- **At parse time** (`plan`, `plan_revise`).  An invalid order is dropped from
  the response — logged, recorded as `dropped-items` — so the artifact never
  carries an invalid contract.  It simply has no `order` key, and downstream is
  identity.  With no line counts available, no order is written at all.
- **At resolve time** (`pipeline/stages._timeline_segments`).  Re-checked against
  current line counts, because `plan.json` is hand-editable and
  `sentence_split`/`summary` can re-run underneath it.

**On any violation: identity for the whole project.**  Never a partial repair —
a partly-repaired order is a video with a scene silently moved.  A rejection is
a WARNING in the log and a paragraph in the order note (below).

## The single source of truth

```python
# pipeline/stages.py
def _timeline_segments(ctx) -> list[Segment]
```

Reads `_effective_plan_json(ctx)`, takes its `order`, validates and normalises
it, and returns it — or the identity value.  This is the one point that replaces
`project_stems()` as the thing stages consult for "what plays before this".

**`project_stems()` stays.**  The proposal says to replace it; replacing it would
remove the fallback.  It stops being consulted directly and becomes the producer
of the identity value: `identity_segments(project_stems(ctx.input_videos_dir) or
ctx.stems)`.

Consumers, all through `_timeline_segments`: the `director` loop (improvement
19's position, 22's seams, 19's prior captions), the `intervals` manifest
builder, and the order note.

### `--source X` keeps global numbering

Improvement 19 made the timeline order independent of `--source` on purpose, and
`test_director_adapter_recovers_the_order_for_a_single_source_run` pins it.  That
property is preserved exactly:

`_timeline_segments(ctx)` always returns the **global** order — every segment of
every project stem.  The director loop walks that whole list for position, seam
neighbours and prior captions, and **calls the LLM only for segments whose stem
is in `ctx.stems`**.  So `--source X` still reports `segment 9 of 14`, still
takes its seams from its real neighbours (reading other sources'
`text_filter/{stem}_edits.txt` off disk), and still sees the captions committed
by earlier segments — including earlier segments of other sources.

The same filter applies to `blender`, `cut_report` and `publish`: they take the
manifest entries whose stem is in `ctx.stems`, in manifest order, which
generalises today's "`--source X` builds a `.blend` of just X".

The existing test is extended rather than replaced: it must additionally assert
the global position and that segments of other sources are not called.

## `plan` emits the order

`plan.json` gains a top-level `order`:

```json
{
  "order": [
    {"stem": "PXL_20260328_082352713", "lines": [1, 127]},
    {"stem": "PXL_20260502_085157585", "lines": [31, 83]},
    {"stem": "PXL_20260502_085157585", "lines": [1, 30]}
  ],
  "directions": [ … ]
}
```

- `plan_to_dict` / `plan_from_dict` carry it; an absent key reads as `[]`, so
  every existing project's `plan.json` degrades to identity.
- `try_parse_plan_response` parses it, validates it against `line_counts`,
  normalises it, and drops the whole order (never part of it) on any problem.
- The default `plan.prompt` gains the `order` key, the coverage contract stated
  as a rule ("every line of every source exactly once; deleting footage is a
  later stage's job"), and the instruction that shooting order is the default
  and a reorder must be announced in the `message`.  No worked reorder example —
  improvement 11 is the standing evidence that an example in these prompts
  anchors harder than the instruction around it.

**No `plan.reorder` config key.**  The constraint the proposal states is
visibility, not permission, and the escape hatches already exist: hand-edit
`plan.json`, or say so in `plan_dialogue/`.

### `plan_revise` restates the order

The revision response gains an optional `order`, restated whole rather than
edited by operation — agreeing with the proposal: order *is* position, so a
partial edit would need an insertion position, which improvement 23 deliberately
removed from the direction ops.

**An omitted `order` inherits the plan's**, it does not clear it.  That is the
same rule that governs directions: what no operation names survives by the code,
not by the model's diligence.  `plan_revise/plan.json` therefore always carries
the effective order, since `_effective_plan_json` picks exactly one file.

## `intervals` emits the manifest

`output/intervals/timeline.json`, written by the `intervals` adapter after its
per-source loop:

```json
{"segments": [
  {"stem": "PXL_20260328_082352713", "start": 0.0, "end": 1531.2},
  {"stem": "PXL_20260502_085157585", "lines": [31, 83], "start": 305.44, "end": 1002.9}
]}
```

Source seconds, playback order.  `lines` is omitted for a whole-source segment
(the canonical identity form).

**Line → seconds.**  A source's first segment starts at `0.0`; its last ends at
that source's `duration_sec`, read back from the `{stem}_intervals.json` just
written rather than recomputed.  An internal boundary between line `b` and line
`b+1` is `end_of_line_b`, from the sentence_split JSON.

That rule gives each segment the silent gap *preceding* its first line, so
`keep_pre_margin` stays with the speech it belongs to: a moved segment takes its
own run-up with it rather than leaving it behind.

**The manifest is thin.**  It names sources and time ranges; it does not carry
pre-sliced intervals.  `{stem}_intervals.json` is the documented human-editable
contract for the Blender stage, and a fat manifest would make a hand edit to it
ineffective.

### Slicing, in one place

```python
# blender/frames.py
def slice_intervals_data(data: dict, start: float, end: float) -> dict
def ordered_sources(segments, data_by_stem) -> list[tuple[str, dict]]
```

`blender/frames.py` is already the bpy-free home shared by the placement loop,
`publish/timeline.py` and `cut_report/metrics.py`, for exactly this reason: so
the finished-timeline arithmetic cannot exist in two versions that disagree.
(`split_intervals_by_speed` lives here, not in `blender/timeline.py` as
improvement 24 states.)

- `keep_intervals`, `speed_ranges`: intersected with the window; a boundary is
  introduced only when it falls strictly inside — the rule
  `split_intervals_by_speed` already uses — so a full-cover window returns the
  input unchanged, which is what makes the identity path provably today's.
- `captions`, `overlays`: assigned to the window containing their **start**, not
  by overlap.  By overlap, a caption straddling a boundary would render twice.
  `place_captions` already clamps against whatever `tl_map` it is given, so a
  straddling caption is clipped for free.
- `duration_sec` and `source_file` are copied through unchanged.

Everything `intervals` writes is bounded to `[0, duration_sec]` (`invert_intervals`,
`apply_margins`, `apply_caption_margins` and `snap_overlay_starts` all clamp to
it), so clipping the last window at `duration_sec` is a no-op.

## `blender` iterates the manifest

`blender_cli` gains an optional `--manifest`.  Absent → today's per-source loop,
so a project with no `timeline.json` still resumes at `--from-stage blender`;
`_intervals_required` deliberately does not require the file, for the same
reason.

The placement loop iterates manifest entries instead of sources.  `place_strips`,
`build_timeline_map` and the cursor arithmetic are untouched — they already take
a `start_cursor` and return the next one.  `source_num` / `idx_offset` become
segment number / running strip count; under identity those are numerically what
they are today, so strip names do not churn.  Scene fps and resolution come from
the **first manifest entry's** source, which under identity is `sources[0]`.

The output filename stays `{ctx.stems[0]}_edited.blend` — shooting order — so a
reorder does not rename the project file.

**Acceptance:** on `/mnt/work/YouTube/water_pump_4` with an identity order, the
build produces 146 strips and `frame_end` 74718, as today.

## `director` runs once per segment

### Absolute line numbers

Line numbers stay absolute, as the proposal requires:
`generate_director_ops(..., first_line=a)` renders
`format_numbered_transcript{,_timed}` from `a`, and `_coerce_lines` / `_parse_op`
validate ops into `[a, b]`.  `clean_lines`, `seg_times` and `silences` are sliced
to the segment, so `anchor_gaps` / `annotate_numbered_transcript` — which work on
positions within the rendered transcript, not on line numbers — need no change at
all.  `ops_from_dict(data, None)`, which reads another video's ops for prior
captions, is unaffected.

### The loop, and the merge

The orchestrator loops the **global segment order**, not the sources — that loop
is what improvements 19 and 22 need anyway.  Ops accumulate in memory per stem
and `{stem}_director.json` is written, line-sorted, as soon as that stem's last
segment in the order completes.  No per-segment files, no new naming convention;
`_director_required` and `guided_edit` are untouched.

Before the loop, every `{stem}_director.json` for a stem in `ctx.stems` is
deleted — those files are about to be rewritten, and this guarantees a crash
cannot leave one of them standing as if it were current.  Files belonging to
sources this run is not processing are left alone (prior captions read them).

- **Prior captions**: the segments *j < i*.  From memory when that segment ran in
  this pass; otherwise from the stem's on-disk `_director.json`, filtered to that
  segment's line range — exact, where today's source-granularity read is already
  an approximation.
- **Seams**: `seam_lines()` receives the neighbouring *segment's* slice of its
  `_edits.txt`.  A small `Neighbour(stem, edits, lines)` replaces the two bare
  `before_edits` / `after_edits` paths.  Every side stays optional and degrades
  to nothing.
- **Context** (`director/context.py`): "This segment" lists the parts overlapping
  its range, clipped; other parts of the same source appear as their own sibling
  segments, which is the uniformity the proposal asks for.  `_sibling_entry`
  labels a whole-source segment by its stem (unchanged) and a partial one
  `stem [31-83]`, with the overlapping part summaries as its text.

### Failure is fatal for a multi-segment source

If any segment call of a source with **more than one segment** fails, the run
fails: the stem's `{stem}_director.json` is not written (and, having been deleted
before the loop, no stale file survives), the failure is logged at ERROR, and a
`PipelineError` is raised.  A source whose ops cover two of its three segments is
a worse artifact than no run at all, and the previous file was built under a
different segmentation, so it is not a valid fallback either.

`generate_director_ops` therefore has to distinguish "the LLM failed" from "the
LLM returned no ops", which today it cannot — both are `[]`.  It returns a
`DirectorResult(ops, ok)`, mirroring `plan_revise.Revision.ok` and
`plan.ParsedPlan`.

A **single-segment** source keeps today's graceful degrade: all attempts failing
writes `{"ops": []}` and the pipeline proceeds with the unedited transcript.  A
1-of-1 failure is not partial, and making it fatal would change behaviour on the
identity path, which must stay as it is.

The divergence note and the order note are written in the adapter's `finally`
today.  On a fatal segment failure they are **skipped**, not written from a
half-deleted director directory: a note describing ops that are not there is
worse than no note.

### `llm_report`

One unit per call, i.e. per segment.  Named `{stem}` when the segment covers the
whole source, `{stem}_{a}-{b}` when partial.  Identity runs keep today's report
filenames; a source split into three appears as three units, which is the
proposal's criterion 3.

### The prompt is not byte-identical under identity

`video 3 of 7` becomes `segment 3 of 7`, and the wording around it follows.  The
alternative — saying "video" whenever segments happen to be whole sources — is a
special case that makes the code lie in the mixed case.  Criterion 1 (the same
`.blend`) is about the mechanical path and holds exactly; the director's prompt
does change by these words.

## `publish`

**The sort is the symptom; the placements are the cause.**  `build_placements()`
lays each *source* end to end, so after a reorder its seconds are computed in
shooting order and sorting wrong times by time fixes nothing.  It has to consume
the manifest and lay out **segments**, sliced, in playback order.

- `build_placements(sources)` keeps its `Sequence[tuple[str, dict]]` signature and
  is fed `ordered_sources(...)`: sliced data, in playback order.  Its docstring
  changes from "sources in order" to "segments in playback order".
- `first_surviving_time(placements, stem, start, end)` needs no change: it filters
  by stem and source-time overlap, so a part lands in whichever segment covers
  its seconds, and the `min` over candidates is the earliest surviving moment in
  the finished timeline.  This is what `_thumbnail_entries` resolves through as
  well, so it is fixed by the same change.
- `_chapter_entries` additionally **sorts by finished-timeline time**.
  `build_chapters` documents that entries must already be in reading order and
  *drops* any entry that does not advance; without the sort a reorder silently
  deletes chapters.  The drop rule stays, for genuine ties.
- `run_publish` takes the ordered sliced sources instead of `stems` +
  `intervals_paths`.  `flat_overlays` is ordered by each stem's first appearance
  in the manifest, so "the captions in the finished video" is in finished-video
  order.

## `cut_report`

- `measure()` is fed the same ordered sliced sources.
- Gaps and fragments are grouped **per segment**, not per stem.  The module
  states that "the seam between two sources is a concatenation boundary, not a
  cut"; a boundary between two segments of one source is exactly that too, and
  grouping by stem would report it as a sub-threshold cut nobody made.
- `source_duration` sums `duration_sec` over **distinct stems**, not over
  segments, or a three-segment source counts its length three times.
- `CutMetrics.sources` stays the distinct-source count; a `segments` count is
  added beside it.

## The order note

`llm_report/notes/order.md`, inlined into `index.md` by `rebuild_index()` beside
`cut_report.md` and `plan_divergence.md`.  Deterministic, no LLM call — the same
spirit as improvements 20 and 21: the machine states what happened and the human
decides whether it was right.

Written when the resolved order differs from shooting order (which segments moved
and to where), and when an order was **rejected** (the problems, and that the
pipeline fell back to shooting order).  Otherwise a stale note is deleted.

`write_order_note(ctx)` is called from both the `director` and the `intervals`
adapters — the same idempotent double-write `write_cut_report` already uses — so a
`--to-stage director` review run sees it rather than only a full run.

## Deliberately out of scope

- **A staleness guard on `intervals/timeline.json`.**  Hand-editing `plan.json`
  and re-running `--from-stage blender` gets the old order.  `plan` is already an
  upstream dependency of `blender`; skipping the stages in between and getting
  stale results is the normal consequence of doing that, not a hazard this change
  introduces.
- Reordering *within* a segment, and any change to how `summary` draws part
  boundaries.
- Transitions, B-roll, audio crossfades.  A bridge at a reorder boundary is the
  existing tools' job: the plan attaches an instruction to the moved segment and
  the director — which sees the lines and now sees its real neighbours — writes an
  overlay or trims a connective phrase.

## Cost

The number of director calls becomes the number of segments rather than the
number of sources.  Each call is smaller, so total tokens should not grow much,
but the count is now driven by the plan's granularity.

## Build order

Each step ends with a working pipeline.

1. **`order.py`** — `Segment`, `identity_segments`, `validate_segments`,
   `normalise`, to/from dict, the manifest types.  Pure; no behaviour change.
   Built first because it is the fallback everything else consumes.
2. **Manifest → `blender` → `publish` → `cut_report`** — the mechanical half,
   under identity order end to end.  The criterion-1 regression (146 strips,
   `frame_end` 74718) is its acceptance test, and the reorder mechanism is proven
   with no LLM in the loop.
3. **Per-segment `director`** — `first_line`, the global segment loop, seams,
   prior captions, context, the merge, `DirectorResult`, the report units.  Lands
   on a proven mechanism.
4. **`plan` emits `order`; `plan_revise` restates it** — prompt, parse, validate,
   normalise.  The first point at which a reorder can happen.
5. **The order note, and the docs** — `AGENTS.md`, `README.md`, `plan.md`,
   `docs/stages/{plan,plan_revise,director,intervals,blender,publish,cut_report,pipeline}.md`,
   and `make config-example`.

## Testing

TDD throughout.  Steps 1 and 2 are genuinely test-first.  Steps 3–5 modify code
that already exists, so every new test gets mutation-catch evidence — break the
clamp, the offset, the sort or the filter, watch the test fail, revert — reported
alongside the green run, per the repository's standing rule.

Specific regression guards, beyond the per-unit tests:

- an absent `order` key, an identity `order` written with explicit full ranges,
  and an identity `order` written without `lines` all produce byte-identical
  downstream inputs (director prompt aside from the position wording, manifest,
  placements, report unit names);
- an order that omits, repeats or overlaps a line is rejected, logged, and falls
  back to shooting order;
- `--source X` reports the global segment position and the real neighbours;
- a full-cover slice returns its input unchanged;
- a caption straddling a boundary is placed once;
- chapters after a reorder are ascending and none are dropped for it.
