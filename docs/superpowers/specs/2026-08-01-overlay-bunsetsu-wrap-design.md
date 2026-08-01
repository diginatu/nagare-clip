# Overlay text bunsetsu-spacing (Improvement 12)

## Problem

Blender's TEXT strip wraps only at spaces. Captions get spaces for free because
`intervals.captions.collect_captions()` joins bunsetsu units with
`intervals.caption.bunsetu_separator` (default `" "`). Overlay text
(`<overlay text="..." duration="N.N"/>`) does not go through that path — it is
written by the director, carried verbatim through `extract_overlay_marks()`,
and reaches the TEXT strip with no spaces at all. Long overlays (recap
captions observed up to 28 characters) can run off the frame instead of
wrapping.

## Root cause detail

`intervals/bunsetu.py`'s existing bunsetsu machinery
(`build_bunsetu_times`) is timing-driven: it maps GiNZA bunsetsu spans back
onto WhisperX per-character start times. Overlay text has no such timing (it
is free text written by the director, not necessarily verbatim from the
transcript), so that function doesn't apply directly. What's needed is a
timing-free variant: segment plain text into bunsetsu and join with a
separator.

## Design

### New function: `intervals/bunsetu.py::bunsetu_join_text`

```python
def bunsetu_join_text(text: str, nlp: spacy.language.Language, separator: str = " ") -> str:
```

- Splits `text` on `"\n"` first, so multi-line overlay captions (a human/LLM
  can already write `\n` inside an overlay's escaped text — see
  `escape_overlay_text`/`unescape_overlay_text` in `intervals/sync_json.py`)
  keep their author-chosen line breaks; bunsetsu segmentation runs
  independently per line.
- For each non-empty line: `doc = nlp(line)`, then
  `separator.join(span.text for span in ginza.bunsetu_spans(doc))`.
- An empty line (or empty overall `text`) passes through unchanged.
- Rejoins lines with `"\n"`.

Pure function, same shape as the existing `flatten_bunsetu`/`build_bunsetu_times`
(lazy `import ginza` inside the function, `nlp` passed in so callers control
model loading).

### Wiring: `intervals/run.py::run_intervals`

`run_intervals` already loads `nlp = spacy.load("ja_ginza")` once (for
caption bunsetsu timing) and already computes `overlay_marks` (via
`extract_overlay_marks` + `snap_overlay_starts`). Add one mapping step, after
`snap_overlay_starts` and before building `output_data["overlays"]`:

```python
overlay_marks = [
    (start, duration, bunsetu_join_text(text, nlp, cap["bunsetu_separator"]))
    for start, duration, text in overlay_marks
]
```

Reuses `cap["bunsetu_separator"]` (`intervals.caption.bunsetu_separator`,
default `" "`) — no new config key. Decision: overlays share the caption
separator rather than getting an independent knob; simpler config surface,
revisit only if a real project needs to diverge.

### Why intervals, not blender

Blender runs `blender/blender_cli.py` in its own bundled Python
(`sys.path.insert(0, str(_SRC))` only adds the pure-Python `src/` tree —
`ginza`/`ja_ginza` are not on Blender's Python path). The intervals stage is
the host `uv run` process that already has GiNZA available and already owns
the `overlays` array it writes into `_intervals.json`. Segmenting there means
Blender keeps rendering the `text` field verbatim, unchanged.

### Non-goals

- No change to the `<overlay .../>` tag syntax, `escape_overlay_text`, or
  `duration` semantics.
- No change to `blender/timeline.py::place_overlays` — it already renders
  whatever `text` string it's given.
- No independent overlay separator config key (see Decision above).

## Testing

- `tests/intervals/test_bunsetu.py`: new unit tests for `bunsetu_join_text`
  — multi-bunsetsu single line gets spaced, an explicit `\n` line break is
  preserved (not merged into one GiNZA parse), and empty text passes through
  unchanged.
- `tests/intervals/test_run_overlay_markers.py`: this file already stubs
  `stage_run.spacy.load` to return a dummy `object()` (since it isn't
  testing bunsetsu timing) and monkeypatches `build_bunsetu_times` to `[]`
  for the same reason. `bunsetu_join_text` would call `nlp(line)` on that
  dummy and raise `TypeError`, so these tests need `bunsetu_join_text`
  monkeypatched too (identity function), the same pattern already used for
  `build_bunsetu_times`. No other test file drives overlay text through
  `run_intervals` with a stubbed `nlp` (`test_overlay_snap.py` and
  `test_overlay_markers.py` test `snap_overlay_starts`/
  `extract_overlay_marks` directly, not through `run_intervals`).

## Docs to update alongside implementation

- `docs/stages/intervals.md` — overlay paragraph, note the bunsetsu-spacing
  step and that it reuses `caption.bunsetu_separator`.
- `AGENTS.md` — intervals stage overview, one clause on overlay text now
  being bunsetsu-spaced for wrapping.
