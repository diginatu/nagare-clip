# `intervals.min_cut` — absorb cuts too short to be worth the jump

**Date:** 2026-07-27
**Stage:** `intervals`

## Problem

`intervals/intervals.py` post-processes keep intervals four ways — `merge_intervals`,
`apply_margins`, `ensure_keep_covers_captions`, `enforce_min_keep_duration`. All four
constrain the *intervals*; nothing constrains the *gaps between them*. A keep interval
that is too short gets extended, but a cut too short to save any time still gets made:
the picture jumps and the runtime barely moves.

Real-run evidence (`water_pump_3`, `PXL_20260324_092107933_intervals.json`: 4064s source,
342 keep intervals, 341 gaps):

| gap length | count |
|---|---|
| < 0.3s | 43 |
| < 0.5s | 65 |
| < 1.0s | 107 |

43 cuts under 0.3s together save a dozen-odd seconds while creating 43 visible jump cuts.
The same pattern appears in the other two sources (17 of 128, and 5 of 30). The cause is
margin arithmetic: `keep_pre_margin`/`keep_post_margin` (1.0s each) plus caption expansion
eat into an exclude gap from both sides, leaving a millisecond sliver of a cut behind.

Config cannot fix this today. Raising `audio_silence.min_silence` works on a different
axis — it changes *which silences are detected at all*, so raising it also keeps the long
pauses. `intervals.silence_threshold` and `intervals.min_keep` are likewise interval-side,
not gap-side. What is wanted is "detect it, but don't act on it if the resulting cut would
be too short to be worth the jump".

## Design

### `merge_close_intervals(keep_intervals, min_cut)`

A fifth post-processor in `src/nagare_clip/intervals/intervals.py`, pure like the other
four: sorted disjoint `{"start", "end"}` dicts in, same shape out. One left-to-right pass;
a gap `< min_cut` from the previous interval's end is absorbed (the previous interval's
end extends to this one's). Gaps are compared strictly (`<`), so a gap exactly at the
threshold survives. Absorption is transitive — a chain of slivers collapses into one
interval. `min_cut <= 0` returns the input unchanged, and the input is never mutated.

### Wiring

Called last in `run_intervals`, directly after `enforce_min_keep_duration` and before
`output_data` is assembled, with an `logging.info` line matching its neighbours.

**Why last.** Every earlier pass only *shrinks* gaps — margins, caption expansion and
`min_keep` all expand intervals outward. Last is therefore the only position where the
gap being measured is the final gap. Run before `min_keep`, and `min_keep`'s expansion
could open a fresh sub-threshold gap that nothing then absorbs.

**Interaction with captions.** `collect_captions` runs on the pre-merge intervals, so
caption chunking is byte-identical to before. Merging only widens keep intervals, so the
"every caption has timeline overlap" invariant that `ensure_keep_covers_captions`
established still holds afterwards.

**Interaction with `speed_ranges`/`overlays`.** None. Both are independent top-level
arrays, and the blender stage's `split_intervals_by_speed()` splits keep intervals at
speed boundaries downstream, so a merged interval still retimes correctly.

### Config

`intervals.min_cut: float = 0.4` in `IntervalsConfig`, after `keep_post_margin`.
`config.example.yml` is regenerated (`make config-example`).

The name is `min_cut`, not `min_gap`: it reads as "don't make a cut shorter than this",
and `gap_context.min_gap` already exists with unrelated semantics (the minimum silence
worth snapshotting).

### Accepted caveat

Audio inside an absorbed gap becomes audible again — including a deliberate deletion (a
`<cut>` tag or a `{{old->}}` patch) shorter than `min_cut`. This is consistent with the
existing 1.0s keep margins, which already re-admit a second of audio at every boundary.
`min_cut: 0` restores the previous behaviour exactly.

## Testing

Unit tests for the pure function (TDD, red first): sub-threshold gap absorbed, gap exactly
at threshold kept, chain of slivers collapsed transitively, `0` disables, empty input,
input not mutated. Plus one `run_intervals`-level test proving the config key is actually
plumbed through end to end.

Existing intervals fixtures are checked for sub-0.4s gaps that the new default would now
merge; any such test gets `min_cut: 0` in its cfg rather than a weakened assertion.

## Docs

`docs/stages/intervals.md` (Margins & captions), the `AGENTS.md` intervals overview, and
`README.md`.
