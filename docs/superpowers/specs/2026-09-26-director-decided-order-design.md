# Let the director decide the order of the finished video

Status: spec; decisions taken on 2026-09-26 (§9).
Step 1 of folding `plan`/`plan_revise` into the director conversation.

## 1. Problem

`plan` decides the playback order (`order` in `plan.json`) from the summaries
alone — it never reads a transcript line. The director, which reads every
line, gets that order as the SHAPE of its view: `build_display_view()` numbers
the segments in playback order, so the order is fixed before the model that
knows the footage sees anything, and nothing it learns can change it.

The goal of the wider change is that the plan becomes the director's own first
thought, written into its conversation and revisable like any other reply. The
order is the hard part, because today it is not a reply: it is the view.

## 2. The view stays in shooting order

The director's view is ALWAYS built from `identity_segments()` — one `[k]`
block per source, in shooting order. The order is state the conversation
carries, not the shape of the transcript.

- A display number names the same line for the whole conversation, whatever
  the order becomes. No renumbering, no ops to translate, no stale history.
- The system message does not depend on the order, so it stays byte-identical
  across turns and cacheable.
- `[k]` is always one source, so "each `[k]` block was recorded as its own
  video" stays true.

The price: the model reads the footage in shooting order and has to picture
the reorder. §5 answers that in the per-turn state (the playback in order, and
every new seam quoted).

## 3. The reply's `order`

Any reply may carry `order`, a list of display ranges in playback order:

```json
{"range": [41, 80], "reviewed_through": 78, "ops": [...],
 "order": [[1, 40], [120, 150], [41, 119], [151, 300]]}
```

- **Whole replacement**, like a range's ops: the last accepted `order` wins. An
  absent key keeps the current order. Allowed on every turn.
- **Coverage, checked in display numbers**: sorted, the ranges tile `1..N`
  exactly (no gap, no overlap, nothing outside). A problem refuses the order
  only — the reply's ops still land — and the refusal names display numbers.
- **A range crossing a source boundary is split** there, losslessly: `[30, 50]`
  over the end of A and the start of B means "play A's tail, then B's head",
  which is two segments that happen to be adjacent.
- **Any granularity**: `[57, 57]` is a valid range. Deleting is `cut`'s job;
  an order can only move footage.
- **The initial order** is the effective plan's `order` (the revised plan when
  it exists), converted into display ranges (§4.3), or shooting order. So the
  plan's order still works, and the director can overrule it.

### 3.1 Timelapses and order boundaries

An order boundary inside ONE source is harmless for `cut`/`keep`: on disk the
op stays one op in source coordinates, `intervals` computes it once, and the
manifest's time windows only slice the result. A `timelapse` is not: its
speed range would be cut in two and its caption would show on one side only.

Checked against the state AFTER the reply's ops are applied, so one reply can
change both consistently:

- A new `order` with a boundary inside any timelapse (old or new) is refused;
  the previous order stays. The refusal names the timelapse and the boundary.
- A new timelapse crossing a boundary of the order in force (after the above)
  is refused, the way a timelapse across a source boundary is today.

A boundary sits between display lines `y` and `y+1` whenever a range ends at
`y` (and `y` is not the last line). A timelapse `[p, q]` is split when
`p <= y < q`.

## 4. Silence lines at range edges

A silence line (`54: [silent 29.9s: …]`) is a line like any other, so a range
may start AND end on one. The silence plays with the range it is in.

### 4.1 `Segment` gets a gap edge

```python
@dataclass(frozen=True)
class Segment:
    stem: str
    lines: tuple[int, int] | None = None
    gap_end: bool = False   # the range ends on the silence AFTER lines[1]
```

Serialised the way `_director.json` already spells a silence edge:
`{"stem": "A", "lines": [31, "57~"]}`. `gap_end` on a source's last line means
nothing (there is no line after it) and is normalised away; so is `gap_end` on
a whole-source segment.

A range STARTING on a silence line needs no flag: the silence after `a-1`
already belongs to the segment starting at `a` — today's rule. The flag only
moves a silence backwards, onto the range it ends.

`validate_segments` is unchanged in lines (a gap is not a line). The flag is
consistent by construction: the silence after `b` belongs to the segment
ending `b~` if there is one, else to the segment starting at `b+1`.

### 4.2 The manifest's boundary

`intervals/manifest.py` places a boundary after line `b`:

- no flag (today): at `end(b)` — the silence goes with the next segment, and
  with it `b+1`'s run-up.
- `b~`: at `max(end(b), start(b+1) − intervals.keep_pre_margin)` — the silence
  stays with the segment it ends, and `b+1` still takes its own run-up with it.

Both sides of one boundary must agree, so `build_manifest` looks up, for a
segment starting at `a`, whether the segment covering `a-1` ends with `~`.
The manifest's JSON shape is unchanged (seconds), so `blender`, `publish`,
`cut_report` and everything else reading `timeline.json` do not change.

### 4.3 Display ↔ source

- Range end `y`: a speech line `b` → `(…, b)`; the silence after `b` →
  `(…, b~)`.
- Range start `x`: a speech line `a` → `a`; the silence after `a-1` → `a`.
- Source → display (seeding from the plan): `(a, b)` → from the silence line
  after `a-1` if there is one, else `a`, through `b` (or through the silence
  after `b` when `b~`).

Tiling in display numbers guarantees every converted order is consistent.

## 5. What the model sees each turn

`edit_state` keeps the op blocks in shooting order (they are addressed in
display numbers) and adds, when the order is not shooting order:

- **The order**: `Playback order: 1-40, 120-150, 41-119, 151-300`.
- **Every seam**: for each pair of consecutive ranges that are not consecutive
  in the view, the last line of one and the first of the next, quoted
  (`40 → 120: 「…」 → 「…」`), so a transition can be judged without
  reassembling the video.
- **"As it plays" in playback order**: `timeline_runs()` takes the order's
  boundaries as forced breaks, so every run lies inside one range, and the runs
  are listed range by range. The totals are unchanged by construction.
- The caption list in playback order.

The runtime does not depend on the order.

## 6. Outputs and the authority rule

- `ConversationResult.order: list[Segment]` (source coordinates, normalised).
- The stage writes `director/order.json` — `{"order": [...]}`, the same array
  shape as `plan.json`'s `order` plus the `~` spelling. Written on every run,
  including a disabled director (the seed passes through) and a conversation
  that ends badly (the order in force).
- `_resolve_order()` reads `director/order.json`, else the effective plan, else
  shooting order; validation and the whole-project fallback are unchanged.
- The director and `director_preview` no longer consult `_resolve_order()` for
  their view; they read it only as the seed / for the playback section.
- The authority rule becomes: the director decides the order; `intervals`
  converts it; `timeline.json` is the authority after that.
- `order_note` reports the director's order; its wording no longer says "the
  plan chose".

A hand-edited `order.json` is validated for coverage at `intervals`. It is not
checked against timelapses: a human who splits one means it.

## 7. Prompt

`DIRECTOR_PROMPT` has 182 characters of budget (the test ceiling is 6289).
The mechanics go in `REPLY_SHAPE`, which every turn carries: the `order` key,
whole replacement, coverage, silence lines travel with their range. The prompt
gets at most one Rules line, paid for by deletion if it does not fit.

## 8. Tests

- `order.py`: `gap_end` round-trip (`"57~"`), normalisation, labels.
- `manifest.py`: a `b~` boundary at `start(b+1) − keep_pre_margin`, clamped to
  `end(b)`; both neighbours agree; no-flag output byte-identical.
- `loop.py`: order parse/validation (gap, overlap, out of range, not a list),
  refusal keeps ops, source-boundary split, silence-line edges both ways,
  timelapse split refused in both directions, whole replacement, absent key.
- `run.py`: view is shooting order whatever the plan says; the seed from the
  plan; `ConversationResult.order`.
- `preview.py`: breaks split runs; playback-order listing sums to the same
  runtime; seams quoted.
- `stages.py`: `order.json` written (enabled, disabled, failed); `_resolve_order`
  precedence.

## 9. Decisions

1. A range may end on a silence line; the silence plays with that range (§4).
2. `order` may be sent on any turn.
3. An order that splits a timelapse is refused, not auto-split.

Out of scope: the planning turn, the human checkpoint, and removing `plan`'s
directions — later steps.
