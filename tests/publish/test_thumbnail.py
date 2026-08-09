"""publish thumbnail rendering: escaping, style validation, layout, argv."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from nagare_clip.publish.thumbnail import (
    PRESETS,
    LineStyle,
    PlacedLine,
    SetStyle,
    build_measure_cmd,
    escape_magick_text,
    layout_lines,
    parse_metrics,
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


def test_measure_puts_every_line_in_one_call():
    cmd = build_measure_cmd(
        [("A", LineStyle(font="F", pointsize=70)), ("B", LineStyle(font="", pointsize=156))]
    )
    assert cmd[0] == "magick"
    assert cmd.count("(") == 2 and cmd.count(")") == 2
    assert cmd[-2:] == ["-format", "%w %h\n"] or cmd[-1] == "info:"
    assert cmd[-1] == "info:"


def test_measure_omits_the_font_flag_when_no_face_is_configured():
    cmd = build_measure_cmd([("A", LineStyle(font="", pointsize=70))])
    assert "-font" not in cmd


def test_measure_escapes_the_text_and_passes_it_as_one_argument():
    cmd = build_measure_cmd([("100% @x", LineStyle())])
    assert "label:100%% @x" in cmd


def test_parse_metrics_reads_width_and_height_per_line():
    assert parse_metrics("120 42\n980 190\n", expected=2) == [(120, 42), (980, 190)]


@pytest.mark.parametrize("out", ["", "120 42\n", "nonsense\n", "120\n980 190\n"])
def test_parse_metrics_rejects_output_it_cannot_trust(out):
    assert parse_metrics(out, expected=2) is None


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
def test_the_measure_command_actually_runs():
    cmd = build_measure_cmd(
        [("あ", LineStyle(font="", pointsize=40)), ("いい", LineStyle(font="", pointsize=40))]
    )
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    metrics = parse_metrics(out, expected=2)
    assert metrics is not None
    assert metrics[1][0] > metrics[0][0]  # two glyphs are wider than one


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
        [(300, 190)],
        SetStyle(),
        CANVAS,
        12,
    )
    assert placed[0].text == "穴あけ不要。"
    assert (placed[0].style.fill, placed[0].style.strokewidth) == ("#B08D3E", 12)
    assert isinstance(placed[0], PlacedLine)
