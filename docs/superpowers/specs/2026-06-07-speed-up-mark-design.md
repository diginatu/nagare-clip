# Speed-up Mark Overlay — Design

**Date:** 2026-06-07
**Status:** Approved

## Goal

Show an on-screen badge (e.g. `x2.0`) during every sped-up region in the final
Blender rough cut, so a viewer/editor can see at a glance that a region plays at
an altered speed. The mark is auto-derived from existing `<speed factor="N.N">`
data — no new authoring in `_edits.txt`.

## Scope / Non-goals

- Stages 1–4 are unchanged. `speed_ranges` (`{start, end, factor}`) already
  lands in the intervals JSON from Stage 4; this feature only consumes it.
- Only Stage 5 (Blender VSE layout) changes, plus config and docs.
- No new pipeline outputs, no changes to `<speed>` retiming behavior.

## Decisions (from brainstorming)

| Question        | Decision                                                |
|-----------------|---------------------------------------------------------|
| Mark content    | Configurable template, default `"x{factor}"` → `x2.0`   |
| Trigger         | Auto from **every** `<speed>` region                    |
| Placement       | New dedicated **channel 5** (above overlay ch4 / caption ch3) |
| Default state   | **Enabled** by default; `blender.speed_mark.enabled` toggles |
| Default style   | Small **top-right** badge (`font_size: 35`, `alignment_x: RIGHT`, `location` ~0.95/0.95) |
| Factor format   | One decimal — `2.0` (rendered via `f"{factor:.1f}"`)    |

## Data Flow

1. Stage 4 writes `speed_ranges: [{start, end, factor}, ...]` to the intervals
   JSON (already implemented).
2. Stage 5 `blender_cli.py` reads `speed_ranges`. When
   `blender.speed_mark.enabled` is true, after `place_overlays()` it calls the
   new `place_speed_marks()` once per source (same wiring pattern as overlays).

Because `<speed>` audio is force-kept (subtracted from excludes in Stage 4), a
matching keep interval should always exist for each speed range. The no-match
path is still handled defensively (log + skip), mirroring `place_overlays()`.

## New Function

`place_speed_marks(speed_ranges, tl_map, effective_fps, sequence_collection, *,
template, mark_style, channel=5)` in `src/nagare_clip/stage4/timeline.py`.

Behavior mirrors `place_overlays()`:

- For each speed range, map `(start, end)` source time through `tl_map`,
  accumulating min `tl_start` / max `tl_end` across every matching (speed-aware)
  keep interval so a span crossing multiple intervals renders as one contiguous
  TEXT strip. Source-time offsets are divided by each interval's `speed_factor`
  (same trick as captions/overlays).
- Text = `template.format(factor=f"{factor:.1f}")`. Default template `"x{factor}"`
  → `"x2.0"`.
- Create a `TEXT` effect strip on `channel` (default 5).
- Apply style: `caption_style` defaults overlaid with `blender.speed_mark`
  overrides (overrides win per key), same resolution as overlay style.
- If no matching interval: `logging.warning(...)` and skip.

## Config

New section under `blender`:

```yaml
speed_mark:
  enabled: true
  template: "x{factor}"
  # style overrides applied on top of caption_style (top-right, small badge):
  font_size: 35
  alignment_x: RIGHT
  anchor_y: TOP
  location_x: 0.95
  location_y: 0.95
```

`DEFAULTS["blender"]["speed_mark"]` in `config.py` holds these; documented in
`config.example.yml`. In `blender_cli.py`, resolve the style the same way as
overlays: `mark_style = {**caption_style, **speed_mark_style_overrides}` where
the style overrides exclude the non-style keys `enabled` and `template`.

## Testing (TDD)

Unit tests in `tests/stage4/`:

1. Factor formatting: `factor=2.0` with template `"x{factor}"` → strip text `"x2.0"`.
2. Template substitution: custom template e.g. `"⏩{factor}x"` honored.
3. Placement on channel 5.
4. Multi-interval contiguous span: a speed range spanning two keep intervals
   yields a single strip from min `tl_start` to max `tl_end`.
5. Speed-aware timeline offset (offsets divided by `speed_factor`).
6. `enabled: false` → `place_speed_marks` not called / no strip created
   (blender_cli wiring level, or a guard test).

Per repo TDD rule, each behavior is mutation-verified (break impl, see the test
fail, revert) and that evidence reported alongside the green run.

## Docs to Update

- `README.md` — user-facing: speed marks appear automatically for `<speed>`
  regions; how to disable/restyle.
- `plan.md` — implementation status.
- `CLAUDE.md` (AGENTS.md) — Stage 5 guardrails / runtime quirks entry.
- `config.example.yml` — documented `speed_mark` keys with defaults.
