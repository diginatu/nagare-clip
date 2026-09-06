"""Tests for the hex-colour parsing shared by every caption_style colour key.

Blender's TextStrip colour properties are ``PROP_COLOR_GAMMA``: the RNA value
IS the sRGB/display value, so the colour picker's Hex field is exactly
``round(value * 255)`` per channel and a hex string round-trips by dividing by
255.  Ctrl-C over the swatch does NOT (Blender linearises a COLOR_GAMMA button
on copy), which is why the Hex field is the supported route.
"""

from __future__ import annotations

import pytest

from nagare_clip.blender.color import is_color_key, parse_hex_color


def _close(got, want, tol=1e-6):
    assert len(got) == len(want)
    for g, w in zip(got, want):
        assert abs(g - w) < tol, (got, want)


# --- is_color_key ----------------------------------------------------------


@pytest.mark.parametrize("key", ["color", "outline_color", "shadow_color", "box_color"])
def test_is_color_key_true_for_colour_attrs(key):
    assert is_color_key(key) is True


@pytest.mark.parametrize(
    "key", ["font", "font_size", "use_outline", "colorful", "color_x", "box_margin", ""]
)
def test_is_color_key_false_for_everything_else(key):
    assert is_color_key(key) is False


# --- parse_hex_color -------------------------------------------------------


def test_parse_six_digit_hex_is_value_over_255():
    # 0xCC == 204; 204 / 255 == 0.8 exactly, i.e. what the picker's slider shows.
    _close(parse_hex_color("#FFCC00"), (1.0, 0.8, 0.0, 1.0))


def test_parse_eight_digit_hex_carries_alpha():
    # TextStrip.color has an alpha channel, so Blender's Hex field shows 8 digits.
    _close(parse_hex_color("#FFCC0080"), (1.0, 0.8, 0.0, 128 / 255))


def test_parse_accepts_lowercase_and_a_missing_hash():
    _close(parse_hex_color("ffcc00"), (1.0, 0.8, 0.0, 1.0))


def test_parse_accepts_three_and_four_digit_shorthand():
    _close(parse_hex_color("#fc0"), (1.0, 0.8, 0.0, 1.0))
    _close(parse_hex_color("#fc08"), (1.0, 0.8, 0.0, 0x88 / 255))


def test_parse_surrounding_whitespace_is_ignored():
    _close(parse_hex_color("  #FFCC00 "), (1.0, 0.8, 0.0, 1.0))


@pytest.mark.parametrize(
    "bad",
    ["#GGGGGG", "#FF", "#FFFFF", "#FFFFFFF", "nope", "", "#", "#FFCC00FF00"],
)
def test_parse_rejects_a_malformed_hex_string(bad):
    # A wrong colour is invisible in a finished render, so this is a hard error
    # like caption_style 'font', not a warn-and-skip.
    with pytest.raises(ValueError):
        parse_hex_color(bad)


def test_parse_rejects_a_non_string():
    with pytest.raises(ValueError):
        parse_hex_color([1, 1, 1, 1])
