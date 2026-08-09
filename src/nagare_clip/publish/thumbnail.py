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

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GRAVITIES = (
    "northwest",
    "north",
    "northeast",
    "west",
    "center",
    "east",
    "southwest",
    "south",
    "southeast",
)
NAMED_COLORS = frozenset(
    {
        "white",
        "black",
        "gray",
        "grey",
        "red",
        "green",
        "blue",
        "yellow",
        "cyan",
        "magenta",
        "orange",
        "none",
        "transparent",
    }
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


def _preset(
    gravity: str, offset: str, tag_fill: str, hook_fill: str, sub_fill: str, outline: str
) -> Preset:
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


def _warn_unknown(
    raw: Mapping[str, Any], known: tuple[str, ...], skip: tuple[str, ...] = ()
) -> None:
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
            sw
            if (sw := _int_in(raw.get("strokewidth"), 0, MAX_STROKEWIDTH)) is not None
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
            offset if isinstance(offset, str) and _OFFSET_RE.match(offset.strip()) else base.offset
        ),
        shadow=shadow,
        shadow_color=_color(shadow_map.get("color")) or base.shadow_color,
        shadow_blur=(
            blur.strip()
            if isinstance(blur := shadow_map.get("blur"), str) and _BLUR_RE.match(blur.strip())
            else base.shadow_blur
        ),
    )


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


def _annotate(line: PlacedLine, *, fill: str, stroke: str, strokewidth: int) -> list[str]:
    args: list[str] = []
    if line.style.font:
        args += ["-font", line.style.font]
    args += [
        "-pointsize",
        str(line.style.pointsize),
        "-fill",
        fill,
        "-stroke",
        stroke,
        "-strokewidth",
        str(strokewidth),
        "-annotate",
        line.offset,
        escape_magick_text(line.text),
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
    cmd = ["magick", str(background), "-resize", f"{size}^", "-gravity", "center", "-extent", size]

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
            line,
            fill=line.style.stroke,
            stroke=line.style.stroke,
            strokewidth=line.style.strokewidth,
        )
        cmd += _annotate(line, fill=line.style.fill, stroke="none", strokewidth=0)
    cmd += ["-quality", "92", str(out_path)]
    return cmd
