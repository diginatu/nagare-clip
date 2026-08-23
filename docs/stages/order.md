# Segment order — the shape of the finished video

The finished video is a sequence of **segments**, not of source files. A segment
is one stretch of one source, and the `plan` stage chooses both the segments and
their sequence.

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
```

**`lines=None` means the whole source**, and that is what makes the fallback
free: shooting order is `identity_segments(project_stems(input_dir))`, which
needs no transcript, no summary and no plan. Every consumer's `None` branch *is*
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

- at **parse time** in `plan`/`plan_revise`, so the artifact never carries a
  contract that has not been checked (with no line counts, no order is written
  at all);
- at **resolve time** in `pipeline/stages._resolve_order`, because `plan.json`
  is hand-editable and `sentence_split`/`summary` can re-run underneath it.

## Where the authority lives

> **`plan` is the authority on the order up to and including `intervals`.
> `intervals/timeline.json` is the authority after it. `intervals` is the single
> conversion point from lines to seconds.**

`director`, the manifest builder and the order note read the plan (through
`pipeline/stages._timeline_segments`). `blender`, `publish` and `cut_report`
read the manifest and never the plan. Nothing downstream of `intervals`
re-derives a time from a line number, and nothing upstream reasons in seconds.

That rule is also why there is no staleness guard on `timeline.json`:
hand-editing `plan.json` and re-running `--from-stage blender` gets the old
order, but `plan` is already an upstream dependency of `blender`, and with
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
`index.md` beside the plan/director divergence note and the cut report. No LLM
call — the same spirit as those two: the machine states what happened and the
human decides whether it was right.

It is written when the resolved order differs from shooting order (each
segment's position, and where it was), and when an order was **rejected** (the
problems, and that the pipeline fell back). Nothing is written when the resolved
order *is* shooting order, including a plan that states shooting order
explicitly — that is not a reorder. A stale note is deleted.

`write_order_note(ctx)` is called from the `director` adapter and again from
`intervals`, the same idempotent double-write `write_cut_report` uses, so a
`--to-stage director` review run sees it rather than only a full run.

Relying on the plan to mention a reorder in its `message` is not enough. A
reorder changes the shape of the finished video more than any other single
decision, and the failure mode to avoid is a human noticing it only while
watching the result.

## What the prompts say, and what they deliberately do not

The default `plan.prompt` gives the segment vocabulary, states the coverage
contract as a **rule**, shows a minimal JSON shape, and requires a changed order
to be announced in the `message`.

It carries **no worked reorder**. The editorial call belongs in the `project:`
brief — which on the project this was built for asks for the device thread first
and the fish thread second, and says the sources need not stay in shooting
order. A prompt that also argued for reordering would compete with the brief,
and this project has repeatedly watched the brief lose that competition; a
worked example in these prompts anchors harder than the instruction around it
(improvement 11). A test asserts the shape example is not itself a reorder, so
one cannot creep back in.

`plan_revise` is **shown** the current order (`plan_llm.format_order`, in the
same terms its response uses) and restates it **whole**: order *is* position, so
unlike a direction it cannot be edited in pieces, and restating something whole
requires seeing it whole. An identity order renders *as* identity rather than
being omitted, so "no reorder yet" is a visible state and not an absence. An
omitted or rejected `order` is **inherited** by the code, not cleared — the same
rule that carries an unnamed direction through — so `plan_revise/plan.json`
always holds the effective order, since `director` reads exactly one plan file.
