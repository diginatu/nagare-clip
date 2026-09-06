"""Hex-colour parsing for caption_style colour keys (bpy-free).

Blender's TextStrip colour properties (``color``, ``outline_color``,
``shadow_color``, ``box_color``) are ``PROP_COLOR_GAMMA``: the RNA value IS the
sRGB/display value, so ``apply_text_style`` can forward a config list verbatim
and the strip shows exactly the numbers the colour picker's RGB sliders show.

The trap is the clipboard.  Blender's ``but_copy_color`` linearises a
COLOR_GAMMA button on **copy** (``srgb_to_linearrgb_v3_v3``) and ``but_paste_color``
un-linearises on paste, so Ctrl-C/Ctrl-V round-trips inside Blender but the
text on the clipboard is NOT the RNA value — pasting it into config renders a
visibly darker colour (mid grey 0.5 copies as 0.214).  Alpha is untouched
(the conversion is ``_v3_v3``), which is the tell-tale.

The colour picker's **Hex** field has no such conversion for a gamma button
(``interface_region_color_picker.cc`` skips it when ``is_color_gamma_picker``),
so hex is exactly ``round(channel * 255)`` and parsing is a plain divide by
255.  That makes ``color: "#FFCC00"`` a copy-paste-safe way to say a colour.
"""

from __future__ import annotations

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# 3/4-digit shorthand expands each digit ("fc0" -> "ffcc00"), matching what
# Blender's own hex_to_rgba() accepts; 6/8 are what the Hex field emits (8 when
# the property carries alpha, which every TextStrip colour does).
_SHORTHAND_LENGTHS = frozenset({3, 4})
_FULL_LENGTHS = frozenset({6, 8})


def is_color_key(key: str) -> bool:
    """True for the caption_style keys whose value is an RGBA colour.

    A suffix rule rather than a fixed list: every Blender TextStrip colour
    attribute is named ``color`` or ``*_color``, so a future one needs no code
    change — the same reason ``apply_text_style`` forwards keys generically.
    """
    return key == "color" or key.endswith("_color")


def parse_hex_color(value: object) -> tuple[float, float, float, float]:
    """Parse ``"#RRGGBB"``/``"#RRGGBBAA"`` (or 3/4-digit shorthand) to RGBA floats.

    A leading ``#`` and surrounding whitespace are optional, case is ignored.
    Alpha defaults to 1.0 when the string carries only RGB.

    Raises ``ValueError`` on anything else.  A wrong colour is invisible in a
    finished render — it just looks like a design decision — so a malformed
    value is a hard error, like a missing caption_style ``font`` path, rather
    than a logged-and-skipped key.
    """
    if not isinstance(value, str):
        raise ValueError(f"hex colour must be a string, got: {value!r}")
    text = value.strip().lstrip("#")
    if not text or any(ch not in _HEX_DIGITS for ch in text):
        raise ValueError(f"not a hex colour: {value!r}")
    if len(text) in _SHORTHAND_LENGTHS:
        text = "".join(ch * 2 for ch in text)
    elif len(text) not in _FULL_LENGTHS:
        raise ValueError(f"hex colour must have 3, 4, 6 or 8 digits, got: {value!r}")
    channels = [int(text[i : i + 2], 16) / 255.0 for i in range(0, len(text), 2)]
    if len(channels) == 3:
        channels.append(1.0)
    return (channels[0], channels[1], channels[2], channels[3])
