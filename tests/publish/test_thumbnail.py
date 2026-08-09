"""publish thumbnail rendering: escaping, style validation, layout, argv."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from nagare_clip.publish.thumbnail import (
    PRESETS,
    escape_magick_text,
    preset_for,
    resolve_line_style,
    resolve_set_style,
)

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
            [
                "magick",
                "-background",
                "none",
                "-pointsize",
                "40",
                f"label:{arg}",
                "-format",
                "%h",
                "info:",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return int(out)

    # A single line of 40pt text; an unescaped \n or @file would make it taller.
    assert height(escape_magick_text(text)) == height("x")


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


@pytest.mark.parametrize(
    "value",
    [
        "#fff",
        "#B08D3E",
        "#FAFAFAFF",
        "rgba(30,30,30,1)",
        "rgb(10, 20, 30)",
        "white",
    ],
)
def test_accepted_colour_syntaxes(value):
    assert _line(fill=value).fill == value


@pytest.mark.parametrize(
    "value",
    ["red; -write /tmp/x", "url(http://x)", "", 5, None, "#12345", "rgba(1,2,3,4) -write out.png"],
)
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
    style = resolve_set_style({"shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"}}, PRESETS[0])
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
