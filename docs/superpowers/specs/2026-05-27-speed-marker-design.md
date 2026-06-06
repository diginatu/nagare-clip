# Speed Marker Design

**Date:** 2026-05-27
**Status:** Approved

## Context

The pipeline already supports `<keep>...</keep>` in `_edits.txt` to force-preserve audio
in regions that silence detection would otherwise cut. This feature adds a companion
`<speed factor="N.N">...</speed>` marker that force-preserves audio AND instructs
Stage 5 (Blender VSE) to play that region at a different speed. `<keep>` is retained
unchanged as a shorthand for force-keep without any speed effect.

## Syntax

Human-authored `_edits.txt`:

```
普通のテキスト<speed factor="2.0">倍速にしたい部分です</speed>そして続き
```

- `factor` is a positive float (e.g., `2.0` = double speed, `0.5` = half speed)
- Multi-line spans are supported: `<speed>` may open on one segment line and `</speed>` close on a later one, exactly like `<keep>`
- Nested or unclosed tags are skipped with a warning, matching `<keep>` error handling
- `<keep>` remains unchanged and is not deprecated

## Data Flow

### Stage 3 — `sync_json.py`

New function `extract_speed_ranges(edit_lines, synced_json) → List[Tuple[float, float, float]]`:

- Mirrors `extract_keep_ranges()` but parses `<speed factor="...">` opening tags with a regex like `r'<speed\s+factor="([0-9.]+)">'`
- Returns `(start_time, end_time, speed_factor)` triples
- Reuses `_resolve_keep_range()`, `_first_word_at_or_after()`, `_last_word_before()`, and `_patched_visible_length()` unchanged
- `sync_text_to_json()` must also strip `<speed ...>` and `</speed>` tags from the corrected text output (same treatment as `<keep>` tags)

### Stage 4 — `cli.py`

1. Call both `extract_keep_ranges()` and `extract_speed_ranges()`:
   ```python
   force_keep_ranges = extract_keep_ranges(edit_lines, whisperx_data)
   speed_ranges = extract_speed_ranges(edit_lines, whisperx_data)
   all_force_keep = force_keep_ranges + [(s, e) for s, e, _ in speed_ranges]
   ```
2. Use `all_force_keep` for `subtract_intervals()` — both `<keep>` and `<speed>` regions are carved from excludes.
3. After finalizing `keep_intervals_dicts` (post-margin, post-caption-expansion, post-min-keep), annotate each interval with `speed_factor`:
   - For each `keep_intervals_dicts` item `[ki_start, ki_end]`, iterate speed ranges and compute overlap `max(0, min(ki_end, sr_end) - max(ki_start, sr_start))`.
   - Pick the speed range with the largest overlap; if > 0, add `"speed_factor": factor` to the dict.
   - Omit `speed_factor` for intervals with no speed range overlap (clean JSON).

JSON output change — `keep_intervals` items gain an optional field:
```json
"keep_intervals": [
  {"start": 0.0, "end": 2.5},
  {"start": 4.0, "end": 11.0, "speed_factor": 2.0},
  {"start": 15.0, "end": 20.0}
]
```

### Stage 5 — `timeline.py`

**`build_timeline_map(keep_intervals, ...)`**:
- For each interval: `speed = interval.get("speed_factor", 1.0)`
- `src_frames = src_end_frame - src_start_frame` (unchanged frame arithmetic)
- `keep_frame_count = max(1, round(src_frames / speed))` — shortened for speed > 1
- Include `"speed_factor"` in `tl_map` entries so `place_captions()` can use it

**`place_strips(keep_intervals, ...)`**:
- For each interval: `speed = interval.get("speed_factor", 1.0)`
- `adjusted_frame_count = max(1, round(keep_frame_count / speed))`
- Advance cursor: `timeline_cursor += adjusted_frame_count` (replaces the old `+= keep_frame_count`)
- When `speed != 1.0`, add a Speed Control effect strip over the video strip immediately after configuring the duplicated strips; `cursor_start` is the cursor value before the advance:
  ```python
  speed_strip = sequence_collection.new_effect(
      name=f"speed_{idx:04d}",
      type='SPEED',
      channel=new_video.channel + 1,
      frame_start=cursor_start,
      frame_end=cursor_start + adjusted_frame_count - 1,
      seq1=new_video,
  )
  speed_strip.use_default_fade = False
  speed_strip.speed_factor = speed
  ```

**`place_captions(captions, tl_map, ...)`**:
- Read `speed = entry.get("speed_factor", 1.0)` from the matching `tl_map` entry
- Divide source-time offsets by speed:
  ```python
  offset_start = sec_to_frames((clamped_start - entry["src_start"]) / speed, effective_fps)
  offset_end   = sec_to_frames((clamped_end   - entry["src_start"]) / speed, effective_fps)
  ```

`blender_cli.py` requires no changes — it already passes `keep_intervals` through as-is.

## Files to Modify

| File | Change |
|------|--------|
| `src/nagare_clip/stage3/sync_json.py` | Add `extract_speed_ranges()`; update `sync_text_to_json()` to strip `<speed>` tags |
| `src/nagare_clip/cli.py` | Call `extract_speed_ranges()`; union force-keep ranges; annotate keep intervals with `speed_factor` |
| `src/nagare_clip/stage4/timeline.py` | `build_timeline_map()`, `place_strips()`, `place_captions()` — speed-aware frame counting and Speed Control effect |

## Tests

- `tests/stage3/test_keep_markers.py` — add `TestSyncStripsSpeedTags` and `TestExtractSpeedRanges` mirroring existing `<keep>` test classes
- `tests/test_cli_keep_markers.py` — add integration tests asserting `keep_intervals` gains `speed_factor` for speed-marked spans and the span is still force-kept
- `tests/stage4/test_speed_strips.py` (new) — unit tests for `place_strips()` and `build_timeline_map()` using a mock `sequence_collection`; assert Speed Control effect is added for speed≠1.0, cursor advances correctly, and caption offsets are halved/doubled as expected

## CLAUDE.md / README.md Updates

- `CLAUDE.md`: Add `<speed factor="N.N">...</speed>` to Stage 3 description alongside `<keep>`; note multi-line support, force-keep behavior, and that `speed_factor` appears in `keep_intervals` JSON
- `README.md`: Document the new marker in the Human Editing Workflow section
