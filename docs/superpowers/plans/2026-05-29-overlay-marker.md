# Overlay Marker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `<overlay text="...">...</overlay>` marker in `_edits.txt` that places a TEXT strip in the Blender VSE at the wrapped span's time range, without affecting audio retention or cut behavior.

**Architecture:** Three layers mirror the existing `<keep>` / `<speed>` flow. (1) Stage 3 `sync_json.py` parses the marker and resolves it to `(start, end, text)` triples via the same word-anchor logic. (2) Stage 4 `cli.py` writes the triples to a new `overlays` key in the output JSON. (3) Stage 5 `timeline.py` adds `place_overlays()`, parallel to `place_captions()`, which maps each overlay through the timeline map (speed-aware) and creates a TEXT strip on a new dedicated channel.

**Tech Stack:** Python 3, `uv` for invocation, `pytest`, Blender Python (`bpy`) for Stage 5.

**Reference spec:** `docs/superpowers/specs/2026-05-29-overlay-marker-design.md`

---

## File Structure

| Path | Action | Responsibility |
|------|--------|----------------|
| `src/nagare_clip/stage3/sync_json.py` | Modify | Add overlay regexes; strip overlay tags in `sync_text_to_json()`; add `extract_overlay_ranges()` |
| `src/nagare_clip/cli.py` | Modify | Call `extract_overlay_ranges()`; write `overlays` key to output JSON when non-empty |
| `src/nagare_clip/config.py` | Modify | Add `blender.overlay_style: {}` to `DEFAULTS` |
| `src/nagare_clip/stage4/timeline.py` | Modify | Add `place_overlays()` parallel to `place_captions()`, speed-aware |
| `src/nagare_clip/stage4/blender_cli.py` | Modify | Read `overlays` from JSON; invoke `place_overlays()` after `place_captions()` |
| `config.example.yml` | Modify | Document `blender.overlay_style` |
| `tests/stage3/test_overlay_markers.py` | Create | Unit tests for `sync_text_to_json` tag stripping + `extract_overlay_ranges` |
| `tests/test_cli_overlay_markers.py` | Create | Integration test: `overlays` key in output JSON; no effect on `keep_intervals` |
| `tests/stage4/test_overlays.py` | Create | Unit tests for `place_overlays()` with mocked `sequence_collection` |
| `CLAUDE.md` | Modify | Document new marker alongside `<keep>` / `<speed>` |
| `README.md` | Modify | Document new marker in Human Editing Workflow |
| `plan.md` | Modify | Record new marker in implementation status |

---

## Task 1 — Stage 3: Add overlay tag regexes and strip them in `sync_text_to_json`

**Files:**
- Modify: `src/nagare_clip/stage3/sync_json.py:26-28` (add overlay regexes near `SPEED_TAG_RE`)
- Modify: `src/nagare_clip/stage3/sync_json.py:211-213` (extend cleaning pass in `sync_text_to_json`)
- Modify: `src/nagare_clip/stage3/sync_json.py:253` (extend cleaning pass in `_patched_visible_length`)
- Test: `tests/stage3/test_overlay_markers.py` (new file)

- [ ] **Step 1: Write the failing test for sync_text_to_json tag stripping**

Create `tests/stage3/test_overlay_markers.py` with this content:

```python
"""Tests for <overlay text="...">...</overlay> marker handling in Stage 3 sync."""

from __future__ import annotations

import pytest

from nagare_clip.stage3.sync_json import (
    extract_overlay_ranges,
    sync_text_to_json,
)


def _word(char: str, start: float, end: float, score: float = 0.9) -> dict:
    return {"word": char, "start": start, "end": end, "score": score}


def _segment(text: str, words: list) -> dict:
    return {"text": text, "start": words[0]["start"], "end": words[-1]["end"], "words": words}


def _whisperx(*segments: dict) -> dict:
    all_words: list = []
    for s in segments:
        all_words.extend(s["words"])
    return {"segments": list(segments), "word_segments": all_words}


# --- sync_text_to_json strips <overlay> tags from corrected text ---


class TestSyncStripsOverlayTags:
    def test_pure_overlay_only_line_keeps_words_unchanged(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
        ]
        data = _whisperx(_segment("あいう", words))
        result = sync_text_to_json(data, ['あ<overlay text="X">い</overlay>う'])
        assert result["segments"][0]["text"] == "あいう"
        assert result["segments"][0]["words"] == words

    def test_overlay_tag_with_patch_inside(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("え", 0.2, 0.4),
            _word("ー", 0.4, 0.6),
            _word("う", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あえーう", words))
        result = sync_text_to_json(
            data, ['あ<overlay text="X">{{えー->}}</overlay>う']
        )
        assert result["segments"][0]["text"] == "あう"
        assert [w["word"] for w in result["segments"][0]["words"]] == ["あ", "う"]

    def test_unclosed_overlay_tag_is_stripped(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        result = sync_text_to_json(data, ['あ<overlay text="X">い'])
        assert result["segments"][0]["text"] == "あい"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stage3/test_overlay_markers.py::TestSyncStripsOverlayTags -v`
Expected: FAIL — `ImportError: cannot import name 'extract_overlay_ranges'`

- [ ] **Step 3: Add regex constants and extend stripping**

In `src/nagare_clip/stage3/sync_json.py`, after line 28 (`_SPEED_OPEN_RE = ...`), insert:

```python
# <overlay text="...">...</overlay> markers: place a Blender VSE TEXT strip
# at the wrapped span's time range. Unlike <keep>/<speed>, overlay does NOT
# affect audio retention; if the wrapped audio is cut, the overlay is skipped
# in Stage 5.
OVERLAY_TAG_RE = re.compile(r'<overlay\s+text="[^"]*">|</overlay>')
_OVERLAY_SPLIT_RE = re.compile(r'(<overlay\s+text="[^"]*">|</overlay>)')
_OVERLAY_OPEN_RE = re.compile(r'<overlay\s+text="([^"]*)">')
```

Then in `sync_text_to_json()` at line 211-213, change:

```python
    cleaned_lines = [
        SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line)) for line in edit_lines
    ]
```

to:

```python
    cleaned_lines = [
        OVERLAY_TAG_RE.sub(
            "", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))
        )
        for line in edit_lines
    ]
```

And in `_patched_visible_length()` at line 253, change:

```python
    cleaned = SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", text))
```

to:

```python
    cleaned = OVERLAY_TAG_RE.sub(
        "", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", text))
    )
```

- [ ] **Step 4: Add stub `extract_overlay_ranges` so the test module imports**

In `src/nagare_clip/stage3/sync_json.py`, at the bottom of the file, append:

```python
def extract_overlay_ranges(
    edit_lines: List[str], synced_json: Dict[str, Any]
) -> List[Tuple[float, float, str]]:
    """Stub — implemented in Task 2."""
    return []
```

- [ ] **Step 5: Run tag-stripping tests to verify they pass**

Run: `uv run pytest tests/stage3/test_overlay_markers.py::TestSyncStripsOverlayTags -v`
Expected: PASS — 3 tests green.

- [ ] **Step 6: Mutation check — confirm tests catch a regression**

Temporarily revert the `OVERLAY_TAG_RE.sub(...)` addition in `sync_text_to_json()` so overlay tags are *not* stripped, then re-run the tests. Expected: all three tests fail (text comparison shows `<overlay text="X">` leakage). Revert your mutation and re-run; tests must pass again.

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/stage3/sync_json.py tests/stage3/test_overlay_markers.py
git commit -m "feat: strip <overlay text=...> tags in sync_text_to_json"
```

---

## Task 2 — Stage 3: Implement `extract_overlay_ranges()`

**Files:**
- Modify: `src/nagare_clip/stage3/sync_json.py` (replace stub with real implementation)
- Test: `tests/stage3/test_overlay_markers.py` (append `TestExtractOverlayRanges` class)

- [ ] **Step 1: Write failing tests for `extract_overlay_ranges`**

Append to `tests/stage3/test_overlay_markers.py`:

```python
# --- extract_overlay_ranges ---


class TestExtractOverlayRanges:
    def test_no_overlay_tags_returns_empty(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        assert extract_overlay_ranges(["あい"], data) == []

    def test_single_overlay_block_returns_triple(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.5),
            _word("う", 0.5, 0.9),
            _word("え", 0.9, 1.2),
        ]
        data = _whisperx(_segment("あいうえ", words))
        ranges = extract_overlay_ranges(
            ['あ<overlay text="Chapter 1">いう</overlay>え'], data
        )
        assert ranges == [(0.2, 0.9, "Chapter 1")]

    def test_multiple_overlay_blocks_emit_multiple_triples(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
            _word("お", 0.8, 1.0),
        ]
        data = _whisperx(_segment("あいうえお", words))
        ranges = extract_overlay_ranges(
            ['<overlay text="A">あ</overlay>い<overlay text="B">うえ</overlay>お'],
            data,
        )
        assert ranges == [(0.0, 0.2, "A"), (0.4, 0.8, "B")]

    def test_overlay_spanning_multiple_lines(self):
        seg1_words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        seg2_words = [_word("う", 1.0, 1.2), _word("え", 1.2, 1.4)]
        data = _whisperx(
            _segment("あい", seg1_words),
            _segment("うえ", seg2_words),
        )
        ranges = extract_overlay_ranges(
            ['あ<overlay text="X">い', "う</overlay>え"], data
        )
        # First wrapped word "い" → 0.2; last wrapped word "う" → 1.2
        assert ranges == [(0.2, 1.2, "X")]

    def test_unclosed_overlay_is_skipped_with_warning(self, caplog):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        with caplog.at_level("WARNING"):
            ranges = extract_overlay_ranges(['あ<overlay text="X">い'], data)
        assert ranges == []
        assert "Unclosed <overlay>" in caplog.text

    def test_unmatched_close_overlay_is_skipped_with_warning(self, caplog):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        with caplog.at_level("WARNING"):
            ranges = extract_overlay_ranges(["あい</overlay>"], data)
        assert ranges == []
        assert "Unmatched </overlay>" in caplog.text

    def test_nested_overlay_inner_opener_ignored(self, caplog):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        with caplog.at_level("WARNING"):
            ranges = extract_overlay_ranges(
                ['<overlay text="A">あい<overlay text="B">う</overlay>え</overlay>'],
                data,
            )
        # Outer span resolves; inner opener is warned and dropped
        assert ranges == [(0.0, 0.8, "A")]
        assert "Nested <overlay>" in caplog.text

    def test_overlay_coexists_with_keep_and_speed(self):
        words = [
            _word("あ", 0.0, 0.2),
            _word("い", 0.2, 0.4),
            _word("う", 0.4, 0.6),
            _word("え", 0.6, 0.8),
        ]
        data = _whisperx(_segment("あいうえ", words))
        # Overlay around い only; keep and speed wrappers around う
        line = '<overlay text="X">あい</overlay><keep>う</keep>え'
        ranges = extract_overlay_ranges([line], data)
        assert ranges == [(0.0, 0.4, "X")]

    def test_empty_text_attribute_resolves_to_empty_string(self):
        words = [_word("あ", 0.0, 0.2), _word("い", 0.2, 0.4)]
        data = _whisperx(_segment("あい", words))
        ranges = extract_overlay_ranges(['あ<overlay text="">い</overlay>'], data)
        # Overlay has no wrapped words → empty range, skipped
        # (this asserts the "</overlay>" placed right after "<overlay text>" with nothing wrapped is dropped)
        assert ranges == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/stage3/test_overlay_markers.py::TestExtractOverlayRanges -v`
Expected: 8 FAIL — the stub returns `[]` for every input, so all but `test_no_overlay_tags_returns_empty` and `test_empty_text_attribute_resolves_to_empty_string` fail with `assert [] == [...]`.

- [ ] **Step 3: Replace the stub with the real implementation**

In `src/nagare_clip/stage3/sync_json.py`, replace the stub `extract_overlay_ranges` with:

```python
def extract_overlay_ranges(
    edit_lines: List[str], synced_json: Dict[str, Any]
) -> List[Tuple[float, float, str]]:
    """Extract `(start, end, text)` triples from `<overlay text="...">...</overlay>` blocks.

    Behaves like :func:`extract_speed_ranges` for span resolution (multi-line
    spans, position tracking, error handling) but returns the overlay text
    parsed from each opening tag instead of a numeric factor.  Overlays do
    NOT affect audio retention; they are consumed by Stage 5 to place
    on-screen TEXT strips only.
    """
    segments = synced_json.get("segments", [])
    ranges: List[Tuple[float, float, str]] = []
    overlay_start: Optional[Tuple[int, int]] = None
    overlay_text: Optional[str] = None

    for seg_idx, line in enumerate(edit_lines):
        if seg_idx >= len(segments):
            break
        output_pos = 0
        for part in _OVERLAY_SPLIT_RE.split(line):
            open_match = _OVERLAY_OPEN_RE.fullmatch(part) if part else None
            if open_match is not None:
                if overlay_start is not None:
                    logger.warning(
                        "Nested <overlay> opener; ignoring inner tag"
                    )
                    continue
                overlay_start = (seg_idx, output_pos)
                overlay_text = open_match.group(1)
            elif part == "</overlay>":
                if overlay_start is None:
                    logger.warning("Unmatched </overlay>; ignoring")
                    continue
                resolved = _resolve_keep_range(
                    segments, overlay_start, (seg_idx, output_pos)
                )
                text = overlay_text
                overlay_start = None
                overlay_text = None
                if resolved is None or text is None:
                    logger.warning(
                        "<overlay> resolved to an empty/invalid range; ignoring"
                    )
                    continue
                start_t, end_t = resolved
                ranges.append((start_t, end_t, text))
            else:
                output_pos += _patched_visible_length(part)

    if overlay_start is not None:
        logger.warning("Unclosed <overlay>; ignoring")

    return ranges
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/stage3/test_overlay_markers.py -v`
Expected: PASS — all tests in both classes green (11 total).

- [ ] **Step 5: Mutation check — confirm tests catch regressions**

Pick three mutations and verify they break the suite, reverting after each:

1. Mutate `ranges.append((start_t, end_t, text))` → `ranges.append((start_t, end_t, ""))`. Re-run: `test_single_overlay_block_returns_triple` and `test_multiple_overlay_blocks_emit_multiple_triples` must fail. Revert.
2. Mutate `if overlay_start is not None:` (the nested-check at top of loop) → `if False:`. Re-run: `test_nested_overlay_inner_opener_ignored` must fail (different range emitted). Revert.
3. Mutate `if overlay_start is not None:` (the unclosed-check below the loops) → `if False:`. Re-run: `test_unclosed_overlay_is_skipped_with_warning` must fail (warning text missing). Revert.

After all reverts, run the full file once more — all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/stage3/sync_json.py tests/stage3/test_overlay_markers.py
git commit -m "feat: add extract_overlay_ranges for <overlay text=...> markers"
```

---

## Task 3 — Stage 4: Write `overlays` key to output JSON

**Files:**
- Modify: `src/nagare_clip/cli.py:28-32` (import `extract_overlay_ranges`)
- Modify: `src/nagare_clip/cli.py:215-223` (call `extract_overlay_ranges`)
- Modify: `src/nagare_clip/cli.py:376-381` (add `overlays` to `output_data` when non-empty)
- Test: `tests/test_cli_overlay_markers.py` (new file)

- [ ] **Step 1: Write the failing integration tests**

Create `tests/test_cli_overlay_markers.py`:

```python
"""Integration: <overlay text="...">...</overlay> markers in _edits.txt are
written to the output JSON's `overlays` key without affecting `keep_intervals`."""

import json
import sys

import yaml

import nagare_clip.cli as stage_cli


def _whisperx_with_silence():
    """One segment with a 3.9-second intra-segment silent gap.

    Word-gap silence detection (threshold 1.0s) will exclude (1.1, 5.0).
    """
    return {
        "duration": 10.0,
        "segments": [
            {
                "start": 0.0,
                "end": 5.6,
                "text": "あいうえ",
                "words": [
                    {"word": "あ", "start": 0.5, "end": 0.8},
                    {"word": "い", "start": 0.8, "end": 1.1},
                    {"word": "う", "start": 5.0, "end": 5.3},
                    {"word": "え", "start": 5.3, "end": 5.6},
                ],
            },
        ],
    }


def _config(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "intervals": {
                    "silence_threshold": 1.0,
                    "min_keep": 0.001,
                    "keep_pre_margin": 0.0,
                    "keep_post_margin": 0.0,
                }
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _run_cli(monkeypatch, tmp_path, edits_text: str):
    json_path = tmp_path / "in.json"
    json_path.write_text(json.dumps(_whisperx_with_silence()), encoding="utf-8")
    edits_path = tmp_path / "in_edits.txt"
    edits_path.write_text(edits_text, encoding="utf-8")
    out_path = tmp_path / "out.json"
    cfg_path = _config(tmp_path)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage_cli",
            "--edits-txt",
            str(edits_path),
            "--json",
            str(json_path),
            "--output",
            str(out_path),
            "--config",
            str(cfg_path),
        ],
    )
    stage_cli.main()
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_overlays_key_present_when_marker_used(monkeypatch, tmp_path):
    out = _run_cli(monkeypatch, tmp_path, 'あ<overlay text="Chapter 1">いう</overlay>え')
    assert "overlays" in out
    assert out["overlays"] == [
        {"start": 0.8, "end": 5.3, "text": "Chapter 1"}
    ]


def test_overlays_key_absent_without_markers(monkeypatch, tmp_path):
    out = _run_cli(monkeypatch, tmp_path, "あいうえ")
    assert "overlays" not in out


def test_overlay_does_not_affect_keep_intervals(monkeypatch, tmp_path):
    """Overlay wraps the silent gap; it must NOT force-keep audio. The gap
    (1.1, 5.0) is still excluded from keep_intervals."""
    out = _run_cli(monkeypatch, tmp_path, 'あ<overlay text="X">いう</overlay>え')
    keep = out["keep_intervals"]
    # No keep interval should cover the (1.1, 5.0) gap
    for iv in keep:
        assert not (iv["start"] <= 1.5 and iv["end"] >= 4.5), (
            f"Overlay accidentally force-kept gap: {iv}"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_overlay_markers.py -v`
Expected: FAIL — `test_overlays_key_present_when_marker_used` fails because no `overlays` key is written; the other two may pass coincidentally (no markers, or overlay has no audio effect by default).

- [ ] **Step 3: Wire `extract_overlay_ranges` into the CLI**

In `src/nagare_clip/cli.py`, change the import block at lines 28-32:

```python
from nagare_clip.stage3.sync_json import (
    extract_keep_ranges,
    extract_speed_ranges,
    sync_text_to_json,
)
```

to:

```python
from nagare_clip.stage3.sync_json import (
    extract_keep_ranges,
    extract_overlay_ranges,
    extract_speed_ranges,
    sync_text_to_json,
)
```

After the `extract_speed_ranges` call at line 217, add:

```python
    overlay_ranges = extract_overlay_ranges(edit_lines, whisperx_data)
    if overlay_ranges:
        logging.info("Overlay ranges from <overlay>: %d", len(overlay_ranges))
```

At the `output_data = { ... }` block (line 376-381), replace with:

```python
    output_data = {
        "source_file": infer_source_file(whisperx_data, json_path),
        "duration_sec": round(duration_sec, 3),
        "keep_intervals": keep_intervals_dicts,
        "captions": captions,
    }
    if overlay_ranges:
        output_data["overlays"] = [
            {"start": round(s, 3), "end": round(e, 3), "text": t}
            for s, e, t in overlay_ranges
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_overlay_markers.py -v`
Expected: PASS — all three tests green.

- [ ] **Step 5: Mutation check**

Two mutations, revert after each:

1. Change `if overlay_ranges:` (the output guard) → `if False:`. Re-run: `test_overlays_key_present_when_marker_used` must fail. Revert.
2. In `extract_overlay_ranges`, force `ranges.append(...)` to also append the range to a passed-in `all_force_keep`-style list (simulate accidentally force-keeping). Easier mutation: in `cli.py`, append overlay ranges to `all_force_keep` and re-run `test_overlay_does_not_affect_keep_intervals`. It must fail. Revert.

- [ ] **Step 6: Run the existing test suite to ensure no regressions**

Run: `uv run pytest tests/test_cli_keep_markers.py tests/test_cli_cuts_merge.py -v`
Expected: PASS — none of the existing CLI integration tests are affected (no JSON shape change when `overlays` is absent).

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/cli.py tests/test_cli_overlay_markers.py
git commit -m "feat: write <overlay> ranges to intervals JSON output"
```

---

## Task 4 — Config: add `blender.overlay_style` to DEFAULTS

**Files:**
- Modify: `src/nagare_clip/config.py:101-112` (extend `blender` section in `DEFAULTS`)
- Modify: `config.example.yml` (document new key)
- Test: existing `tests/test_config.py` will exercise it (no new test needed for a dict default; Task 5 covers actual style application)

- [ ] **Step 1: Add `overlay_style` to `DEFAULTS`**

In `src/nagare_clip/config.py`, in the `blender` block at lines 101-112, after the `caption_style` dict closes (line 111), add a sibling `overlay_style` key. The full block becomes:

```python
    "blender": {
        "default_fps": 30.0,
        "use_proxy": True,
        "proxy_size": 100,
        "caption_style": {
            "font_size": 50,
            "alignment_x": "CENTER",
            "anchor_y": "BOTTOM",
            "location_x": 0.5,
            "location_y": 0.05,
        },
        "overlay_style": {
            "anchor_y": "TOP",
            "location_y": 0.95,
        },
    },
```

Rationale: anchor at top, near top of frame — overlays don't collide with bottom-anchored captions. Other style fields (font, color, etc.) inherit from `caption_style` at the strip-placement layer (Task 5).

- [ ] **Step 2: Document the key in `config.example.yml`**

Read the existing `config.example.yml` to find the `blender:` section, then add an `overlay_style:` key under `blender:` with the same structure as `caption_style` documented above. Use a block that mirrors the existing `caption_style` format:

```yaml
  # Overlay TEXT strip style for <overlay text="..."> markers in _edits.txt.
  # Any field not set here is inherited from caption_style.
  overlay_style:
    anchor_y: TOP        # default: TOP (overlays sit at top of frame)
    location_y: 0.95     # default: 0.95
    # font_size: 50      # inherits from caption_style if omitted
    # alignment_x: CENTER
    # location_x: 0.5
```

- [ ] **Step 3: Verify the config loads correctly**

Run: `uv run python -c "from nagare_clip.config import get_effective_config; c = get_effective_config(None); print(c['blender']['overlay_style'])"`
Expected output: `{'anchor_y': 'TOP', 'location_y': 0.95}`

- [ ] **Step 4: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml
git commit -m "feat: add blender.overlay_style config defaults"
```

---

## Task 5 — Stage 5: Implement `place_overlays()` in `timeline.py`

**Files:**
- Modify: `src/nagare_clip/stage4/timeline.py` (add `place_overlays` function at end of file)
- Test: `tests/stage4/test_overlays.py` (new file)

- [ ] **Step 1: Write failing tests for `place_overlays`**

Create `tests/stage4/test_overlays.py`:

```python
"""Tests for place_overlays() — TEXT strip placement for <overlay> markers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.timeline import build_timeline_map, place_overlays


def _seq_with_capture():
    """Return (sequence_collection_mock, captured_kwargs_list)."""
    captured: list = []
    seq = MagicMock()

    def capture_effect(**kwargs):
        captured.append(kwargs)
        m = MagicMock()
        # Allow attribute assignment in place_overlays
        return m

    seq.new_effect = capture_effect
    return seq, captured


def _simple_tl_map(fps: float = 30.0):
    """One 4-second keep interval starting at source 0.0, timeline frame 1."""
    return build_timeline_map(
        [{"start": 0.0, "end": 4.0}], effective_fps=fps, source_fps=fps
    )


def test_overlay_within_keep_interval_creates_text_strip():
    fps = 30.0
    tl_map = _simple_tl_map(fps)
    overlays = [{"start": 1.0, "end": 3.0, "text": "Chapter 1"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    assert kw["type"] == "TEXT"
    assert kw["channel"] == 4
    assert kw["frame_start"] == 1 + 30  # 1.0s * 30fps offset within interval (tl_start=1)
    assert kw["length"] == 60           # 2.0s duration


def test_overlay_text_assigned_to_strip():
    fps = 30.0
    tl_map = _simple_tl_map(fps)
    overlays = [{"start": 0.5, "end": 1.5, "text": "Hello"}]
    seq = MagicMock()
    created = []

    def capture(**kwargs):
        m = MagicMock()
        created.append(m)
        return m

    seq.new_effect = capture
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={"font_size": 70, "location_y": 0.95},
        channel=4,
    )
    assert len(created) == 1
    text_strip = created[0]
    assert text_strip.text == "Hello"
    assert text_strip.font_size == 70
    assert text_strip.location[1] == 0.95


def test_overlay_outside_any_keep_interval_is_skipped():
    fps = 30.0
    tl_map = _simple_tl_map(fps)  # covers source 0-4s
    overlays = [{"start": 10.0, "end": 11.0, "text": "Lost"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert captured == []


def test_overlay_inside_sped_up_interval_scales_offsets():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    overlays = [{"start": 1.0, "end": 3.0, "text": "Fast"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    # speed=2.0 halves offsets: 1.0s/2.0 * 30fps = 15 frames offset from tl_start=1
    assert kw["frame_start"] == 1 + 15
    # length: 2.0s/2.0 * 30fps = 30 frames
    assert kw["length"] == 30


def test_overlay_partial_overlap_clamps_to_interval():
    """Overlay extends beyond the keep interval; should clamp to the interval edges."""
    fps = 30.0
    tl_map = _simple_tl_map(fps)  # 0-4s
    overlays = [{"start": 3.0, "end": 6.0, "text": "Edge"}]
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert len(captured) == 1
    kw = captured[0]
    # Clamped end = 4.0s → frame_start = 1 + 90, length = (4.0-3.0)*30 = 30
    assert kw["frame_start"] == 1 + 90
    assert kw["length"] == 30


def test_empty_overlay_text_is_skipped():
    fps = 30.0
    tl_map = _simple_tl_map(fps)
    overlays = [{"start": 1.0, "end": 3.0, "text": "   "}]  # whitespace-only
    seq, captured = _seq_with_capture()
    place_overlays(
        overlays,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        overlay_style={},
        channel=4,
    )
    assert captured == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/stage4/test_overlays.py -v`
Expected: FAIL — `ImportError: cannot import name 'place_overlays'`.

- [ ] **Step 3: Implement `place_overlays`**

In `src/nagare_clip/stage4/timeline.py`, after the existing `place_captions()` function, append:

```python
def place_overlays(
    overlays: list,
    tl_map: list,
    effective_fps: float,
    sequence_collection: object,
    *,
    overlay_style: dict | None = None,
    channel: int = 4,
) -> None:
    """Place TEXT strips for <overlay> markers on a dedicated channel.

    Overlays do NOT force-keep audio: if an overlay's source time falls
    entirely outside the timeline map (e.g., the wrapped audio was cut),
    it is silently skipped.  Partial overlaps are clamped to the matching
    keep interval.  Speed-factor scaling mirrors place_captions().
    """
    for ov in overlays:
        ov_src_start = float(ov["start"])
        ov_src_end = float(ov["end"])
        text = ov.get("text", "").strip()
        if not text:
            continue

        tl_start = None
        tl_end = None
        length = None
        for entry in tl_map:
            if ov_src_start < entry["src_end"] and ov_src_end > entry["src_start"]:
                speed = float(entry.get("speed_factor", 1.0))
                clamped_start = max(ov_src_start, entry["src_start"])
                clamped_end = min(ov_src_end, entry["src_end"])
                offset_start = sec_to_frames(
                    (clamped_start - entry["src_start"]) / speed, effective_fps
                )
                offset_end = sec_to_frames(
                    (clamped_end - entry["src_start"]) / speed, effective_fps
                )
                tl_start = entry["tl_start"] + offset_start
                tl_end = entry["tl_start"] + offset_end
                length = max(1, tl_end - tl_start)
                tl_end = tl_start + length
                break

        if tl_start is None or tl_end is None or length is None or tl_end <= tl_start:
            logging.warning(
                "Overlay skipped (no matching keep interval): %r", text[:60]
            )
            continue

        text_strip = sequence_collection.new_effect(
            name=f"ov_{ov_src_start:.3f}",
            type="TEXT",
            channel=channel,
            frame_start=tl_start,
            length=length,
        )
        style = overlay_style or {}
        text_strip.text = text
        text_strip.font_size = style.get("font_size", 50)
        text_strip.alignment_x = style.get("alignment_x", "CENTER")
        text_strip.anchor_y = style.get("anchor_y", "TOP")
        text_strip.location[0] = style.get("location_x", 0.5)
        text_strip.location[1] = style.get("location_y", 0.95)
        if "use_shadow" in style:
            text_strip.use_shadow = style["use_shadow"]
        if "wrap_width" in style:
            text_strip.wrap_width = style["wrap_width"]
        if "use_outline" in style:
            text_strip.use_outline = style["use_outline"]
        if "outline_color" in style:
            text_strip.outline_color = style["outline_color"]
        if "outline_width" in style:
            text_strip.outline_width = style["outline_width"]
        if "use_box" in style:
            text_strip.use_box = style["use_box"]
        if "box_color" in style:
            text_strip.box_color = style["box_color"]
        logging.debug(
            "Overlay '%s': timeline frames %d-%d", text[:40], tl_start, tl_end
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/stage4/test_overlays.py -v`
Expected: PASS — 6 tests green.

- [ ] **Step 5: Mutation check**

Three mutations, revert after each:

1. Change `if ov_src_start < entry["src_end"] and ov_src_end > entry["src_start"]:` → `if False:`. Re-run: `test_overlay_within_keep_interval_creates_text_strip` and `test_overlay_partial_overlap_clamps_to_interval` must fail. Revert.
2. Drop the `/ speed` divisions in the `offset_start` / `offset_end` calculations. Re-run: `test_overlay_inside_sped_up_interval_scales_offsets` must fail. Revert.
3. Remove the `if not text: continue` early return. Re-run: `test_empty_overlay_text_is_skipped` must fail. Revert.

After all reverts, run the full test file once more — all green.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/stage4/timeline.py tests/stage4/test_overlays.py
git commit -m "feat: add place_overlays for Blender VSE TEXT strips"
```

---

## Task 6 — Stage 5: Wire `place_overlays` into `blender_cli.py`

**Files:**
- Modify: `src/nagare_clip/stage4/blender_cli.py:23-27` (import `place_overlays`)
- Modify: `src/nagare_clip/stage4/blender_cli.py:127-158` (read overlays, call `place_overlays`)

This task has no automated tests (Blender CLI exercises live Blender); verify by inspection and the end-to-end smoke test in Verification.

- [ ] **Step 1: Add the import**

In `src/nagare_clip/stage4/blender_cli.py`, change:

```python
from nagare_clip.stage4.timeline import (
    build_timeline_map,
    place_captions,
    place_strips,
)
```

to:

```python
from nagare_clip.stage4.timeline import (
    build_timeline_map,
    place_captions,
    place_overlays,
    place_strips,
)
```

- [ ] **Step 2: Read overlays and call `place_overlays` after captions**

In `src/nagare_clip/stage4/blender_cli.py`, in the per-source loop, after the `captions = intervals_data.get("captions", [])` line at line 128, add:

```python
        overlays = intervals_data.get("overlays", [])
```

After the `place_captions(...)` block at lines 150-157, add:

```python
        if overlays:
            # Resolve overlay style: caption_style defaults overlaid with
            # overlay_style overrides (overlay_style wins per key).
            cap_style = cfg["blender"]["caption_style"]
            ov_style = {**cap_style, **cfg["blender"]["overlay_style"]}
            # Overlay channel sits above the caption channel (which is 3).
            place_overlays(
                overlays,
                tl_map,
                effective_fps,
                sequence_collection,
                overlay_style=ov_style,
                channel=4,
            )
```

- [ ] **Step 3: Smoke-test the import path with py_compile**

Run: `uv run python -m py_compile src/nagare_clip/stage4/blender_cli.py src/nagare_clip/stage4/timeline.py`
Expected: no output (success).

- [ ] **Step 4: Run the full unit/integration suite**

Run: `uv run pytest`
Expected: all tests pass (existing + the new overlay tests from Tasks 1-5).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/stage4/blender_cli.py
git commit -m "feat: invoke place_overlays per source in blender_cli"
```

---

## Task 7 — Documentation updates

**Files:**
- Modify: `CLAUDE.md` (Stage 3 description + Stage 4 runtime quirks)
- Modify: `README.md` (Human Editing Workflow section)
- Modify: `plan.md` (implementation status)

- [ ] **Step 1: Update `CLAUDE.md`**

In `/home/diginah/ghq/github.com/diginatu/nagare-clip/CLAUDE.md`, find the Stage 3 description that mentions `<speed factor="N.N">...</speed>` and append a new paragraph after it (analogous structure):

```
`<overlay text="...">...</overlay>` is a third companion marker that places
an on-screen TEXT strip in Stage 5 at the wrapped span's time range. Unlike
`<keep>` and `<speed>`, **it does not force-preserve audio** — if the wrapped
audio is cut by silence detection, the overlay is silently skipped (it cannot
display over content that does not exist on the timeline). Multi-line spans,
nesting/unmatched-tag warnings, and human-after-LLM authorship follow the
`<keep>` rules. Quotes inside the `text="..."` attribute value are not
supported (regex uses `[^"]*`).
```

Then in the Stage 4 runtime-quirks bullet list (the one that describes `extract_keep_ranges` / `extract_speed_ranges`), append a sibling bullet:

```
- Stage 4 additionally extracts `<overlay text="...">...</overlay>` ranges via
  `extract_overlay_ranges()` in `src/nagare_clip/stage3/sync_json.py`, returning
  `(start, end, text)` triples. Overlays do **not** participate in
  `subtract_intervals()` or speed annotation; they are written verbatim to a
  top-level `overlays` array in the intervals JSON. Stage 5 reads
  `overlays`, maps each through `build_timeline_map()` (speed-aware,
  same trick as captions), and calls `place_overlays()` to create a TEXT strip
  on channel 4 (one above the caption channel). Overlays that fall on cut
  content are silently skipped. Style is `caption_style` overlaid with
  `blender.overlay_style` overrides (defaults: `anchor_y: TOP`, `location_y: 0.95`).
```

- [ ] **Step 2: Update `README.md`**

In `/home/diginah/ghq/github.com/diginatu/nagare-clip/README.md`, find the Human Editing Workflow section that documents `<keep>` and `<speed>`. Append documentation for `<overlay>` (parallel structure to the `<speed>` paragraph). The exact wording depends on what's already there — match the existing tone — but cover:

- Syntax: `<overlay text="...">wrapped transcript words</overlay>`
- Purpose: places an on-screen TEXT strip in Blender at the wrapped time range
- Does NOT force-keep audio (unlike `<keep>` / `<speed>`)
- Multi-line spans supported; quotes inside `text="..."` are not
- Resume with `--from-stage 4` to apply

- [ ] **Step 3: Update `plan.md`**

In `/home/diginah/ghq/github.com/diginatu/nagare-clip/plan.md` (project-local plan, not the superpowers plan), find the section that lists implemented markers (`<keep>`, `<speed>`) and add a corresponding entry for `<overlay>` describing it as text-display-only with no audio side-effect.

- [ ] **Step 4: Final regression run**

Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md plan.md
git commit -m "docs: document <overlay text=...> marker"
```

---

## Verification

After all tasks complete:

1. **Unit tests:**
   Run: `uv run pytest tests/stage3/test_overlay_markers.py tests/stage4/test_overlays.py -v`
   Expected: 17 tests pass.

2. **Integration tests:**
   Run: `uv run pytest tests/test_cli_overlay_markers.py -v`
   Expected: 3 tests pass.

3. **Full suite regression:**
   Run: `uv run pytest`
   Expected: full suite green (pre-existing tests unaffected).

4. **End-to-end smoke test (requires Blender + a sample video):**
   - Take a sample `input/<sample>.mp4` that has already been processed through Stage 3 (so `output/stage3/<sample>_edits.txt` exists).
   - Hand-edit `output/stage3/<sample>_edits.txt` to add `<overlay text="Test Overlay">…</overlay>` around a few words that you know are inside a speech span (will be kept).
   - Also add a second overlay wrapping words inside a known silence span (will be cut), to verify the skip path.
   - Run: `./scripts/run_pipeline.sh --source input/<sample>.mp4 --from-stage 4`
   - Open `output/stage5/<sample>_edited.blend` in Blender.
   - Verify:
     - A TEXT strip with `Test Overlay` appears on channel 4 at the expected timeline frames.
     - The strip is anchored near the top of the frame (does not collide with bottom-anchored captions).
     - The second overlay (over the silence) is NOT present in the timeline; its absence is logged with `Overlay skipped (no matching keep interval)` in the log.
     - The wrapped audio for the first overlay was NOT specially force-kept (control: the timeline length matches the non-overlay run; only the TEXT strip is new).
