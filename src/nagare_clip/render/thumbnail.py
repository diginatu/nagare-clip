"""Render the thumbnail candidates, rather than describing them.

The publish LLM writes the copy AND the style for that copy, in ImageMagick's
own vocabulary (``-fill``, ``-stroke``, ``-strokewidth``, ``-pointsize``,
``-gravity``), so the sets in ``publish.json`` are real options rather than
several wordings of one look.  This module is the *render* stage: it turns
those values into images and never calls a model.

It receives *values*; it builds every command.  ``magick`` reads and writes
files (``@``, ``-write``, MSL), which makes a model-authored command line a
real hole and an allowlisted operator set a cheap fix: the worst a bad
generation can do is produce an ugly image.

Commands are argument lists.  Never a shell string.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from nagare_clip.publish.thumbs import ThumbShot

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

# A thumbnail line states which job it does, so a layout can follow a set of
# one, two or three lines instead of the copy being padded to fill a template.
VALID_ROLES = ("tag", "hook", "subtitle")
MAX_THUMB_LINES = 3


@dataclass(frozen=True)
class ThumbLine:
    role: str  # one of VALID_ROLES
    text: str
    style: dict[str, Any] = field(default_factory=dict)  # raw; validated at render time


@dataclass(frozen=True)
class ThumbSet:
    """One alternative: its copy, the picture it goes on, and its look.

    Defined here rather than beside the LLM call that writes it: this is what
    ``publish.json`` carries, and the render stage must be able to read it
    without loading the transport.

    ``background`` is per **set**, because a headline and the photograph it
    sits on are one decision: four copy treatments over one frame is four
    wordings of one thumbnail.  Empty means "nothing chosen" and falls back to
    the frame shortlist.
    """

    lines: list[ThumbLine] = field(default_factory=list)
    style: dict[str, Any] = field(default_factory=dict)  # gravity / offset / shadow
    background: str = ""  # relative to the publish stage dir, or absolute


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
# neighbours, or `render.md` shows four copies of one thumbnail.
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
            logger.warning("render: thumbnail style key %r is not supported; ignored", key)


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


@dataclass(frozen=True)
class ThumbRender:
    index: int  # 1-based set number, matching render.md's "Set N"
    path: str  # relative to the render stage dir
    background: str  # the still it was composited onto


@dataclass(frozen=True)
class SkippedSet:
    """A set that produced no image, and the reason a human can act on.

    The loop here is "edit publish.json, re-run render, look at render.md", so
    a set that quietly disappears from the contact sheet -- with the reason
    only in the log -- is a typo the human cannot see.
    """

    index: int
    reason: str


@dataclass(frozen=True)
class RenderResult:
    """What became of every copy set: an image, or a reason there is none."""

    renders: list[ThumbRender] = field(default_factory=list)
    skipped: list[SkippedSet] = field(default_factory=list)


def set_relpath(index: int) -> str:
    """Render path relative to the render stage dir."""
    return f"thumbnails/set{index}.jpg"


def resolve_background(thumbs: Sequence[Any], publish_dir: Path) -> Path | None:
    """The fallback still, for a set that names none of its own.

    The first candidate that is actually on disk: the shortlist is already
    ordered by how interesting the director thought the moment was, and a
    project that says nothing renders exactly as it always has.
    """
    for shot in thumbs:
        path = publish_dir / shot.path
        if path.is_file():
            return path
    return None


def named_background(thumb_set: Any) -> str:
    """The path this set names, if any -- lenient about a hand-edited value."""
    return str(getattr(thumb_set, "background", "") or "").strip()


def set_background(thumb_set: Any, thumbs: Sequence[Any], publish_dir: Path) -> Path | None:
    """The still THIS set is composited onto.

    A relative path is resolved against the **publish** stage dir, because
    that is where the stills are and where ``publish.json`` names them.  An
    absolute path is taken as it stands, and neither form has to be a
    shortlist frame: ``build_render_cmd`` covers-and-crops whatever it is
    given, so a photograph the camera never rolled on, or a frame pulled by
    hand at a timestamp the shortlist missed, is one line of JSON away.

    A named background that is not on disk yields ``None`` rather than the
    shortlist fallback: rendering a hook over some other frame and calling it
    the chosen one misleads review worse than a missing image does.
    """
    named = named_background(thumb_set)
    if not named:
        return resolve_background(thumbs, publish_dir)
    path = Path(named)
    path = path if path.is_absolute() else publish_dir / path
    if path.is_file():
        return path
    logger.warning("render: background %s not found; set dropped", named)
    return None


def render_sets(
    sets: Sequence[Any],
    thumbs: Sequence[Any],
    render_cfg: Mapping[str, Any],
    publish_dir: Path,
    out_dir: Path,
    run: Callable[[list[str]], str],
) -> RenderResult:
    """One rendered thumbnail per copy set, so render.md shows options.

    Two calls per set: measure, then render.  *run* returns stdout, and is
    injected so this module never starts a subprocess itself.

    Each set is composited onto **its own** background (``set_background``),
    resolved against *publish_dir* because that is where the stills are and
    where ``publish.json`` names them; the images land under *out_dir*, which
    is the render stage's own directory.

    Nothing here is allowed to fail the stage: a set whose background is
    missing or whose magick call dies comes back as a ``SkippedSet`` carrying
    the reason, and the rest still render.
    """
    import subprocess

    result = RenderResult(renders=[], skipped=[])
    if not render_cfg.get("enabled", True) or not sets:
        return result

    fonts = render_cfg.get("fonts") or {}
    canvas = (int(render_cfg.get("width", 1280)), int(render_cfg.get("height", 720)))
    line_gap = int(render_cfg.get("line_gap", 12))
    (out_dir / "thumbnails").mkdir(parents=True, exist_ok=True)

    def skip(index: int, reason: str) -> None:
        logger.warning("render: set %d not rendered: %s", index, reason)
        result.skipped.append(SkippedSet(index=index, reason=reason))

    for index, thumb_set in enumerate(sets, start=1):
        background = set_background(thumb_set, thumbs, publish_dir)
        if background is None:
            named = named_background(thumb_set)
            skip(
                index,
                f"background not found: {named}"
                if named
                else "no background: the set names none and no frame shortlist "
                "candidate is on disk",
            )
            continue
        preset = preset_for(index)
        styled = [
            (line.text, resolve_line_style(line.style, line.role, fonts, preset))
            for line in thumb_set.lines
        ]
        set_style = resolve_set_style(thumb_set.style, preset)
        try:
            metrics = parse_metrics(run(build_measure_cmd(styled)), expected=len(styled))
        except subprocess.CalledProcessError as e:
            # magick's stderr is the actual reason (e.g. "unable to read font 'X'"
            # from a bad `render.fonts` slot) -- without it the log only
            # has the argv and "exit status 1", and the human has to re-run by hand
            # to learn why every set silently dropped.
            logger.debug("render: measure failed for set %d", index, exc_info=True)
            skip(index, f"could not measure the text: {e.stderr}")
            continue
        except Exception as e:  # noqa: BLE001 - a dead magick must not fail the stage
            logger.debug("render: measure failed for set %d", index, exc_info=True)
            skip(index, f"could not measure the text: {e}")
            continue
        if metrics is None:
            # Unmeasured fallback: point size is a fair proxy for line height.
            logger.warning("render: unusable text metrics for set %d; estimating", index)
            metrics = [(1, int(style.pointsize * 1.2)) for _, style in styled]
        placed = layout_lines(styled, metrics, set_style, canvas, line_gap)
        rel = set_relpath(index)
        try:
            run(build_render_cmd(background, placed, set_style, canvas, out_dir / rel))
        except subprocess.CalledProcessError as e:
            logger.debug("render: magick failed for set %d", index, exc_info=True)
            skip(index, f"magick failed: {e.stderr}")
            continue
        except Exception as e:  # noqa: BLE001
            logger.debug("render: magick failed for set %d", index, exc_info=True)
            skip(index, f"magick failed: {e}")
            continue
        try:
            bg_name = str(background.relative_to(publish_dir))
        except ValueError:
            bg_name = str(background)
        result.renders.append(ThumbRender(index=index, path=rel, background=bg_name))
    logger.info(
        "render: rendered %d thumbnail(s), %d skipped",
        len(result.renders),
        len(result.skipped),
    )
    return result


def sets_from_dict(data: Any) -> list[ThumbSet]:
    """``thumbnail_copy`` read back out of publish.json.

    Lenient, like every hand-editable intermediate in this pipeline: a
    malformed set is dropped, never raised.
    """
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
                ThumbLine(
                    role=role, text=text, style={k: entry[k] for k in LINE_KEYS if k in entry}
                )
            )
        if lines:
            background = raw.get("background")
            out.append(
                ThumbSet(
                    lines=lines,
                    style={k: raw[k] for k in SET_KEYS if k in raw},
                    background=background.strip() if isinstance(background, str) else "",
                )
            )
    return out


def shots_from_dict(data: Any) -> list[ThumbShot]:
    """``thumbnails`` read back out of publish.json, as real ``ThumbShot``s.

    Tolerant like ``sets_from_dict``: a missing/malformed field falls back to
    a sensible default, and an entry with no usable ``path`` is dropped. Path
    existence is not checked here -- ``resolve_background``/``render_sets``
    do that against the publish dir -- but a real dataclass beats an anonymous
    stand-in for anything that later touches ``.stem``/``.time``/``.kind``/
    ``.label``.
    """
    raw_thumbs = data.get("thumbnails") if isinstance(data, Mapping) else None
    out = []
    for raw in raw_thumbs if isinstance(raw_thumbs, list) else []:
        if not isinstance(raw, Mapping):
            continue
        path = str(raw.get("path", "")).strip()
        if not path:
            continue
        try:
            time = float(raw.get("source_time", 0.0))
        except (TypeError, ValueError):
            time = 0.0
        out.append(
            ThumbShot(
                stem=str(raw.get("stem", "")),
                time=time,
                kind=str(raw.get("kind", "")),
                label=str(raw.get("label", "")),
                path=path,
            )
        )
    return out
