# Speed-up Mark Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-place an on-screen TEXT badge (e.g. `x2.0`) over every `<speed>` region in the Stage 5 Blender rough cut, derived from the existing `speed_ranges` data.

**Architecture:** A new pure-ish `place_speed_marks()` in `timeline.py` mirrors `place_overlays()` — it maps each speed range's source-time span through the speed-aware `tl_map` and creates a TEXT effect strip on channel 5, with text from a config template. `blender_cli.py` calls it once per source after `place_overlays()`, gated by a new `blender.speed_mark.enabled` config flag. No Stage 1–4 changes.

**Tech Stack:** Python, Blender `bpy` VSE API (mocked in tests via `MagicMock`), pytest, uv.

---

### Task 1: `place_speed_marks()` core placement + factor formatting

**Files:**
- Modify: `src/nagare_clip/stage4/timeline.py` (add function after `place_overlays`, ends at line 529)
- Test: `tests/stage4/test_speed_marks.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/stage4/test_speed_marks.py`:

```python
"""Tests for place_speed_marks() — auto TEXT badge for <speed> regions."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.timeline import build_timeline_map, place_speed_marks


def _seq_with_capture():
    """Return (sequence_collection_mock, captured_kwargs_list)."""
    captured: list = []
    seq = MagicMock()

    def capture_effect(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    seq.new_effect = capture_effect
    return seq, captured


class _AttrTracker:
    """Records attributes set via assignment; has a real list 'location'."""

    def __init__(self):
        object.__setattr__(self, "_assigned", {})
        object.__setattr__(self, "location", [0.0, 0.0])

    def __setattr__(self, name, value):
        self._assigned[name] = value

    def __getattr__(self, name):
        try:
            return self._assigned[name]
        except KeyError:
            raise AttributeError(name)


def test_speed_mark_creates_text_strip_on_channel_5():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert len(captured) == 1
    kw = captured[0]
    assert kw["type"] == "TEXT"
    assert kw["channel"] == 5


def test_speed_mark_factor_formatted_one_decimal():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert strip.text == "x2.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stage4/test_speed_marks.py -v`
Expected: FAIL with `ImportError: cannot import name 'place_speed_marks'`

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/stage4/timeline.py`, add this function immediately after `place_overlays()` (after current line 529). It mirrors the `place_overlays()` mapping loop:

```python
def place_speed_marks(
    speed_ranges: list,
    tl_map: list,
    effective_fps: float,
    sequence_collection: object,
    *,
    template: str = "x{factor}",
    mark_style: dict | None = None,
    channel: int = 5,
) -> None:
    """Place a TEXT badge (e.g. ``x2.0``) over each ``<speed>`` region.

    Auto-derived from ``speed_ranges`` (``{start, end, factor}``).  The span is
    mapped through the speed-aware ``tl_map`` exactly like ``place_overlays()``
    (offsets divided by each interval's ``speed_factor``; min ``tl_start`` /
    max ``tl_end`` accumulated so a span crossing several keep intervals renders
    as one contiguous strip).  Badge text is ``template.format(factor=...)``
    with the factor rendered to one decimal.
    """
    for sr in speed_ranges:
        sr_src_start = float(sr["start"])
        sr_src_end = float(sr["end"])
        factor = float(sr["factor"])
        text = template.format(factor=f"{factor:.1f}").strip()
        if not text:
            continue

        tl_start = None
        tl_end = None
        for entry in tl_map:
            if sr_src_start < entry["src_end"] and sr_src_end > entry["src_start"]:
                speed = float(entry.get("speed_factor", 1.0))
                clamped_start = max(sr_src_start, entry["src_start"])
                clamped_end = min(sr_src_end, entry["src_end"])
                offset_start = sec_to_frames(
                    (clamped_start - entry["src_start"]) / speed, effective_fps
                )
                offset_end = sec_to_frames(
                    (clamped_end - entry["src_start"]) / speed, effective_fps
                )
                entry_tl_start = entry["tl_start"] + offset_start
                entry_tl_end = entry["tl_start"] + offset_end
                tl_start = (
                    entry_tl_start if tl_start is None else min(tl_start, entry_tl_start)
                )
                tl_end = entry_tl_end if tl_end is None else max(tl_end, entry_tl_end)

        if tl_start is None or tl_end is None:
            logging.warning(
                "Speed mark skipped (no matching keep interval): %r", text[:60]
            )
            continue
        length = max(1, tl_end - tl_start)

        text_strip = sequence_collection.new_effect(
            name=f"spd_{sr_src_start:.3f}",
            type="TEXT",
            channel=channel,
            frame_start=tl_start,
            length=length,
        )
        style = mark_style or {}
        text_strip.text = text
        text_strip.font_size = style.get("font_size", 35)
        text_strip.alignment_x = style.get("alignment_x", "RIGHT")
        text_strip.anchor_y = style.get("anchor_y", "TOP")
        text_strip.location[0] = style.get("location_x", 0.95)
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
            "Speed mark '%s': timeline frames %d-%d", text, tl_start, tl_start + length
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stage4/test_speed_marks.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Mutation-catch verification (repo TDD rule)**

For each behavior, break the impl, confirm the test fails, then revert:
- Change `channel=channel` arg in `new_effect(...)` to `channel=4` → run `test_speed_mark_creates_text_strip_on_channel_5` → expect FAIL → revert.
- Change `f"{factor:.1f}"` to `f"{factor:.0f}"` → run `test_speed_mark_factor_formatted_one_decimal` → expect FAIL (`x2` != `x2.0`) → revert.

Report the fail-then-pass evidence.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/stage4/timeline.py tests/stage4/test_speed_marks.py
git commit -m "feat: add place_speed_marks() for <speed> badges"
```

---

### Task 2: Template substitution, timeline offset, multi-interval span, skip cases

**Files:**
- Test: `tests/stage4/test_speed_marks.py` (append)
- Modify: `src/nagare_clip/stage4/timeline.py` (only if a test reveals a gap — implementation from Task 1 should already cover these)

- [ ] **Step 1: Append the failing tests**

Add to `tests/stage4/test_speed_marks.py`:

```python
def test_speed_mark_custom_template():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 1.5}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 1.5}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="⏩{factor}x",
        mark_style={},
        channel=5,
    )
    assert strip.text == "⏩1.5x"


def test_speed_mark_scales_offsets_inside_sped_up_interval():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    # Mark covers 1.0-3.0s of source; speed 2.0 halves offsets/length.
    speed_ranges = [{"start": 1.0, "end": 3.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert len(captured) == 1
    kw = captured[0]
    # 1.0s/2.0 * 30 = 15 frames offset from tl_start=1
    assert kw["frame_start"] == 1 + 15
    # (3.0-1.0)/2.0 * 30 = 30 frames
    assert kw["length"] == 30


def test_speed_mark_spanning_multiple_keep_intervals():
    fps = 30.0
    tl_map = build_timeline_map(
        [
            {"start": 0.0, "end": 2.0, "speed_factor": 2.0},
            {"start": 4.0, "end": 6.0, "speed_factor": 2.0},
        ],
        effective_fps=fps,
        source_fps=fps,
    )
    # Each interval is 2.0s/2.0 = 1.0s = 30 frames on timeline.
    # interval 1: tl 1-31; interval 2: tl 31-61 (contiguous).
    speed_ranges = [{"start": 0.0, "end": 6.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert len(captured) == 1
    kw = captured[0]
    assert kw["frame_start"] == 1
    assert kw["length"] == 60


def test_speed_mark_outside_any_keep_interval_is_skipped():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0}], effective_fps=fps, source_fps=fps
    )
    speed_ranges = [{"start": 10.0, "end": 11.0, "factor": 2.0}]
    seq, captured = _seq_with_capture()
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={},
        channel=5,
    )
    assert captured == []


def test_speed_mark_style_overrides_applied():
    fps = 30.0
    tl_map = build_timeline_map(
        [{"start": 0.0, "end": 4.0, "speed_factor": 2.0}],
        effective_fps=fps,
        source_fps=fps,
    )
    speed_ranges = [{"start": 0.0, "end": 4.0, "factor": 2.0}]
    seq = MagicMock()
    strip = _AttrTracker()
    seq.new_effect = MagicMock(return_value=strip)
    place_speed_marks(
        speed_ranges,
        tl_map,
        effective_fps=fps,
        sequence_collection=seq,
        template="x{factor}",
        mark_style={"font_size": 40, "alignment_x": "RIGHT", "location_x": 0.9},
        channel=5,
    )
    assert strip.font_size == 40
    assert strip.alignment_x == "RIGHT"
    assert strip.location[0] == 0.9
```

- [ ] **Step 2: Run tests to verify they pass (Task 1 impl already covers them)**

Run: `uv run pytest tests/stage4/test_speed_marks.py -v`
Expected: PASS (all tests). If any fail, the Task 1 implementation has a gap — fix `place_speed_marks()` to satisfy the test, then re-run.

- [ ] **Step 3: Mutation-catch verification**

Break, confirm fail, revert for each:
- Remove the `template.format(...)` call (hardcode `text = "x"`) → `test_speed_mark_custom_template` FAILs → revert.
- Change `/ speed` to `* 1` in `offset_start`/`offset_end` → `test_speed_mark_scales_offsets_inside_sped_up_interval` FAILs → revert.
- Change `min(tl_start, ...)`/`max(tl_end, ...)` accumulation to overwrite (`tl_start = entry_tl_start`) → `test_speed_mark_spanning_multiple_keep_intervals` FAILs → revert.
- Change the `tl_start is None` skip guard to `pass` (force a strip) → `test_speed_mark_outside_any_keep_interval_is_skipped` FAILs → revert.

Report evidence.

- [ ] **Step 4: Commit**

```bash
git add tests/stage4/test_speed_marks.py src/nagare_clip/stage4/timeline.py
git commit -m "test: cover speed-mark template, scaling, span, skip cases"
```

---

### Task 3: Config defaults for `blender.speed_mark`

**Files:**
- Modify: `src/nagare_clip/config.py:112-115` (insert `speed_mark` after `overlay_style`)
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (uses the existing `get_effective_config` import pattern in that file — check the file's existing imports and reuse them; the function is `get_effective_config(config_path, cli_overrides)`):

```python
def test_speed_mark_defaults_present():
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    sm = cfg["blender"]["speed_mark"]
    assert sm["enabled"] is True
    assert sm["template"] == "x{factor}"
    assert sm["font_size"] == 35
    assert sm["alignment_x"] == "RIGHT"
    assert sm["anchor_y"] == "TOP"
    assert sm["location_x"] == 0.95
    assert sm["location_y"] == 0.95
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_speed_mark_defaults_present -v`
Expected: FAIL with `KeyError: 'speed_mark'`

- [ ] **Step 3: Add the config default**

In `src/nagare_clip/config.py`, inside `DEFAULTS["blender"]`, add after the `overlay_style` block (current line 115, before the closing `}` of `"blender"`):

```python
        "speed_mark": {
            "enabled": True,
            "template": "x{factor}",
            "font_size": 35,
            "alignment_x": "RIGHT",
            "anchor_y": "TOP",
            "location_x": 0.95,
            "location_y": 0.95,
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_speed_mark_defaults_present -v`
Expected: PASS

- [ ] **Step 5: Mutation-catch verification**

Change `"enabled": True` to `False` → test FAILs → revert. Change `"template"` value → test FAILs → revert. Report evidence.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/config.py tests/test_config.py
git commit -m "feat: add blender.speed_mark config defaults"
```

---

### Task 4: Wire `place_speed_marks` into `blender_cli.py`

**Files:**
- Modify: `src/nagare_clip/stage4/blender_cli.py:25-28` (import) and `:168-181` (call site)
- Test: `tests/stage4/test_blender_cli_speed_mark.py` (create) — guards the enabled-gate and style resolution without launching Blender

- [ ] **Step 1: Write the failing test**

The `build_blend` flow needs `bpy`; to keep the test light, test the style-resolution + gate logic by calling a small helper. Add the helper in `blender_cli.py` and test it.

Create `tests/stage4/test_blender_cli_speed_mark.py`:

```python
"""Speed-mark wiring: style resolution + enabled gate in blender_cli."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("bpy", MagicMock())

from nagare_clip.stage4.blender_cli import resolve_speed_mark_style


def test_resolve_speed_mark_style_merges_caption_then_overrides():
    caption_style = {"font_size": 50, "alignment_x": "CENTER", "location_y": 0.05}
    speed_mark_cfg = {
        "enabled": True,
        "template": "x{factor}",
        "font_size": 35,
        "alignment_x": "RIGHT",
        "location_y": 0.95,
    }
    style = resolve_speed_mark_style(caption_style, speed_mark_cfg)
    # caption defaults present unless overridden
    assert style["font_size"] == 35       # override wins
    assert style["alignment_x"] == "RIGHT"  # override wins
    assert style["location_y"] == 0.95    # override wins
    # non-style keys must NOT leak into the strip style
    assert "enabled" not in style
    assert "template" not in style
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stage4/test_blender_cli_speed_mark.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_speed_mark_style'`

- [ ] **Step 3: Add the helper and wire the call site**

In `src/nagare_clip/stage4/blender_cli.py`, add the import alongside the others (current line 25-28 import `place_captions, place_overlays, ..., split_intervals_by_speed`):

```python
    place_overlays,
    place_speed_marks,
```

Add this module-level helper (e.g. just above `build_blend` / near the other top-level defs):

```python
def resolve_speed_mark_style(caption_style: dict, speed_mark_cfg: dict) -> dict:
    """caption_style defaults overlaid with speed_mark style overrides.

    Non-style config keys (``enabled``, ``template``) are excluded so they do
    not leak onto the TEXT strip.
    """
    overrides = {
        k: v for k, v in speed_mark_cfg.items() if k not in ("enabled", "template")
    }
    return {**caption_style, **overrides}
```

Then wire the call site after the `if overlays:` block (current lines 168-181), inside the per-source loop:

```python
        speed_mark_cfg = cfg["blender"]["speed_mark"]
        if speed_mark_cfg["enabled"] and speed_ranges:
            place_speed_marks(
                speed_ranges,
                tl_map,
                effective_fps,
                sequence_collection,
                template=speed_mark_cfg["template"],
                mark_style=resolve_speed_mark_style(
                    cfg["blender"]["caption_style"], speed_mark_cfg
                ),
                channel=5,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stage4/test_blender_cli_speed_mark.py -v`
Expected: PASS

- [ ] **Step 5: Mutation-catch verification**

In `resolve_speed_mark_style`, drop the `enabled`/`template` exclusion (return `{**caption_style, **speed_mark_cfg}`) → test FAILs on `"enabled" not in style` → revert. Report evidence.

- [ ] **Step 6: Run the full Stage 5 test suite**

Run: `uv run pytest tests/stage4/ -v`
Expected: PASS (no regressions in overlays/captions/retiming).

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/stage4/blender_cli.py tests/stage4/test_blender_cli_speed_mark.py
git commit -m "feat: wire place_speed_marks into Stage 5 (enabled by default)"
```

---

### Task 5: Documentation

**Files:**
- Modify: `config.example.yml` (document `speed_mark` keys under `blender:`)
- Modify: `README.md` (user-facing note)
- Modify: `plan.md` (status)
- Modify: `CLAUDE.md` (Stage 5 runtime-quirk entry)

- [ ] **Step 1: Document config keys**

In `config.example.yml`, under the `blender:` section near `overlay_style`, add:

```yaml
    # Speed-up mark: auto on-screen badge over every <speed> region.
    speed_mark:
      enabled: true            # set false to disable all speed badges
      template: "x{factor}"    # {factor} is the speed factor (one decimal, e.g. 2.0)
      font_size: 35
      alignment_x: RIGHT
      anchor_y: TOP
      location_x: 0.95
      location_y: 0.95
```

(Match the existing indentation style of `config.example.yml` — verify by reading the `caption_style`/`overlay_style` entries there first.)

- [ ] **Step 2: Update README.md**

Add a short note in the section describing `<speed>` / Stage 5 behavior: every `<speed factor="N.N">` region automatically shows a small top-right `x2.0`-style badge in the rendered cut; disable via `blender.speed_mark.enabled: false` or restyle via `blender.speed_mark.*` / change wording with `blender.speed_mark.template`.

- [ ] **Step 3: Update plan.md**

Add a status entry noting the speed-up mark feature is implemented (Stage 5 `place_speed_marks`, config `blender.speed_mark`, enabled by default).

- [ ] **Step 4: Update CLAUDE.md**

Add a Stage 5 runtime-quirk bullet: "Stage 5 reads the existing `speed_ranges` array and, when `blender.speed_mark.enabled` (default true), calls `place_speed_marks()` (`src/nagare_clip/stage4/timeline.py`) once per source after `place_overlays()`. It creates a TEXT badge on **channel 5** (above overlay ch4) for each speed range, text = `template.format(factor=f"{factor:.1f}")` (default template `"x{factor}"` → `x2.0`), styled as `caption_style` overlaid with `blender.speed_mark` overrides (default small top-right badge). Timeline mapping is speed-aware and multi-interval-contiguous, identical to `place_overlays()`. No Stage 1–4 changes — purely a Stage 5 consumer of `speed_ranges`."

- [ ] **Step 5: Commit**

```bash
git add config.example.yml README.md plan.md CLAUDE.md
git commit -m "docs: document blender.speed_mark speed-up badge"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 2: Byte-compile the touched modules (mirrors the validation hook)**

Run: `uv run python -m py_compile src/nagare_clip/stage4/timeline.py src/nagare_clip/stage4/blender_cli.py src/nagare_clip/config.py`
Expected: no output (success).

- [ ] **Step 3: Confirm and report**

Report green test output + mutation-catch evidence gathered in Tasks 1–4. Do not claim completion without showing the passing run.
