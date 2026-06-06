# Overlay Text Marker Design

**Date:** 2026-05-29
**Status:** Approved

## Context

The pipeline lets a human author wrap transcript text in `_edits.txt` with
`<keep>...</keep>` (force-preserve audio) and `<speed factor="N.N">...</speed>`
(force-preserve audio plus playback-speed change in Stage 5). Both markers
manipulate audio/timing only — there is currently no way to add **on-screen
annotation text** that is independent of the auto-generated captions.

This change adds a new `<overlay text="...">...</overlay>` marker that places
a TEXT strip in the Blender VSE at the wrapped span's time range, **without**
altering audio retention or cut behavior. It enables chapter labels, comments,
and other arbitrary annotations.

## Syntax

Human-authored `_edits.txt`:

```
普通のテキスト<overlay text="第2章">この部分にラベルを出したい</overlay>続き
```

- `text` attribute is required, double-quoted. Quotes inside the value are not
  supported (regex uses `[^"]*`); document this limit in `CLAUDE.md`.
- Span is defined by wrapped transcript words: first wrapped word `start` →
  last wrapped word `end`, resolved with the same anchor logic as `<keep>` /
  `<speed>`.
- Multi-line spans are supported (open in one segment, close in a later one).
- Coexists with `<keep>`, `<speed>`, and `{{old->new}}` markers in the same line.
- Added by the human *after* the LLM filter; LLM never sees it.
- **No audio side-effect.** Overlays do not force-keep audio. If the wrapped
  audio is cut by silence detection, the overlay text is silently skipped in
  Stage 5 (it cannot display over content that does not exist on the timeline).
- Nested / unclosed / unmatched / empty-resolved → log warning, skip
  (mirrors `<keep>` / `<speed>` error handling).

## Data Flow

### Stage 3 — `src/nagare_clip/stage3/sync_json.py`

**Regex additions** (alongside existing `KEEP_TAG_RE` / `SPEED_TAG_RE`):

```python
OVERLAY_TAG_RE   = re.compile(r'<overlay\s+text="[^"]*">|</overlay>')
_OVERLAY_SPLIT_RE = re.compile(r'(<overlay\s+text="[^"]*">|</overlay>)')
_OVERLAY_OPEN_RE  = re.compile(r'<overlay\s+text="([^"]*)">')
```

**`sync_text_to_json()` update:** strip `<overlay ...>` and `</overlay>` tags
from segment text (same place `<keep>` / `<speed>` tags are stripped) so the
synced JSON text does not contain marker syntax and patch decomposition is not
disturbed.

**New function** `extract_overlay_ranges(edit_lines, synced_json) → List[Tuple[float, float, str]]`:

- Mirrors `extract_speed_ranges()` but parses `<overlay text="...">` open tags.
- Returns `(start_time, end_time, overlay_text)` triples.
- Reuses `_resolve_keep_range()`, `_first_word_at_or_after()`, `_last_word_before()`,
  and `_patched_visible_length()` unchanged.

### Stage 4 — `src/nagare_clip/cli.py`

After the existing `extract_speed_ranges()` call (~line 217):

```python
overlay_ranges = extract_overlay_ranges(edit_lines, whisperx_data)
```

Overlays do **not** participate in `subtract_intervals()` or
`keep_intervals_dicts` annotation — they have no effect on audio retention or
speed. They are written to the output JSON as a new top-level key:

```json
{
  "source": "...",
  "keep_intervals": [...],
  "captions": [...],
  "overlays": [
    {"start": 12.5, "end": 15.0, "text": "第2章"}
  ]
}
```

If `overlay_ranges` is empty, the `overlays` key is **omitted** entirely
(backward-compatible — existing JSON outputs and test fixtures are unchanged).

### Stage 5 — `src/nagare_clip/stage4/timeline.py`

**New function** `place_overlays(...)`, parallel to `place_captions()`:

```python
def place_overlays(
    scene,
    sequence_collection,
    overlays: List[Dict],        # [{"start", "end", "text"}, ...]
    timeline_map: List[Dict],    # output of build_timeline_map()
    effective_fps: float,
    overlay_style: Dict,
    channel: int,                # dedicated overlay channel
    src_start_cursor: float = 0.0,  # for multi-source concatenation
) -> None
```

For each overlay:

1. Find the timeline-map entry whose `(src_start, src_end)` covers the overlay
   time range (or partially overlaps it).
2. Map overlay times through that entry, accounting for `speed_factor` (same
   trick `place_captions()` uses):
   ```python
   speed = float(entry.get("speed_factor", 1.0))
   offset_start = sec_to_frames((clamped_start - entry["src_start"]) / speed, effective_fps)
   offset_end   = sec_to_frames((clamped_end   - entry["src_start"]) / speed, effective_fps)
   ```
3. Clamp to the entry's timeline frame range. If the overlay falls entirely on
   cut content (no overlapping `timeline_map` entry), skip silently.
4. Create a TEXT strip with `overlay.text` on the dedicated overlay channel,
   styled per `overlay_style`.

**Channel layout** (extends existing):

| Channel | Content |
|--------|---------|
| `new_video.channel` | video |
| `new_video.channel + 1` | audio |
| `new_video.channel + 2` | speed effect (when present) |
| `caption_channel` | auto captions (existing) |
| **`caption_channel + 1`** | **overlays (new)** |

**`src/nagare_clip/stage4/blender_cli.py` change:** read `overlays` from the
intervals JSON (default `[]` if missing) and call `place_overlays()` after
`place_captions()`, passing the same `src_start_cursor` / `idx_offset` used by
captions so multi-source concatenation works.

## Config

**`config.example.yml` addition:**

```yaml
blender:
  # existing caption_style: { ... }
  overlay_style:
    # Inherits all fields from caption_style; override any field here.
    # Defaults anchor at the top of the frame so overlays don't collide
    # with bottom-anchored captions.
    anchor_y: TOP
    location_y: 0.95
    # font_size: 50
    # alignment_x: CENTER
    # location_x: 0.5
```

**`src/nagare_clip/config.py`:** add `blender.overlay_style` to `DEFAULTS` with
the top-of-frame defaults shown above. At strip-placement time, effective style
is computed in `blender_cli.py` as:

```python
ov_style = {**cfg["blender"]["caption_style"], **cfg["blender"]["overlay_style"]}
```

(`overlay_style` overrides `caption_style` per key; `place_overlays` consumes
the resolved dict.)

## Files to Modify

| File | Change |
|------|--------|
| `src/nagare_clip/stage3/sync_json.py` | Add overlay regexes, `extract_overlay_ranges()`; strip overlay tags in `sync_text_to_json()` |
| `src/nagare_clip/cli.py` | Call `extract_overlay_ranges()`; write `overlays` key to output JSON (omit when empty) |
| `src/nagare_clip/stage4/timeline.py` | Add `place_overlays()` parallel to `place_captions()`, speed-aware |
| `src/nagare_clip/stage4/blender_cli.py` | Read `overlays` from JSON; invoke `place_overlays()` after `place_captions()`; multi-source aware |
| `src/nagare_clip/config.py` | Add `blender.overlay_style` to `DEFAULTS` |
| `config.example.yml` | Document `blender.overlay_style` |

## Tests

- `tests/stage3/test_overlay_markers.py` (new) — `TestSyncStripsOverlayTags` and
  `TestExtractOverlayRanges`, mirroring existing `<keep>` / `<speed>` test
  classes: single-line, cross-line, nested, unclosed, unmatched, empty resolved,
  coexistence with `<keep>` / `<speed>` / `{{old->new}}`, multiple overlays per
  line.
- `tests/test_cli_overlay_markers.py` (new) — integration tests asserting:
  - `overlays` key appears in output JSON with correct `(start, end, text)`.
  - `overlays` key is absent when no overlay markers are present.
  - Overlay markers do **not** affect `keep_intervals` (control test against
    a span that would otherwise be cut — confirm it is still cut).
- `tests/stage4/test_overlays.py` (new) — unit tests for `place_overlays()`
  using a mock `sequence_collection`: TEXT strip created at expected frame
  range, speed-factor scaling applied, skipped when overlay falls in cut
  region, multi-source `src_start_cursor` offset is correct.

Per project TDD guidance: for each new test, mutate the implementation (return
empty list, off-by-one in frame mapping, ignore `overlay_style`) and confirm
the test fails before reverting.

## CLAUDE.md / README.md / plan.md Updates

- `CLAUDE.md`: add `<overlay text="...">...</overlay>` to Stage 3 description
  alongside `<keep>` / `<speed>`; note text-display-only behavior (no audio
  force-keep), multi-line support, double-quote limitation in `text`, and the
  new `overlays` JSON key.
- `README.md`: document the new marker in the Human Editing Workflow section.
- `plan.md`: record the new marker in implementation status.

## Verification

1. **Unit tests:** `uv run pytest tests/stage3/test_overlay_markers.py tests/stage4/test_overlays.py`
2. **Integration tests:** `uv run pytest tests/test_cli_overlay_markers.py`
3. **Full suite regression:** `uv run pytest`
4. **TDD mutation check:** for each new test, verify it fails against a broken
   implementation before passing against the real one. Report mutation-catch
   evidence alongside the green result.
5. **End-to-end smoke test:** run
   `./scripts/run_pipeline.sh --source input/<sample>.mp4 --from-stage 3`
   against a sample with a hand-added `<overlay text="Test">...</overlay>`
   marker in `_edits.txt`; open the resulting `.blend` in Blender and verify:
   - Overlay TEXT strip appears at the correct timeline frames.
   - Strip is on `caption_channel + 1` (above captions).
   - Wrapped audio is **not** force-kept (control: place overlay over a
     silence span and confirm the silence is still cut).
