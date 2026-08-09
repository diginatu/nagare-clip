# Publish Thumbnail Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `publish` stage renders one thumbnail image per copy set with ImageMagick and embeds every image in `publish.md`, so a human compares finished thumbnails instead of reading file paths.

**Architecture:** A new pure module `publish/thumbnail.py` owns text escaping, style validation, layout and ImageMagick **argument-list** construction. The publish LLM authors the style *values* (magick's own keys: `-fill`, `-stroke`, `-strokewidth`, `-pointsize`, `-gravity`) alongside each copy set; our code validates them and builds every command. `run_publish` gains an injected `render` callable so the stage stays subprocess-free, and the pipeline adapter supplies one backed by `external.run_magick`.

**Tech Stack:** Python 3, pydantic-settings config models, pytest, ImageMagick 7 (`magick`) on the host.

## Global Constraints

- **Never invoke ImageMagick through a shell.** Every command is a `list[str]` passed to `subprocess.run` without `shell=True`. The copy is LLM-written and may contain quotes, `$`, backslashes.
- **Escape every piece of text that reaches ImageMagick**, in this order: `\` → `\\`, then `%` → `%%`, then a leading `@` → `\@`. Verified against ImageMagick 7.1.2: `%w` expands to the image width, `\n` becomes a real newline, and `label:@file` reads that file's contents into the image.
- **The model emits values, never operators.** Only the keys in the table below exist; an unknown key is dropped and logged.
- **Every failure degrades, never aborts.** A missing `magick`, a non-zero exit, or no background still → warn, no renders, `publish.json` still written.
- **All Python runs through `uv run`.** e.g. `uv run pytest tests/publish/test_thumbnail.py -v`.
- **Validation command:** `make check` (ruff lint + format-check + validate + pytest).
- Per the user's global instructions: for any test written against code that already exists, briefly mutate the implementation, confirm the test fails, revert, and report that evidence alongside the green run.

### Allowed style keys

| key | flag | scope | accepted |
|---|---|---|---|
| `font` | `-font` | line | a key of `publish.thumbnail.fonts` |
| `pointsize` | `-pointsize` | line | int 8–400 |
| `fill` | `-fill` | line | `#RGB`/`#RRGGBB`/`#RRGGBBAA`/`rgb(…)`/`rgba(…)`/named-colour allowlist |
| `stroke` | `-stroke` | line | as `fill` |
| `strokewidth` | `-strokewidth` | line | int 0–40 |
| `gravity` | `-gravity` | set | the nine gravity names |
| `offset` | `-annotate +x+y` | set | `[+-]N[+-]N` |
| `shadow` | `-blur` + colour | set | `{"color": <colour>, "blur": "RxS"}` |

### Deviations from the spec (deliberate)

- The whole-set style fallback is **round-robin over the presets by set index**, not random. Same outcome — four sets that look different — but reproducible across runs, which a pipeline needs.
- Config gains `line_gap` (px between stacked lines); the spec's layout section needs it and it is not something a model should decide.
- `render_sets()` lives in `thumbnail.py` with an **injected runner** rather than in `stages.py`, because the measure call needs captured stdout and both the pipeline adapter and the CLI need the same orchestration. `external.run_magick` is the only place a subprocess is actually started.

---

## File Structure

- **Create** `src/nagare_clip/publish/thumbnail.py` — escaping, style types + validation, presets, layout, argv builders, `render_sets()`, `resolve_background()`, `main()` CLI.
- **Create** `tests/publish/test_thumbnail.py` — everything pure in the module.
- **Create** `tests/publish/test_thumbnail_cli.py` — the standalone re-render CLI.
- **Modify** `src/nagare_clip/config.py` — `ThumbnailConfig` model, `PublishConfig.thumbnail`, `PUBLISH_PROMPT` style shape.
- **Modify** `src/nagare_clip/publish/publish_llm.py` — `ThumbLine.style`, new `ThumbSet`, style pass-through, `font_slot_note()`, `thumbnail_copy_to_dict()`.
- **Modify** `src/nagare_clip/publish/run.py` — `render` parameter, `renders` in the artifact, markdown embedding.
- **Modify** `src/nagare_clip/pipeline/external.py` — `run_magick()`.
- **Modify** `src/nagare_clip/pipeline/stages.py` — `_render_thumbnails()` closure.
- **Modify** `config.example.yml` (generated), `README.md`, `AGENTS.md`, `docs/stages/publish.md`.
- **Modify** `tests/publish/test_publish_llm.py`, `tests/publish/test_run.py`, `tests/publish/test_stage_wiring.py` for the new shapes.

---

### Task 1: Text escaping

**Files:**
- Create: `src/nagare_clip/publish/thumbnail.py`
- Test: `tests/publish/test_thumbnail.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `escape_magick_text(text: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
"""publish thumbnail rendering: escaping, style validation, layout, argv."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from nagare_clip.publish.thumbnail import escape_magick_text

HAS_MAGICK = shutil.which("magick") is not None


def test_percent_is_doubled_so_imagemagick_does_not_expand_it():
    # `%w` is an ImageMagick escape for the image width, not literal text.
    assert escape_magick_text("100% done") == "100%% done"
    assert escape_magick_text("%w") == "%%w"


def test_backslash_is_doubled_so_it_does_not_become_a_newline():
    assert escape_magick_text(r"a\nb") == r"a\\nb"


def test_a_leading_at_sign_is_neutralised_so_no_file_is_read():
    # `label:@path` makes ImageMagick render that FILE's contents.
    assert escape_magick_text("@/etc/passwd") == r"\@/etc/passwd"


def test_a_non_leading_at_sign_is_left_alone():
    assert escape_magick_text("a@b") == "a@b"


def test_the_at_escape_is_added_after_backslash_doubling():
    """Order matters: doubling backslashes last would break the @ escape."""
    assert escape_magick_text("@a") == r"\@a"


def test_plain_text_is_unchanged():
    assert escape_magick_text("穴あけ不要。") == "穴あけ不要。"


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
@pytest.mark.parametrize("text", ["100% done", "%w", r"a\nb", "@nonexistent-file.txt"])
def test_escaped_text_renders_as_one_literal_line(tmp_path, text):
    """The escapes are pinned to real ImageMagick behaviour, not to a guess."""

    def height(arg: str) -> int:
        out = subprocess.run(
            ["magick", "-background", "none", "-pointsize", "40", f"label:{arg}",
             "-format", "%h", "info:"],
            check=True, capture_output=True, text=True,
        ).stdout
        return int(out)

    # A single line of 40pt text; an unescaped \n or @file would make it taller.
    assert height(escape_magick_text(text)) == height("x")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.publish.thumbnail'`

- [ ] **Step 3: Write the implementation**

Create `src/nagare_clip/publish/thumbnail.py`:

```python
"""Render the thumbnail candidates, rather than describing them.

The publish LLM writes the copy AND the style for that copy, in ImageMagick's
own vocabulary (``-fill``, ``-stroke``, ``-strokewidth``, ``-pointsize``,
``-gravity``), so the four sets in ``publish.md`` are four real options rather
than four wordings of one look.

It emits *values*; this module builds every command.  The publish prompt is fed
the summaries, the plan directions and the director's captions -- all derived
from the video's transcript -- so anything said on camera reaches the model.
``magick`` reads and writes files (``@``, ``-write``, MSL), which makes a
model-authored command line a real hole and an allowlisted operator set a cheap
fix: the worst a bad generation can do is produce an ugly image.

Commands are argument lists.  Never a shell string.
"""

from __future__ import annotations


def escape_magick_text(text: str) -> str:
    """Text that ImageMagick will render verbatim.

    Three hazards, all verified against ImageMagick 7.1.2:

    - ``%w`` expands to the image width (``%%`` renders a literal ``%``);
    - a backslash is an escape -- ``a\\nb`` becomes two lines;
    - a **leading** ``@`` makes ImageMagick read a FILE and render its
      contents (``label:@secret.txt`` leaked the file into the image).

    Order matters: the backslash of the ``@`` escape is added last so it is
    not doubled by the first rule.
    """
    out = text.replace("\\", "\\\\").replace("%", "%%")
    return "\\" + out if out.startswith("@") else out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: PASS (the `magick` test runs if ImageMagick is installed, otherwise SKIPPED — say which in the report)

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py tests/publish/test_thumbnail.py
git commit -m "feat(publish): escape LLM copy for ImageMagick"
```

---

### Task 2: Style types, presets and validation

**Files:**
- Modify: `src/nagare_clip/publish/thumbnail.py`
- Test: `tests/publish/test_thumbnail.py`

**Interfaces:**
- Consumes: `escape_magick_text` (Task 1).
- Produces:
  - `LineStyle(font: str, pointsize: int, fill: str, stroke: str, strokewidth: int)` — frozen dataclass.
  - `SetStyle(gravity: str, offset: str, shadow: bool, shadow_color: str, shadow_blur: str)` — frozen dataclass.
  - `Preset(set_style: SetStyle, lines: dict[str, LineStyle])` — frozen dataclass; `lines` keyed by role (`tag`/`hook`/`subtitle`).
  - `PRESETS: tuple[Preset, ...]`
  - `preset_for(index: int) -> Preset` — round-robin, 1-based.
  - `resolve_line_style(raw: Mapping[str, Any], role: str, fonts: Mapping[str, str], preset: Preset) -> LineStyle`
  - `resolve_set_style(raw: Mapping[str, Any], preset: Preset) -> SetStyle`

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_thumbnail.py`:

```python
from nagare_clip.publish.thumbnail import (
    PRESETS,
    LineStyle,
    SetStyle,
    preset_for,
    resolve_line_style,
    resolve_set_style,
)

FONTS = {"sans-bold": "Noto-Sans-CJK-JP-Bold", "serif-black": "Noto-Serif-CJK-JP-Black"}


def _line(**raw):
    return resolve_line_style(raw, "hook", FONTS, PRESETS[0])


def test_a_font_slot_resolves_to_the_configured_face():
    assert _line(font="serif-black").font == "Noto-Serif-CJK-JP-Black"


def test_an_unknown_font_slot_falls_back_to_the_preset():
    """The model cannot know what is installed, so only slots are accepted."""
    assert _line(font="Comic Sans").font == PRESETS[0].lines["hook"].font


def test_a_font_path_is_not_accepted_as_a_slot():
    assert _line(font="/usr/share/fonts/evil.ttf").font == PRESETS[0].lines["hook"].font


@pytest.mark.parametrize("value", ["#fff", "#B08D3E", "#FAFAFAFF", "rgba(30,30,30,1)",
                                   "rgb(10, 20, 30)", "white"])
def test_accepted_colour_syntaxes(value):
    assert _line(fill=value).fill == value


@pytest.mark.parametrize("value", ["red; -write /tmp/x", "url(http://x)", "", 5, None,
                                   "#12345", "rgba(1,2,3,4) -write out.png"])
def test_a_rejected_colour_falls_back_to_the_preset(value):
    assert _line(fill=value).fill == PRESETS[0].lines["hook"].fill


@pytest.mark.parametrize("value,expected", [(8, 8), (400, 400), (156, 156)])
def test_pointsize_within_range_is_kept(value, expected):
    assert _line(pointsize=value).pointsize == expected


@pytest.mark.parametrize("value", [7, 401, 0, -20, "156", True, None])
def test_a_pointsize_out_of_range_or_wrong_type_falls_back(value):
    assert _line(pointsize=value).pointsize == PRESETS[0].lines["hook"].pointsize


@pytest.mark.parametrize("value", [-1, 41, "8", True])
def test_a_bad_strokewidth_falls_back(value):
    assert _line(strokewidth=value).strokewidth == PRESETS[0].lines["hook"].strokewidth


def test_zero_strokewidth_is_a_real_choice():
    assert _line(strokewidth=0).strokewidth == 0


def test_one_bad_key_does_not_lose_the_others():
    style = _line(fill="#B08D3E", stroke="not a colour", pointsize=120)
    assert style.fill == "#B08D3E"
    assert style.pointsize == 120
    assert style.stroke == PRESETS[0].lines["hook"].stroke


def test_an_unknown_key_is_ignored(caplog):
    style = _line(**{"-write": "/tmp/pwned", "fill": "white"})
    assert style.fill == "white"
    assert "-write" in caplog.text


def test_the_preset_is_chosen_by_role():
    tag = resolve_line_style({}, "tag", FONTS, PRESETS[0])
    hook = resolve_line_style({}, "hook", FONTS, PRESETS[0])
    assert tag.pointsize < hook.pointsize


def test_an_unknown_role_falls_back_to_the_hook_style():
    assert resolve_line_style({}, "banner", FONTS, PRESETS[0]) == PRESETS[0].lines["hook"]


@pytest.mark.parametrize("value", ["northwest", "center", "southeast", "north"])
def test_accepted_gravities(value):
    assert resolve_set_style({"gravity": value}, PRESETS[0]).gravity == value


@pytest.mark.parametrize("value", ["NorthWest", "middle", "", 3])
def test_a_bad_gravity_falls_back(value):
    assert resolve_set_style({"gravity": value}, PRESETS[0]).gravity == PRESETS[0].set_style.gravity


@pytest.mark.parametrize("value", ["+56+62", "-10+0", "+0-120"])
def test_accepted_offsets(value):
    assert resolve_set_style({"offset": value}, PRESETS[0]).offset == value


@pytest.mark.parametrize("value", ["56,62", "+56", "+56+62 -write x", "+99999+0"])
def test_a_bad_offset_falls_back(value):
    assert resolve_set_style({"offset": value}, PRESETS[0]).offset == PRESETS[0].set_style.offset


def test_shadow_colour_and_blur_are_validated():
    style = resolve_set_style(
        {"shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"}}, PRESETS[0]
    )
    assert style.shadow is True
    assert (style.shadow_color, style.shadow_blur) == ("rgba(0,0,0,0.8)", "0x8")


def test_shadow_false_turns_it_off():
    assert resolve_set_style({"shadow": False}, PRESETS[0]).shadow is False


def test_a_bad_blur_falls_back_but_keeps_the_shadow():
    style = resolve_set_style({"shadow": {"blur": "8"}}, PRESETS[0])
    assert style.shadow is True
    assert style.shadow_blur == PRESETS[0].set_style.shadow_blur


def test_presets_differ_from_each_other():
    """A set with no usable style still has to look unlike its neighbours."""
    assert len(PRESETS) >= 4
    looks = {(p.set_style.gravity, p.set_style.offset, p.lines["hook"].fill) for p in PRESETS}
    assert len(looks) == len(PRESETS)


def test_preset_for_is_round_robin_and_one_based():
    assert preset_for(1) is PRESETS[0]
    assert preset_for(len(PRESETS) + 1) is PRESETS[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: FAIL — `ImportError: cannot import name 'PRESETS'`

- [ ] **Step 3: Write the implementation**

Append to `src/nagare_clip/publish/thumbnail.py` (extend the imports at the top of the file):

```python
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

GRAVITIES = (
    "northwest", "north", "northeast",
    "west", "center", "east",
    "southwest", "south", "southeast",
)
NAMED_COLORS = frozenset(
    {"white", "black", "gray", "grey", "red", "green", "blue", "yellow",
     "cyan", "magenta", "orange", "none", "transparent"}
)
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_RE = re.compile(r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*[\d.]+\s*)?\)$")
_OFFSET_RE = re.compile(r"^[+-]\d{1,4}[+-]\d{1,4}$")
_BLUR_RE = re.compile(r"^\d{1,3}(?:\.\d+)?x\d{1,3}(?:\.\d+)?$")

MIN_POINTSIZE, MAX_POINTSIZE = 8, 400
MAX_STROKEWIDTH = 40
LINE_KEYS = ("font", "pointsize", "fill", "stroke", "strokewidth")
SET_KEYS = ("gravity", "offset", "shadow")


@dataclass(frozen=True)
class LineStyle:
    font: str = ""  # resolved face name; "" -> no -font flag, ImageMagick's default
    pointsize: int = 72
    fill: str = "white"
    stroke: str = "rgba(30,30,30,1)"
    strokewidth: int = 8


@dataclass(frozen=True)
class SetStyle:
    gravity: str = "northwest"
    offset: str = "+56+62"
    shadow: bool = True
    shadow_color: str = "rgba(0,0,0,0.8)"
    shadow_blur: str = "0x8"


@dataclass(frozen=True)
class Preset:
    set_style: SetStyle
    lines: Mapping[str, LineStyle]  # by role: tag / hook / subtitle


def _preset(gravity: str, offset: str, tag_fill: str, hook_fill: str, sub_fill: str,
            outline: str) -> Preset:
    return Preset(
        set_style=SetStyle(gravity=gravity, offset=offset),
        lines={
            "tag": LineStyle(pointsize=70, fill=tag_fill, stroke=outline, strokewidth=8),
            "hook": LineStyle(pointsize=156, fill=hook_fill, stroke=outline, strokewidth=12),
            "subtitle": LineStyle(pointsize=98, fill=sub_fill, stroke=outline, strokewidth=10),
        },
    )


# Fallbacks only. A set whose style is unusable still has to look unlike its
# neighbours, or `publish.md` shows four copies of one thumbnail.
PRESETS: tuple[Preset, ...] = (
    _preset("northwest", "+56+62", "white", "#B08D3E", "white", "rgba(30,30,30,1)"),
    _preset("southwest", "+56+62", "#FFD54F", "white", "#FFD54F", "rgba(20,20,20,1)"),
    _preset("northeast", "+56+62", "white", "#4FC3F7", "white", "rgba(10,20,40,1)"),
    _preset("center", "+0+0", "#FFAB91", "white", "#FFAB91", "rgba(40,10,10,1)"),
)


def preset_for(index: int) -> Preset:
    """The fallback look for the *index*-th set (1-based), round-robin.

    Round-robin rather than random: four sets still look different, and a
    rerun of the same pipeline produces the same images.
    """
    return PRESETS[(max(index, 1) - 1) % len(PRESETS)]


def _color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.lower() in NAMED_COLORS or _HEX_RE.match(v) or _RGB_RE.match(v):
        return v
    return None


def _int_in(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _warn_unknown(raw: Mapping[str, Any], known: tuple[str, ...], skip: tuple[str, ...] = ()) -> None:
    for key in raw:
        if key not in known and key not in skip:
            logger.warning("publish: thumbnail style key %r is not supported; ignored", key)


def resolve_line_style(
    raw: Mapping[str, Any], role: str, fonts: Mapping[str, str], preset: Preset
) -> LineStyle:
    """One line's validated style; every rejected key falls back on its own.

    Per-key rather than per-set, so one malformed colour cannot cost the set
    the rest of the style the model chose for it.
    """
    _warn_unknown(raw, LINE_KEYS, skip=("role", "text"))
    base = preset.lines.get(role, preset.lines["hook"])
    slot = raw.get("font")
    font = fonts.get(slot, base.font) if isinstance(slot, str) else base.font
    return LineStyle(
        font=font,
        pointsize=_int_in(raw.get("pointsize"), MIN_POINTSIZE, MAX_POINTSIZE) or base.pointsize,
        fill=_color(raw.get("fill")) or base.fill,
        stroke=_color(raw.get("stroke")) or base.stroke,
        strokewidth=(
            sw if (sw := _int_in(raw.get("strokewidth"), 0, MAX_STROKEWIDTH)) is not None
            else base.strokewidth
        ),
    )


def resolve_set_style(raw: Mapping[str, Any], preset: Preset) -> SetStyle:
    """The block anchor and shadow for one set."""
    _warn_unknown(raw, SET_KEYS, skip=("lines",))
    base = preset.set_style
    gravity = raw.get("gravity")
    offset = raw.get("offset")
    shadow_raw = raw.get("shadow", True)
    shadow = shadow_raw is not False
    shadow_map = shadow_raw if isinstance(shadow_raw, Mapping) else {}
    return SetStyle(
        gravity=gravity if gravity in GRAVITIES else base.gravity,
        offset=(
            offset if isinstance(offset, str) and _OFFSET_RE.match(offset.strip())
            else base.offset
        ),
        shadow=shadow,
        shadow_color=_color(shadow_map.get("color")) or base.shadow_color,
        shadow_blur=(
            blur.strip() if isinstance(blur := shadow_map.get("blur"), str)
            and _BLUR_RE.match(blur.strip()) else base.shadow_blur
        ),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py tests/publish/test_thumbnail.py
git commit -m "feat(publish): validate model-authored thumbnail style values"
```

---

### Task 3: Measuring rendered text

**Files:**
- Modify: `src/nagare_clip/publish/thumbnail.py`
- Test: `tests/publish/test_thumbnail.py`

**Interfaces:**
- Consumes: `LineStyle`, `escape_magick_text`.
- Produces:
  - `build_measure_cmd(lines: Sequence[tuple[str, LineStyle]]) -> list[str]`
  - `parse_metrics(stdout: str, expected: int) -> list[tuple[int, int]] | None` — `None` when the output does not describe exactly *expected* lines.

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_thumbnail.py`:

```python
from nagare_clip.publish.thumbnail import build_measure_cmd, parse_metrics


def test_measure_puts_every_line_in_one_call():
    cmd = build_measure_cmd([("A", LineStyle(font="F", pointsize=70)),
                             ("B", LineStyle(font="", pointsize=156))])
    assert cmd[0] == "magick"
    assert cmd.count("(") == 2 and cmd.count(")") == 2
    assert cmd[-2:] == ["-format", "%w %h\n"] or cmd[-1] == "info:"
    assert cmd[-1] == "info:"


def test_measure_omits_the_font_flag_when_no_face_is_configured():
    cmd = build_measure_cmd([("A", LineStyle(font="", pointsize=70))])
    assert "-font" not in cmd


def test_measure_escapes_the_text_and_passes_it_as_one_argument():
    cmd = build_measure_cmd([("100% @x", LineStyle())])
    assert r"label:100%% \@x" in cmd


def test_parse_metrics_reads_width_and_height_per_line():
    assert parse_metrics("120 42\n980 190\n", expected=2) == [(120, 42), (980, 190)]


@pytest.mark.parametrize("out", ["", "120 42\n", "nonsense\n", "120\n980 190\n"])
def test_parse_metrics_rejects_output_it_cannot_trust(out):
    assert parse_metrics(out, expected=2) is None


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
def test_the_measure_command_actually_runs():
    cmd = build_measure_cmd([("あ", LineStyle(font="", pointsize=40)),
                             ("いい", LineStyle(font="", pointsize=40))])
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    metrics = parse_metrics(out, expected=2)
    assert metrics is not None
    assert metrics[1][0] > metrics[0][0]  # two glyphs are wider than one
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_measure_cmd'`

- [ ] **Step 3: Write the implementation**

Append to `thumbnail.py` (add `from collections.abc import Sequence` to the imports):

```python
def build_measure_cmd(lines: Sequence[tuple[str, LineStyle]]) -> list[str]:
    """Natural width/height of every line, in ONE magick call.

    Layout needs the rendered size of each glyph run, which only ImageMagick
    knows.  Each line is a parenthesised ``label:`` and ``info:`` prints one
    ``%w %h`` row per image in the list, so a set costs one call, not three.
    """
    cmd = ["magick"]
    for text, style in lines:
        cmd += ["(", "-background", "none"]
        if style.font:
            cmd += ["-font", style.font]
        cmd += ["-pointsize", str(style.pointsize), f"label:{escape_magick_text(text)}", ")"]
    cmd += ["-format", "%w %h\n", "info:"]
    return cmd


def parse_metrics(stdout: str, expected: int) -> list[tuple[int, int]] | None:
    """``(width, height)`` per line, or ``None`` if the output is not usable.

    All-or-nothing: a partial read would silently mislay a line, and the
    caller can fall back to the unmeasured path instead.
    """
    out: list[tuple[int, int]] = []
    for row in stdout.split("\n"):
        parts = row.split()
        if not parts:
            continue
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        out.append((int(parts[0]), int(parts[1])))
    return out if len(out) == expected else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py tests/publish/test_thumbnail.py
git commit -m "feat(publish): measure thumbnail text in one magick call"
```

---

### Task 4: Stacking and shrink-to-fit

**Files:**
- Modify: `src/nagare_clip/publish/thumbnail.py`
- Test: `tests/publish/test_thumbnail.py`

**Interfaces:**
- Consumes: `LineStyle`, `SetStyle`, `parse_metrics` output.
- Produces:
  - `PlacedLine(text: str, style: LineStyle, offset: str)` — frozen dataclass; `offset` is the ready-to-use `-annotate` argument (e.g. `"+56+228"`).
  - `layout_lines(lines: Sequence[tuple[str, LineStyle]], metrics: Sequence[tuple[int, int]], set_style: SetStyle, canvas: tuple[int, int], line_gap: int) -> list[PlacedLine]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_thumbnail.py`:

```python
from nagare_clip.publish.thumbnail import PlacedLine, layout_lines

CANVAS = (1280, 720)


def _layout(metrics, gravity="northwest", offset="+56+62", styles=None, gap=12):
    styles = styles or [LineStyle(pointsize=70)] * len(metrics)
    lines = [(f"L{i}", s) for i, s in enumerate(styles)]
    return layout_lines(lines, metrics, SetStyle(gravity=gravity, offset=offset), CANVAS, gap)


def test_lines_stack_downward_by_measured_height_plus_the_gap():
    placed = _layout([(200, 80), (900, 190), (400, 120)])
    assert [p.offset for p in placed] == ["+56+62", "+56+154", "+56+356"]


def test_the_horizontal_offset_is_the_anchor_for_every_line():
    placed = _layout([(200, 80), (900, 190)], offset="-40+30")
    assert [p.offset for p in placed] == ["-40+30", "-40+122"]


def test_south_gravity_stacks_upward_so_the_block_stays_on_screen():
    """With a south* gravity a bigger +y moves UP, so the order reverses;
    the returned list still reads top-to-bottom."""
    placed = _layout([(200, 80), (900, 190)], gravity="southwest")
    assert [p.offset for p in placed] == ["+56+264", "+56+62"]


def test_a_line_wider_than_the_frame_gets_a_smaller_pointsize():
    # 2400px wide at 156pt, usable width is 1280 - 2*56 = 1168 -> factor 0.486
    placed = _layout([(2400, 190)], styles=[LineStyle(pointsize=156)])
    assert placed[0].style.pointsize == 75


def test_shrinking_never_goes_below_the_minimum_pointsize():
    placed = _layout([(40000, 190)], styles=[LineStyle(pointsize=156)])
    assert placed[0].style.pointsize == 8


def test_a_shrunk_line_takes_less_vertical_room():
    placed = _layout([(2400, 200), (100, 100)], styles=[LineStyle(pointsize=156), LineStyle()])
    # 200 * (1168/2400) = 97 -> next line at 62 + 97 + 12
    assert placed[1].offset == "+56+171"


def test_a_line_that_fits_keeps_its_pointsize_exactly():
    placed = _layout([(1000, 190)], styles=[LineStyle(pointsize=156)])
    assert placed[0].style.pointsize == 156


def test_the_text_and_the_rest_of_the_style_survive_layout():
    placed = layout_lines(
        [("穴あけ不要。", LineStyle(fill="#B08D3E", stroke="white", strokewidth=12))],
        [(300, 190)], SetStyle(), CANVAS, 12,
    )
    assert placed[0].text == "穴あけ不要。"
    assert (placed[0].style.fill, placed[0].style.strokewidth) == ("#B08D3E", 12)
    assert isinstance(placed[0], PlacedLine)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlacedLine'`

- [ ] **Step 3: Write the implementation**

Append to `thumbnail.py` (add `from dataclasses import dataclass, replace`):

```python
@dataclass(frozen=True)
class PlacedLine:
    text: str
    style: LineStyle  # pointsize possibly reduced to fit the frame
    offset: str  # the ready-made `-annotate` argument, e.g. "+56+228"


def _parse_offset(offset: str) -> tuple[int, int]:
    x, y = re.findall(r"[+-]\d+", offset)
    return int(x), int(y)


def layout_lines(
    lines: Sequence[tuple[str, LineStyle]],
    metrics: Sequence[tuple[int, int]],
    set_style: SetStyle,
    canvas: tuple[int, int],
    line_gap: int,
) -> list[PlacedLine]:
    """Where each line goes, and how big it may be.

    Positions are ours, not the model's: it can pick the block anchor and a
    point size, but it cannot measure a rendered glyph run, and that is where
    overlap and overflow come from.

    An over-wide line has its **pointsize** scaled down rather than its
    rendered layer resized, because the shadow, outline and fill passes must
    share one size to line up.

    With a ``south*`` gravity a larger ``+y`` moves the text UP, so the block
    is laid out from its last line; the returned list still reads top-to-bottom.
    """
    width, _ = canvas
    x0, y0 = _parse_offset(set_style.offset)
    usable = max(width - 2 * abs(x0), 1)

    sized: list[tuple[str, LineStyle, int]] = []
    for (text, style), (w, h) in zip(lines, metrics):
        if w > usable:
            factor = usable / w
            style = replace(style, pointsize=max(MIN_POINTSIZE, int(style.pointsize * factor)))
            h = int(h * factor)
        sized.append((text, style, h))

    order = list(reversed(sized)) if set_style.gravity.startswith("south") else sized
    placed: list[PlacedLine] = []
    y = y0
    for text, style, h in order:
        placed.append(PlacedLine(text=text, style=style, offset=f"{x0:+d}{y:+d}"))
        y += h + line_gap
    return list(reversed(placed)) if set_style.gravity.startswith("south") else placed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py tests/publish/test_thumbnail.py
git commit -m "feat(publish): stack thumbnail lines and shrink overruns"
```

---

### Task 5: The render command

**Files:**
- Modify: `src/nagare_clip/publish/thumbnail.py`
- Test: `tests/publish/test_thumbnail.py`

**Interfaces:**
- Consumes: `PlacedLine`, `SetStyle`, `escape_magick_text`.
- Produces: `build_render_cmd(background: Path, placed: Sequence[PlacedLine], set_style: SetStyle, canvas: tuple[int, int], out_path: Path) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_thumbnail.py`:

```python
from pathlib import Path

from nagare_clip.publish.thumbnail import build_render_cmd

PLACED = [
    PlacedLine("水槽DIY", LineStyle(font="F1", pointsize=70, fill="white",
                                    stroke="rgba(30,30,30,1)", strokewidth=8), "+56+62"),
    PlacedLine("穴あけ不要。", LineStyle(font="F2", pointsize=156, fill="#B08D3E",
                                        stroke="white", strokewidth=12), "+56+166"),
]


def _render(set_style=SetStyle(), placed=PLACED):
    return build_render_cmd(Path("bg.jpg"), placed, set_style, CANVAS, Path("out.jpg"))


def test_the_background_is_cropped_to_fill_the_canvas():
    cmd = _render()
    assert cmd[:2] == ["magick", "bg.jpg"]
    assert "-resize" in cmd and "1280x720^" in cmd
    assert "-extent" in cmd and "1280x720" in cmd
    assert cmd[-1] == "out.jpg"


def test_each_line_is_drawn_twice_outline_then_fill():
    """make_thumb.sh's two-pass outline: a thick stroke in the outline colour,
    then the fill on top."""
    cmd = _render()
    at = [i for i, a in enumerate(cmd) if a == "-annotate"]
    # 2 shadow passes + 2 outline passes + 2 fill passes
    assert len(at) == 6
    outline = cmd[at[2] - 6 : at[2]]
    assert "rgba(30,30,30,1)" in outline and "8" in outline
    fill = cmd[at[3] - 6 : at[3]]
    assert "white" in fill and "none" in fill


def test_all_three_passes_of_a_line_share_one_pointsize():
    cmd = _render()
    sizes = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-pointsize"]
    assert sizes == ["70", "156", "70", "156", "70", "156"]


def test_the_shadow_layer_is_blurred_and_composited():
    cmd = _render()
    assert "xc:none" in cmd
    assert cmd[cmd.index("-blur") + 1] == "0x8"
    assert "-composite" in cmd
    assert cmd.index("-composite") < cmd.index("-annotate", cmd.index("-composite"))


def test_no_shadow_layer_when_the_set_turns_it_off():
    cmd = _render(SetStyle(shadow=False))
    assert "xc:none" not in cmd and "-composite" not in cmd
    assert len([a for a in cmd if a == "-annotate"]) == 4


def test_the_gravity_is_the_sets_and_the_offsets_are_the_placed_ones():
    cmd = _render(SetStyle(gravity="southeast"))
    assert "southeast" in cmd
    assert cmd[cmd.index("-annotate") + 1] == "+56+62"


def test_text_is_escaped_and_stays_one_argument():
    placed = [PlacedLine("100% @x", LineStyle(font=""), "+0+0")]
    cmd = _render(placed=placed)
    assert r"100%% \@x" in cmd
    assert "-font" not in cmd


def test_the_command_is_an_argument_list_of_plain_strings():
    assert all(isinstance(a, str) for a in _render())


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
def test_a_real_thumbnail_is_produced(tmp_path):
    bg = tmp_path / "bg.jpg"
    subprocess.run(["magick", "-size", "1920x1080", "xc:steelblue", str(bg)], check=True)
    out = tmp_path / "set1.jpg"
    placed = [PlacedLine("100% 水浸し！", LineStyle(font="", pointsize=120), "+56+62")]
    subprocess.run(build_render_cmd(bg, placed, SetStyle(), CANVAS, out), check=True)
    size = subprocess.run(["magick", "identify", "-format", "%wx%h", str(out)],
                          check=True, capture_output=True, text=True).stdout
    assert size == "1280x720"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_render_cmd'`

- [ ] **Step 3: Write the implementation**

Append to `thumbnail.py` (add `from pathlib import Path`):

```python
def _annotate(line: PlacedLine, *, fill: str, stroke: str, strokewidth: int) -> list[str]:
    args: list[str] = []
    if line.style.font:
        args += ["-font", line.style.font]
    args += [
        "-pointsize", str(line.style.pointsize),
        "-fill", fill,
        "-stroke", stroke,
        "-strokewidth", str(strokewidth),
        "-annotate", line.offset, escape_magick_text(line.text),
    ]
    return args


def build_render_cmd(
    background: Path,
    placed: Sequence[PlacedLine],
    set_style: SetStyle,
    canvas: tuple[int, int],
    out_path: Path,
) -> list[str]:
    """One thumbnail, in one magick call.

    Follows ``make_thumb.sh``: the still is cropped to fill the canvas, a
    single blurred layer carries every line's shadow, then each line is drawn
    twice -- a thick stroke in the outline colour, the fill on top.  All three
    passes of a line share one ``-pointsize``, which is why shrink-to-fit
    changes the point size rather than resizing a rendered layer.
    """
    width, height = canvas
    size = f"{width}x{height}"
    cmd = ["magick", str(background),
           "-resize", f"{size}^", "-gravity", "center", "-extent", size]

    if set_style.shadow:
        cmd += ["(", "-size", size, "xc:none", "-gravity", set_style.gravity]
        for line in placed:
            cmd += _annotate(
                line, fill=set_style.shadow_color, stroke=set_style.shadow_color, strokewidth=1
            )
        cmd += ["-blur", set_style.shadow_blur, ")", "-gravity", "center", "-composite"]

    cmd += ["-gravity", set_style.gravity]
    for line in placed:
        cmd += _annotate(
            line, fill=line.style.stroke, stroke=line.style.stroke,
            strokewidth=line.style.strokewidth,
        )
        cmd += _annotate(line, fill=line.style.fill, stroke="none", strokewidth=0)
    cmd += ["-quality", "92", str(out_path)]
    return cmd
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_thumbnail.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py tests/publish/test_thumbnail.py
git commit -m "feat(publish): build the thumbnail render command"
```

---

### Task 6: Config section

**Files:**
- Modify: `src/nagare_clip/config.py` (add `ThumbnailConfig` before `PublishConfig`; add the field to `PublishConfig`)
- Modify: `config.example.yml` (generated)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `cfg["publish"]["thumbnail"]` with keys `enabled: bool`, `background: str`, `width: int`, `height: int`, `line_gap: int`, `fonts: dict[str, str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_publish_thumbnail_defaults():
    cfg = get_effective_config(None, {})
    thumb = cfg["publish"]["thumbnail"]
    assert thumb["enabled"] is True
    assert thumb["background"] == ""
    assert (thumb["width"], thumb["height"]) == (1280, 720)
    assert thumb["line_gap"] == 12
    assert thumb["fonts"] == {}


def test_publish_thumbnail_fonts_come_from_the_file(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        'publish:\n  thumbnail:\n    fonts:\n      hook: "Noto-Serif-CJK-JP-Black"\n',
        encoding="utf-8",
    )
    cfg = get_effective_config(path, {})
    assert cfg["publish"]["thumbnail"]["fonts"] == {"hook": "Noto-Serif-CJK-JP-Black"}


def test_an_unknown_thumbnail_key_is_rejected(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("publish:\n  thumbnail:\n    colour: red\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})
```

`pytest`, `ValidationError` and `get_effective_config` are already imported at the top of `tests/test_config.py`; no import changes are needed.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `KeyError: 'thumbnail'`

- [ ] **Step 3: Write the implementation**

In `src/nagare_clip/config.py`, insert before `class PublishConfig`:

```python
class ThumbnailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "Thumbnail rendering: one image per LLM copy set, composited with ImageMagick\n"
        "(`magick` must be on PATH). The LLM writes the colours, point sizes and\n"
        "placement for its own copy, in ImageMagick's vocabulary; only `fonts` is set\n"
        "here, because the model cannot know what is installed. Renders land in\n"
        "output/publish/thumbnails/ and are embedded in publish.md."
    )
    enabled: bool = Field(True, description="Render a thumbnail per copy set")
    background: str = Field(
        "",
        description=(
            "Still to composite onto: path relative to output/publish/ (or absolute); "
            "empty = the first candidate in the frame shortlist"
        ),
    )
    width: int = Field(1280, description="Canvas width in px")
    height: int = Field(720, description="Canvas height in px")
    line_gap: int = Field(12, description="Vertical gap between stacked lines in px")
    fonts: dict[str, str] = _commented(
        {},
        sample='{sans-bold: "Noto-Sans-CJK-JP-Bold", serif-black: "Noto-Serif-CJK-JP-Black"}',
        description="Font slots the LLM may choose from: slot name -> ImageMagick font name or path",
    )
```

Add to `PublishConfig`, after `frame_width`:

```python
    thumbnail: ThumbnailConfig = Field(default_factory=ThumbnailConfig)
```

- [ ] **Step 4: Regenerate the example config and run the tests**

Run:
```bash
make config-example
uv run pytest tests/test_config.py -v
```
Expected: PASS, including `test_example_file_matches_generator`. Confirm `config.example.yml` now contains a `thumbnail:` block under `publish:` with the commented `# fonts:` sample.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py
git commit -m "feat(config): add publish.thumbnail section"
```

---

### Task 7: The LLM authors the style

**Files:**
- Modify: `src/nagare_clip/publish/publish_llm.py`
- Modify: `src/nagare_clip/config.py` (`PUBLISH_PROMPT`)
- Test: `tests/publish/test_publish_llm.py`

**Interfaces:**
- Consumes: `LINE_KEYS`, `SET_KEYS` (Task 2) — imported from `nagare_clip.publish.thumbnail`.
- Produces:
  - `ThumbLine(role: str, text: str, style: dict[str, Any])` — `style` defaults to `{}`.
  - `ThumbSet(lines: list[ThumbLine], style: dict[str, Any])`.
  - `PublishCopy.thumbnail_copy: list[ThumbSet]`.
  - `thumbnail_copy_to_dict(copy) -> list[dict[str, Any]]` — `{"lines": [{"role", "text", **line style}], **set style}`.
  - `font_slot_note(fonts: Mapping[str, str]) -> str`.

Style values are **carried through unvalidated**; `thumbnail.py` is the single validator, so a hand-edited `publish.json` and an LLM response go through exactly the same checks.

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_publish_llm.py`:

```python
from nagare_clip.publish.publish_llm import ThumbSet, font_slot_note, thumbnail_copy_to_dict


def test_line_style_keys_are_carried_through():
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "hook", "text": "h", "font": "serif-black",
                        "pointsize": 156, "fill": "#B08D3E", "stroke": "white",
                        "strokewidth": 12}]}
        ],
    }
    copy = try_parse_publish_response(json.dumps(data), num_parts=1)
    assert copy is not None
    assert copy.thumbnail_copy[0].lines[0].style == {
        "font": "serif-black", "pointsize": 156, "fill": "#B08D3E",
        "stroke": "white", "strokewidth": 12,
    }


def test_set_style_keys_are_carried_through():
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "hook", "text": "h"}], "gravity": "southwest",
             "offset": "+56+62", "shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"}}
        ],
    }
    copy = try_parse_publish_response(json.dumps(data), num_parts=1)
    assert copy is not None
    assert copy.thumbnail_copy[0].style == {
        "gravity": "southwest", "offset": "+56+62",
        "shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"},
    }


def test_unknown_style_keys_never_reach_the_artifact():
    """Validation lives in thumbnail.py, but an operator-shaped key has no
    business being carried at all."""
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "hook", "text": "h", "-write": "/tmp/x"}], "-delete": "0"}
        ],
    }
    copy = try_parse_publish_response(json.dumps(data), num_parts=1)
    assert copy is not None
    assert copy.thumbnail_copy[0].lines[0].style == {}
    assert copy.thumbnail_copy[0].style == {}


def test_a_set_with_no_style_still_parses():
    data = {"titles": ["A"], "thumbnail_copy": [{"lines": [{"role": "hook", "text": "h"}]}]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=1)
    assert copy is not None
    assert copy.thumbnail_copy == [ThumbSet(lines=[ThumbLine("hook", "h")], style={})]


def test_thumbnail_copy_to_dict_flattens_style_beside_the_text():
    copy = PublishCopy(
        titles=["A"],
        thumbnail_copy=[
            ThumbSet(
                lines=[ThumbLine("hook", "h", {"fill": "#B08D3E", "pointsize": 156})],
                style={"gravity": "southwest"},
            )
        ],
    )
    assert thumbnail_copy_to_dict(copy) == [
        {"lines": [{"role": "hook", "text": "h", "fill": "#B08D3E", "pointsize": 156}],
         "gravity": "southwest"}
    ]


def test_the_prompts_own_thumbnail_example_parses():
    """A stale example in the prompt must fail loudly, not quietly mislead."""
    from nagare_clip.config import DEFAULTS

    prompt = DEFAULTS["publish"]["prompt"]
    start = prompt.index("{\n")
    shape = prompt[start : prompt.index("\n}\n", start) + 3]
    copy = try_parse_publish_response(shape, num_parts=3)
    assert copy is not None
    assert copy.thumbnail_copy and copy.thumbnail_copy[0].lines


def test_the_prompt_tells_the_model_the_sets_must_look_different():
    from nagare_clip.config import DEFAULTS

    assert "differ" in DEFAULTS["publish"]["prompt"]


def test_font_slot_note_lists_the_configured_slots():
    note = font_slot_note({"serif-black": "X", "sans-bold": "Y"})
    assert "sans-bold" in note and "serif-black" in note


def test_the_system_prompt_is_unchanged_when_no_fonts_are_configured(monkeypatch):
    """Regression guard: a project without font slots gets the prompt it had."""
    seen = []

    def fake_call(messages, cfg):
        seen.append(messages[0]["content"])
        return json.dumps({"titles": ["A"]})

    project = ProjectSummary("s", [PartSummary("a", (1, 2), "p")])
    generate_publish_copy(project, {"prompt": "BASE"}, call_llm=fake_call)
    generate_publish_copy(project, {"prompt": "BASE", "thumbnail": {"fonts": {}}},
                          call_llm=fake_call)
    assert seen == ["BASE", "BASE"]


def test_the_font_slots_are_appended_to_the_system_prompt(monkeypatch):
    seen = []

    def fake_call(messages, cfg):
        seen.append(messages[0]["content"])
        return json.dumps({"titles": ["A"]})

    project = ProjectSummary("s", [PartSummary("a", (1, 2), "p")])
    generate_publish_copy(
        project, {"prompt": "BASE", "thumbnail": {"fonts": {"sans-bold": "X"}}},
        call_llm=fake_call,
    )
    assert seen[0].startswith("BASE\n\n")
    assert "sans-bold" in seen[0]
```

Also update the existing assertions in this file that compare against `list[list[ThumbLine]]` — they become `ThumbSet` comparisons:

- `test_...` at line ~54: `copy.thumbnail_copy == [ThumbSet(lines=[ThumbLine(...), ...], style={})]`
- line ~107: `[len(s.lines) for s in copy.thumbnail_copy] == [1, 3]`
- lines ~113, ~125: wrap the expected list in `ThumbSet(lines=[...], style={})`
- line ~146: `[line.text for line in copy.thumbnail_copy[0].lines]`

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_publish_llm.py -v`
Expected: FAIL — `ImportError: cannot import name 'ThumbSet'`

- [ ] **Step 3: Write the implementation**

In `src/nagare_clip/publish/publish_llm.py`:

```python
from collections.abc import Mapping

from nagare_clip.publish.thumbnail import LINE_KEYS, SET_KEYS


@dataclass(frozen=True)
class ThumbLine:
    role: str  # one of VALID_ROLES
    text: str
    style: dict[str, Any] = field(default_factory=dict)  # raw; validated at render time


@dataclass(frozen=True)
class ThumbSet:
    """One alternative: its copy, and the look the model chose for that copy."""

    lines: list[ThumbLine] = field(default_factory=list)
    style: dict[str, Any] = field(default_factory=dict)  # gravity / offset / shadow
```

Change `PublishCopy.thumbnail_copy` to `list[ThumbSet]`.

Add the key picker and use it in `_parse_thumb_set`:

```python
def _pick(raw: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """Only the style keys we know; anything else never enters the artifact."""
    return {k: raw[k] for k in keys if k in raw}
```

In `_parse_thumb_set`, return a `ThumbSet` instead of a list — build each line as
`ThumbLine(role=role, text=text, style=_pick(entry, LINE_KEYS))`, trim to
`MAX_THUMB_LINES` as today, and finish with
`return ThumbSet(lines=lines, style=_pick(raw, SET_KEYS) if isinstance(raw, dict) else {})`.

In `try_parse_publish_response`, append the set when `thumb_set.lines` is non-empty.

Add the prompt addendum:

```python
def font_slot_note(fonts: Mapping[str, str]) -> str:
    """The one-line prompt addendum naming the installed font slots.

    Generated rather than written into PUBLISH_PROMPT so the names the LLM is
    offered are always the names the renderer can resolve.
    """
    return (
        'Thumbnail "font" must be one of these slot names: '
        + ", ".join(sorted(fonts))
        + ". A line with no font, or an unknown one, uses the default face."
    )
```

In `generate_publish_copy`, before building `messages`:

```python
    system_prompt = cfg.get("prompt", "")
    fonts = (cfg.get("thumbnail") or {}).get("fonts") or {}
    if fonts:
        system_prompt = f"{system_prompt}\n\n{font_slot_note(fonts)}"
```

and use `system_prompt` in the system message.

Replace `thumbnail_copy_to_dict`:

```python
def thumbnail_copy_to_dict(copy: PublishCopy) -> list[dict[str, Any]]:
    """The thumbnail sets as plain JSON: copy and look together.

    The line count is never normalised -- a punchier video may want only a
    hook.  Style keys sit beside the text they apply to, which is what makes
    publish.json hand-editable: change a colour, re-run the render CLI.
    """
    return [
        {
            "lines": [
                {"role": line.role, "text": line.text, **line.style} for line in thumb_set.lines
            ],
            **thumb_set.style,
        }
        for thumb_set in copy.thumbnail_copy
    ]
```

In `src/nagare_clip/config.py`, replace the `"thumbnail_copy"` shape lines and rule in `PUBLISH_PROMPT`:

```python
    '  "thumbnail_copy": [\n'
    '    {"lines": [{"role": "tag", "text": "...", "font": "<slot>", "pointsize": 70,\n'
    '                "fill": "white", "stroke": "rgba(30,30,30,1)", "strokewidth": 8},\n'
    '               {"role": "hook", "text": "...", "font": "<slot>", "pointsize": 156,\n'
    '                "fill": "#B08D3E", "stroke": "rgba(250,250,250,1)", "strokewidth": 12}],\n'
    '     "gravity": "northwest", "offset": "+56+62",\n'
    '     "shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"}}\n'
    "  ]\n"
```

and extend the `"thumbnail_copy"` rule with:

```python
    "- Each thumbnail set also carries its own LOOK, as ImageMagick options "
    "on a 1280x720 canvas: per line \"fill\" and \"stroke\" colours "
    "(#RRGGBB or rgba(r,g,b,a)), \"strokewidth\" (0-40, the outline that "
    "keeps text readable over a photo), \"pointsize\" (8-400; a hook is "
    "large, a tag small); per set \"gravity\" (northwest / north / … / "
    "southeast), \"offset\" (+x+y from that corner) and \"shadow\". The sets "
    "must DIFFER visibly from each other in colour and placement, not only "
    "in wording — they are alternatives a human chooses between. Line "
    "positions are computed, so give the block anchor, not a position per "
    "line.\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_publish_llm.py -v && make config-example`
Expected: PASS; `config.example.yml` unchanged by the regeneration except the prompt is a commented sample, so verify `git diff --stat config.example.yml` is empty.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/publish_llm.py src/nagare_clip/config.py tests/publish/test_publish_llm.py
git commit -m "feat(publish): let the LLM author each thumbnail set's look"
```

---

### Task 8: Rendering a set, end to end

**Files:**
- Modify: `src/nagare_clip/publish/thumbnail.py`
- Modify: `src/nagare_clip/pipeline/external.py`
- Test: `tests/publish/test_thumbnail.py`, `tests/pipeline/test_external.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5, `ThumbSet` (Task 7), `ThumbShot` (`publish/thumbs.py`).
- Produces:
  - `ThumbRender(index: int, path: str, background: str)` — frozen dataclass; `path`/`background` relative to the publish stage dir where possible.
  - `set_relpath(index: int) -> str` → `"thumbnails/set{index}.jpg"`.
  - `resolve_background(thumbnail_cfg: Mapping[str, Any], thumbs: Sequence[ThumbShot], stage_dir: Path) -> Path | None`
  - `render_sets(sets, thumbs, thumbnail_cfg, stage_dir, run) -> list[ThumbRender]` where `run: Callable[[list[str]], str]` returns stdout.
  - `external.run_magick(cmd: list[str]) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_thumbnail.py`:

```python
from nagare_clip.publish.thumbnail import (
    ThumbRender,
    render_sets,
    resolve_background,
    set_relpath,
)
from nagare_clip.publish.publish_llm import ThumbLine, ThumbSet
from nagare_clip.publish.thumbs import ThumbShot

CFG = {"enabled": True, "background": "", "width": 1280, "height": 720,
       "line_gap": 12, "fonts": {"serif-black": "SerifFace"}}


def _shot(path="frames/a/1.000.jpg"):
    return ThumbShot(stem="a", time=1.0, kind="overlay", label="l", path=path)


def _touch(stage_dir, rel):
    p = stage_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


class FakeRun:
    """Records commands; answers measure calls with plausible metrics."""

    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.cmds.append(cmd)
        if cmd[-1] == "info:":
            return "".join("300 90\n" for a in cmd if a.startswith("label:"))
        return ""


def test_the_background_defaults_to_the_first_candidate(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    got = resolve_background(CFG, [_shot()], tmp_path)
    assert got == tmp_path / "frames/a/1.000.jpg"


def test_a_configured_background_wins(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    _touch(tmp_path, "frames/a/9.000.jpg")
    cfg = {**CFG, "background": "frames/a/9.000.jpg"}
    assert resolve_background(cfg, [_shot()], tmp_path) == tmp_path / "frames/a/9.000.jpg"


def test_an_absolute_background_is_used_as_is(tmp_path):
    p = _touch(tmp_path, "elsewhere.jpg")
    cfg = {**CFG, "background": str(p)}
    assert resolve_background(cfg, [], tmp_path) == p


def test_a_missing_configured_background_is_reported_and_gives_nothing(tmp_path, caplog):
    cfg = {**CFG, "background": "nope.jpg"}
    assert resolve_background(cfg, [_shot()], tmp_path) is None
    assert "nope.jpg" in caplog.text


def test_no_candidates_and_no_config_means_no_background(tmp_path):
    assert resolve_background(CFG, [], tmp_path) is None


def test_a_candidate_whose_file_vanished_is_skipped(tmp_path):
    _touch(tmp_path, "frames/a/2.000.jpg")
    shots = [_shot("frames/a/1.000.jpg"), _shot("frames/a/2.000.jpg")]
    assert resolve_background(CFG, shots, tmp_path) == tmp_path / "frames/a/2.000.jpg"


def _sets(n=2):
    return [
        ThumbSet(lines=[ThumbLine("hook", f"H{i}", {"font": "serif-black"})], style={})
        for i in range(n)
    ]


def test_one_measure_and_one_render_call_per_set(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    run = FakeRun()
    renders = render_sets(_sets(2), [_shot()], CFG, tmp_path, run)
    assert len(run.cmds) == 4
    assert [c[-1] for c in run.cmds] == [
        "info:", str(tmp_path / "thumbnails/set1.jpg"),
        "info:", str(tmp_path / "thumbnails/set2.jpg"),
    ]
    assert renders == [
        ThumbRender(1, "thumbnails/set1.jpg", "frames/a/1.000.jpg"),
        ThumbRender(2, "thumbnails/set2.jpg", "frames/a/1.000.jpg"),
    ]


def test_the_font_slot_is_resolved_before_the_command_is_built(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    run = FakeRun()
    render_sets(_sets(1), [_shot()], CFG, tmp_path, run)
    assert "SerifFace" in run.cmds[1]


def test_sets_without_a_usable_style_get_different_presets(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    run = FakeRun()
    render_sets(_sets(2), [_shot()], CFG, tmp_path, run)
    first, second = run.cmds[1], run.cmds[3]
    assert first[first.index("-gravity", 3) :] != second[second.index("-gravity", 3) :]


def test_the_output_directory_is_created(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    render_sets(_sets(1), [_shot()], CFG, tmp_path, FakeRun())
    assert (tmp_path / "thumbnails").is_dir()


def test_disabled_renders_nothing(tmp_path):
    _touch(tmp_path, "frames/a/1.000.jpg")
    run = FakeRun()
    assert render_sets(_sets(2), [_shot()], {**CFG, "enabled": False}, tmp_path, run) == []
    assert run.cmds == []


def test_no_background_renders_nothing(tmp_path, caplog):
    run = FakeRun()
    assert render_sets(_sets(1), [], CFG, tmp_path, run) == []
    assert run.cmds == []
    assert "background" in caplog.text


def test_a_failing_magick_drops_only_that_set(tmp_path, caplog):
    _touch(tmp_path, "frames/a/1.000.jpg")

    calls = {"n": 0}

    def run(cmd):
        calls["n"] += 1
        if calls["n"] == 2:  # the first set's render
            raise OSError("magick: boom")
        return FakeRun()(cmd)

    renders = render_sets(_sets(2), [_shot()], CFG, tmp_path, run)
    assert [r.index for r in renders] == [2]
    assert "boom" in caplog.text


def test_a_missing_magick_binary_drops_every_set_without_raising(tmp_path, caplog):
    _touch(tmp_path, "frames/a/1.000.jpg")

    def run(cmd):
        raise FileNotFoundError("magick")

    assert render_sets(_sets(2), [_shot()], CFG, tmp_path, run) == []
    assert "magick" in caplog.text


def test_unusable_measure_output_still_renders_the_set(tmp_path):
    """Falling back to unmeasured layout beats losing the thumbnail."""
    _touch(tmp_path, "frames/a/1.000.jpg")

    def run(cmd):
        return "garbage\n" if cmd[-1] == "info:" else ""

    assert len(render_sets(_sets(1), [_shot()], CFG, tmp_path, run)) == 1


def test_set_relpath_is_one_based():
    assert set_relpath(1) == "thumbnails/set1.jpg"
```

Append to `tests/pipeline/test_external.py`:

```python
def test_run_magick_returns_stdout():
    from nagare_clip.pipeline.external import run_magick

    assert run_magick(["printf", "hello"]) == "hello"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_thumbnail.py tests/pipeline/test_external.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_sets'`

- [ ] **Step 3: Write the implementation**

Append to `thumbnail.py`:

```python
@dataclass(frozen=True)
class ThumbRender:
    index: int  # 1-based set number, matching publish.md's "Set N"
    path: str  # relative to the publish stage dir
    background: str  # the still it was composited onto


def set_relpath(index: int) -> str:
    """Render path relative to the publish stage dir."""
    return f"thumbnails/set{index}.jpg"


def resolve_background(
    thumbnail_cfg: Mapping[str, Any], thumbs: Sequence[Any], stage_dir: Path
) -> Path | None:
    """The still every set is composited onto.

    Config wins; otherwise the first candidate that is actually on disk --
    picking the frame is quick, and the shortlist is already ordered by how
    interesting the director thought the moment was.
    """
    configured = str(thumbnail_cfg.get("background", "")).strip()
    if configured:
        path = Path(configured)
        path = path if path.is_absolute() else stage_dir / path
        if path.is_file():
            return path
        logger.warning("publish: thumbnail background %s not found", configured)
        return None
    for shot in thumbs:
        path = stage_dir / shot.path
        if path.is_file():
            return path
    return None


def render_sets(
    sets: Sequence[Any],
    thumbs: Sequence[Any],
    thumbnail_cfg: Mapping[str, Any],
    stage_dir: Path,
    run: Callable[[list[str]], str],
) -> list[ThumbRender]:
    """One rendered thumbnail per copy set, so publish.md shows options.

    Two calls per set: measure, then render.  *run* returns stdout, and is
    injected so this module never starts a subprocess itself.

    Nothing here is allowed to fail the stage: a set whose magick call dies is
    dropped with a warning and the rest still render.
    """
    if not thumbnail_cfg.get("enabled", True) or not sets:
        return []
    background = resolve_background(thumbnail_cfg, thumbs, stage_dir)
    if background is None:
        logger.warning("publish: no thumbnail background available; nothing rendered")
        return []

    fonts = thumbnail_cfg.get("fonts") or {}
    canvas = (int(thumbnail_cfg.get("width", 1280)), int(thumbnail_cfg.get("height", 720)))
    line_gap = int(thumbnail_cfg.get("line_gap", 12))
    out_dir = stage_dir / "thumbnails"
    out_dir.mkdir(parents=True, exist_ok=True)

    renders: list[ThumbRender] = []
    for index, thumb_set in enumerate(sets, start=1):
        preset = preset_for(index)
        styled = [
            (line.text, resolve_line_style(line.style, line.role, fonts, preset))
            for line in thumb_set.lines
        ]
        if not styled:
            continue
        set_style = resolve_set_style(thumb_set.style, preset)
        try:
            metrics = parse_metrics(run(build_measure_cmd(styled)), expected=len(styled))
        except Exception:  # noqa: BLE001 - a dead magick must not fail the stage
            logger.warning("publish: could not measure thumbnail set %d", index, exc_info=True)
            continue
        if metrics is None:
            # Unmeasured fallback: point size is a fair proxy for line height.
            logger.warning("publish: unusable text metrics for set %d; estimating", index)
            metrics = [(1, int(style.pointsize * 1.2)) for _, style in styled]
        placed = layout_lines(styled, metrics, set_style, canvas, line_gap)
        rel = set_relpath(index)
        try:
            run(build_render_cmd(background, placed, set_style, canvas, stage_dir / rel))
        except Exception:  # noqa: BLE001
            logger.warning("publish: could not render thumbnail set %d", index, exc_info=True)
            continue
        try:
            bg_name = str(background.relative_to(stage_dir))
        except ValueError:
            bg_name = str(background)
        renders.append(ThumbRender(index=index, path=rel, background=bg_name))
    logger.info("publish: rendered %d thumbnail(s)", len(renders))
    return renders
```

Add `from collections.abc import Callable, Mapping, Sequence` to the imports.

Append to `src/nagare_clip/pipeline/external.py`:

```python
def run_magick(cmd: list[str]) -> str:
    """Run an ImageMagick command and return its stdout.

    ImageMagick is a host binary here, like `blender` -- the whisperx image has
    neither ImageMagick nor CJK fonts, and font slots resolve through host
    fontconfig.  Never `shell=True`: the copy is LLM-written.
    """
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish/test_thumbnail.py tests/pipeline/test_external.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py src/nagare_clip/pipeline/external.py tests/publish/test_thumbnail.py tests/pipeline/test_external.py
git commit -m "feat(publish): render one thumbnail per copy set"
```

---

### Task 9: The artifact and the review page

**Files:**
- Modify: `src/nagare_clip/publish/run.py`
- Test: `tests/publish/test_run.py`

**Interfaces:**
- Consumes: `ThumbRender`, `ThumbSet`, `thumbnail_copy_to_dict`.
- Produces: `run_publish(..., render: Callable[[Sequence[ThumbSet], Sequence[ThumbShot]], list[ThumbRender]] | None = None)`; `publish.json` gains `renders: list[{set, path, background}]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/publish/test_run.py`:

```python
from nagare_clip.publish.publish_llm import ThumbSet
from nagare_clip.publish.thumbnail import ThumbRender


def _sets():
    return [
        ThumbSet(lines=[ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")], style={}),
        ThumbSet(lines=[ThumbLine("hook", "穴あけ不要。")], style={"gravity": "southwest"}),
    ]


def test_disabled_artifact_has_an_empty_renders_array(tmp_path):
    data, _ = _write(tmp_path, {"publish": {"enabled": False}})
    assert data["renders"] == []


def test_the_renders_are_recorded_in_the_artifact(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    render = lambda sets, thumbs: [ThumbRender(1, "thumbnails/set1.jpg", "frames/a/1.000.jpg")]
    data, _ = _write(tmp_path, {"publish": {"enabled": True}}, render=render)
    assert data["renders"] == [
        {"set": 1, "path": "thumbnails/set1.jpg", "background": "frames/a/1.000.jpg"}
    ]


def test_the_renderer_receives_the_sets_and_the_shortlist(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    seen = {}

    def render(sets, thumbs):
        seen["sets"] = sets
        seen["thumbs"] = thumbs
        return []

    shots = [ThumbShot("a", 12.0, "overlay", "l", "frames/a/12.000.jpg")]
    _write(tmp_path, {"publish": {"enabled": True}}, render=render, thumbs=shots)
    assert len(seen["sets"]) == 2
    assert seen["thumbs"] == shots


def test_each_rendered_thumbnail_appears_under_its_copy_set(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    render = lambda sets, thumbs: [
        ThumbRender(1, "thumbnails/set1.jpg", "b.jpg"),
        ThumbRender(2, "thumbnails/set2.jpg", "b.jpg"),
    ]
    _, md = _write(tmp_path, {"publish": {"enabled": True}}, render=render)
    text = md.read_text(encoding="utf-8")
    first = text.index("### Set 1")
    second = text.index("### Set 2")
    assert first < text.index('<img src="thumbnails/set1.jpg"') < second
    assert second < text.index('<img src="thumbnails/set2.jpg"')


def test_a_set_without_a_render_still_shows_its_copy(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    render = lambda sets, thumbs: [ThumbRender(2, "thumbnails/set2.jpg", "b.jpg")]
    _, md = _write(tmp_path, {"publish": {"enabled": True}}, render=render)
    text = md.read_text(encoding="utf-8")
    assert "水浸し！" in text
    assert "thumbnails/set1.jpg" not in text


def test_no_renderer_leaves_the_markdown_without_images(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    _, md = _write(tmp_path, {"publish": {"enabled": True}})
    assert "<img" not in md.read_text(encoding="utf-8").split("## Thumbnail frame")[0]


def test_the_candidate_table_shows_the_still_not_its_path(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy())
    shots = [ThumbShot("a", 12.0, "overlay", "水浸し！", "frames/a/12.000.jpg")]
    _, md = _write(tmp_path, {"publish": {"enabled": True}}, thumbs=shots)
    text = md.read_text(encoding="utf-8")
    assert '<img src="frames/a/12.000.jpg" width="240">' in text
    assert "`frames/a/12.000.jpg`" not in text
```

Update `_copy()` in this file so its default `thumbnail_copy` is
`[ThumbSet(lines=[ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")], style={})]`,
and update `test_disabled_writes_an_empty_artifact` and
`test_enabled_writes_titles_and_description_with_chapters` for the new
`renders` key and the `{"lines": [...]}` shape of `thumbnail_copy`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_run.py -v`
Expected: FAIL — `TypeError: run_publish() got an unexpected keyword argument 'render'`

- [ ] **Step 3: Write the implementation**

In `src/nagare_clip/publish/run.py`:

- add `"renders": []` to `empty_publish()`;
- import `ThumbRender` from `nagare_clip.publish.thumbnail` and `ThumbSet` from `publish_llm`;
- add the parameter and call it after the copy is parsed:

```python
    render: Callable[[Sequence[ThumbSet], Sequence[ThumbShot]], list[ThumbRender]] | None = None,
```

```python
        renders = list(render(copy.thumbnail_copy, thumbs or [])) if render else []
```

- add to `data`: `"renders": [{"set": r.index, "path": r.path, "background": r.background} for r in renders]`;
- extend the log line with `%d thumbnail render(s)`.

In `_render_markdown`, the thumbnail-copy section becomes:

```python
    lines += ["## Thumbnail copy", ""]
    by_index = {r["set"]: r["path"] for r in data["renders"]}
    if data["thumbnail_copy"]:
        for i, thumb_set in enumerate(data["thumbnail_copy"], start=1):
            lines.append(f"### Set {i}")
            lines += [f"- {line['role']}: {line['text']}" for line in thumb_set["lines"]]
            if i in by_index:
                lines += ["", f'<img src="{by_index[i]}" width="480">']
            lines.append("")
    else:
        lines += ["_(none)_", ""]
```

and the candidate table's last column becomes
`f'<img src="{thumb["path"]}" width="240">'` in place of the backticked path.
A 1280px still in a five-column cell renders at the previewer's discretion; a
fixed width does not.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/run.py tests/publish/test_run.py
git commit -m "feat(publish): show rendered thumbnails and stills in publish.md"
```

---

### Task 10: Pipeline wiring

**Files:**
- Modify: `src/nagare_clip/pipeline/stages.py`
- Test: `tests/publish/test_stage_wiring.py`

**Interfaces:**
- Consumes: `render_sets`, `external.run_magick`.
- Produces: `_render_thumbnails(ctx, sets, thumbs) -> list[ThumbRender]`, passed to `run_publish(render=…)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/publish/test_stage_wiring.py` (follow the existing fixtures in that file for building a `PipelineContext`):

This file already provides a `ctx` fixture and imports the stages module as `st`
— use both rather than building a new context.

```python
def test_the_publish_stage_passes_a_renderer(ctx, monkeypatch):
    seen = {}

    def fake_run_publish(*args, **kwargs):
        seen["render"] = kwargs.get("render")

    monkeypatch.setattr(st, "run_publish", fake_run_publish)
    monkeypatch.setattr(st, "_recorder", lambda c, s: _NullRec())
    _enable(ctx)
    st._publish_run(ctx)
    assert callable(seen["render"])


def test_the_renderer_uses_run_magick(ctx, monkeypatch):
    calls = {}

    def fake_render_sets(sets, thumbs, cfg, stage_dir, run):
        calls["run"] = run
        calls["cfg"] = cfg
        return []

    monkeypatch.setattr(st, "render_sets", fake_render_sets)
    _enable(ctx)
    st._render_thumbnails(ctx, [], [])
    assert calls["run"] is st.run_magick
    assert calls["cfg"] == ctx.cfg["publish"]["thumbnail"]
```

Check how the existing tests in this file stub the recorder before copying the
`_recorder` line — if they rely on a different mechanism, follow theirs.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_stage_wiring.py -v`
Expected: FAIL — `AttributeError: module 'nagare_clip.pipeline.stages' has no attribute '_render_thumbnails'`

- [ ] **Step 3: Write the implementation**

In `src/nagare_clip/pipeline/stages.py`:

```python
from nagare_clip.pipeline.external import run_magick
from nagare_clip.publish.thumbnail import ThumbRender, render_sets
```

```python
def _render_thumbnails(
    ctx: PipelineContext, sets: Sequence[Any], thumbs: Sequence[ThumbShot]
) -> list[ThumbRender]:
    """Composite each copy set over the chosen still (host ImageMagick)."""
    return render_sets(
        sets, thumbs, ctx.cfg["publish"]["thumbnail"], ctx.stage_dir("publish"), run_magick
    )
```

and in `_publish_run`, pass it:

```python
            render=lambda sets, shots: _render_thumbnails(ctx, sets, shots),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish tests/pipeline -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/pipeline/stages.py tests/publish/test_stage_wiring.py
git commit -m "feat(pipeline): wire thumbnail rendering into the publish stage"
```

---

### Task 11: The standalone re-render CLI

**Files:**
- Modify: `src/nagare_clip/publish/thumbnail.py`
- Create: `tests/publish/test_thumbnail_cli.py`

**Interfaces:**
- Consumes: `render_sets`, `get_effective_config`, `ThumbSet`/`ThumbLine`.
- Produces:
  - `sets_from_dict(data: Any) -> list[ThumbSet]` — read `thumbnail_copy` back out of `publish.json`.
  - `main(argv: Sequence[str] | None = None) -> int`.

Picking a background is iterative — you look at four renders and want the same
copy over a different frame. Re-running `--from-stage publish` would re-run the
LLM and give you copy you were not judging, so the CLI re-renders from the
existing `publish.json` with no model call. It also makes `publish.json` the
hand-editable contract for the look, like `_director.json` is for the edit.

- [ ] **Step 1: Write the failing tests**

Create `tests/publish/test_thumbnail_cli.py`:

```python
"""The standalone re-render CLI: swap a background or hand-edit a colour in
publish.json and rebuild the images, with no LLM call."""

from __future__ import annotations

import json

import pytest

from nagare_clip.publish import thumbnail
from nagare_clip.publish.publish_llm import ThumbLine, ThumbSet

PUBLISH_JSON = {
    "titles": ["A"],
    "thumbnail_copy": [
        {"lines": [{"role": "tag", "text": "水槽DIY"},
                   {"role": "hook", "text": "水浸し！", "fill": "#B08D3E"}],
         "gravity": "northwest", "offset": "+56+62"},
        {"lines": [{"role": "hook", "text": "穴あけ不要。"}]},
    ],
    "thumbnails": [{"stem": "a", "path": "frames/a/1.000.jpg"}],
    "renders": [],
}


def test_sets_are_read_back_out_of_publish_json():
    sets = thumbnail.sets_from_dict(PUBLISH_JSON)
    assert sets[0] == ThumbSet(
        lines=[ThumbLine("tag", "水槽DIY", {}),
               ThumbLine("hook", "水浸し！", {"fill": "#B08D3E"})],
        style={"gravity": "northwest", "offset": "+56+62"},
    )
    assert sets[1].style == {}


@pytest.mark.parametrize("data", [{}, {"thumbnail_copy": "x"}, {"thumbnail_copy": [1, None]}])
def test_a_file_with_no_usable_sets_reads_as_empty(data):
    assert thumbnail.sets_from_dict(data) == []


def _project(tmp_path):
    stage = tmp_path / "output" / "publish"
    (stage / "frames" / "a").mkdir(parents=True)
    (stage / "frames" / "a" / "1.000.jpg").write_bytes(b"x")
    (stage / "publish.json").write_text(json.dumps(PUBLISH_JSON), encoding="utf-8")
    return stage


def test_the_cli_renders_every_set_without_an_llm_call(tmp_path, monkeypatch):
    stage = _project(tmp_path)
    cmds = []
    monkeypatch.setattr(
        thumbnail, "_runner",
        lambda: (lambda cmd: (cmds.append(cmd), "300 90\n" * 3)[1]),
    )
    assert thumbnail.main(["--publish-dir", str(stage)]) == 0
    assert len([c for c in cmds if c[-1] != "info:"]) == 2


def test_the_cli_background_flag_overrides_the_config(tmp_path, monkeypatch):
    stage = _project(tmp_path)
    (stage / "frames" / "a" / "9.000.jpg").write_bytes(b"x")
    cmds = []
    monkeypatch.setattr(
        thumbnail, "_runner",
        lambda: (lambda cmd: (cmds.append(cmd), "300 90\n" * 3)[1]),
    )
    thumbnail.main(["--publish-dir", str(stage), "--background", "frames/a/9.000.jpg"])
    assert str(stage / "frames/a/9.000.jpg") in cmds[1]


def test_the_cli_reports_a_missing_publish_json(tmp_path, capsys):
    assert thumbnail.main(["--publish-dir", str(tmp_path)]) == 1
    assert "publish.json" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/publish/test_thumbnail_cli.py -v`
Expected: FAIL — `AttributeError: module … has no attribute 'sets_from_dict'`

- [ ] **Step 3: Write the implementation**

Append to `thumbnail.py`:

```python
def sets_from_dict(data: Any) -> list[Any]:
    """``thumbnail_copy`` read back out of publish.json.

    Lenient, like every hand-editable intermediate in this pipeline: a
    malformed set is dropped, never raised.
    """
    from nagare_clip.publish.publish_llm import LINE_KEYS as _LK  # noqa: F401
    from nagare_clip.publish.publish_llm import ThumbLine, ThumbSet

    raw_sets = data.get("thumbnail_copy") if isinstance(data, Mapping) else None
    out = []
    for raw in raw_sets if isinstance(raw_sets, list) else []:
        if not isinstance(raw, Mapping):
            continue
        lines = []
        for entry in raw.get("lines") if isinstance(raw.get("lines"), list) else []:
            if not isinstance(entry, Mapping):
                continue
            role = str(entry.get("role", "")).strip().lower()
            text = str(entry.get("text", "")).strip()
            if not text:
                continue
            lines.append(
                ThumbLine(role=role, text=text, style={k: entry[k] for k in LINE_KEYS if k in entry})
            )
        if lines:
            out.append(
                ThumbSet(lines=lines, style={k: raw[k] for k in SET_KEYS if k in raw})
            )
    return out


def _runner() -> Callable[[list[str]], str]:
    """The subprocess runner, indirected so tests never shell out."""
    from nagare_clip.pipeline.external import run_magick

    return run_magick


def main(argv: Sequence[str] | None = None) -> int:
    """Re-render the thumbnails from an existing publish.json.

    Picking the background and nudging a colour is iterative; re-running the
    stage would re-run the LLM and change the copy being judged.
    """
    import argparse
    import json
    import sys

    from nagare_clip.config import get_effective_config

    parser = argparse.ArgumentParser(description="Re-render publish thumbnails")
    parser.add_argument("--publish-dir", default="output/publish",
                        help="directory holding publish.json (default: output/publish)")
    parser.add_argument("--config", default=None, help="config YAML")
    parser.add_argument("--background", default=None,
                        help="still to composite onto (overrides publish.thumbnail.background)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    stage_dir = Path(args.publish_dir)
    publish_json = stage_dir / "publish.json"
    if not publish_json.is_file():
        print(f"error: no publish.json at {publish_json}", file=sys.stderr)
        return 1
    data = json.loads(publish_json.read_text(encoding="utf-8"))

    cfg = get_effective_config(Path(args.config) if args.config else None, {})
    thumbnail_cfg = dict(cfg["publish"]["thumbnail"])
    if args.background:
        thumbnail_cfg["background"] = args.background
    thumbnail_cfg["enabled"] = True

    shots = [
        type("Shot", (), {"path": str(t.get("path", ""))})()
        for t in data.get("thumbnails", [])
        if isinstance(t, Mapping)
    ]
    renders = render_sets(sets_from_dict(data), shots, thumbnail_cfg, stage_dir, _runner())
    for render in renders:
        print(f"set {render.index}: {stage_dir / render.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note the `LINE_KEYS`/`SET_KEYS` used by `sets_from_dict` are this module's own
(Task 2) — drop the stray `publish_llm` import line if ruff flags it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/publish -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/publish/thumbnail.py tests/publish/test_thumbnail_cli.py
git commit -m "feat(publish): add a standalone thumbnail re-render CLI"
```

---

### Task 12: Documentation and full validation

**Files:**
- Modify: `README.md`, `plan.md`, `AGENTS.md`, `docs/stages/publish.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Update `docs/stages/publish.md`**

Add a "Thumbnail rendering" section covering: the two magick calls per set; the
allowed style keys table from this plan's Global Constraints; the escaping rules
and *why* (`%w` expands, backslash escapes, `label:@file` reads a file); that
positions are computed and only the block anchor comes from the model; the
round-robin preset fallback; `publish.json`'s `renders` array; and the
`python -m nagare_clip.publish.thumbnail --publish-dir output/publish
--background frames/…/2528.021.jpg` re-render loop. Update the file's table of
outputs with `output/publish/thumbnails/setN.jpg`.

- [ ] **Step 2: Update `AGENTS.md`**

- In the `publish` stage section, replace the sentence saying compositing "stays
  a project-level script" with the rendering behaviour, and state that the LLM
  authors the style values while the code builds every command.
- Under **Hard Constraints**, amend the media-tooling line: ffmpeg still routes
  through the whisperx image; ImageMagick (`magick`) is a **host** binary, like
  `blender`, because the whisperx image has neither ImageMagick nor CJK fonts
  and font slots resolve through host fontconfig.
- Add `thumbnail.py` to the `publish/` entry of the Project Structure tree.

- [ ] **Step 3: Update `README.md`**

Note that `publish` renders a thumbnail per copy set into
`output/publish/thumbnails/`, that it needs ImageMagick on PATH, and document
the re-render CLI.

(AGENTS.md's documentation policy also names `plan.md`, but no such file exists
in the repo — skip it rather than creating one.)

- [ ] **Step 4: Run the full check**

Run: `make check`
Expected: ruff lint + format-check clean, `make validate` clean, full pytest
suite green. Report the summary line.

Then confirm the mutation evidence for anything written against pre-existing
code (per the repo's global TDD instruction): mutate
`escape_magick_text` (drop the `%` rule), `layout_lines` (remove the shrink
clamp) and `_render_markdown` (revert the `<img>` cell), confirm the relevant
tests fail, revert, and report which test caught each.

- [ ] **Step 5: Commit**

```bash
git add README.md plan.md AGENTS.md docs/stages/publish.md
git commit -m "docs: publish stage renders thumbnail candidates"
```

---

## Self-Review Notes

- **Spec coverage:** render per copy set (T8/T9), background default + override (T8/T11), style from model in magick vocabulary (T2/T7), images in `publish.md` for both sets and candidates (T9), argv never shell (T1/T5, asserted), `%` escaping (T1), host ImageMagick (T8), degradation (T8), config (T6), CLI (T11), docs (T12).
- **Deviations** from the spec are listed under Global Constraints and are deliberate: round-robin instead of random presets, `line_gap` added to config, `render_sets` in `thumbnail.py` with an injected runner.
- **Type consistency:** `ThumbRender(index, path, background)` is used identically in T8, T9 and T10; `ThumbSet(lines, style)` in T7, T9, T11; `LineStyle`/`SetStyle` field names match between T2, T4 and T5; `render_sets(sets, thumbs, thumbnail_cfg, stage_dir, run)` has the same signature in T8, T10 and T11.
