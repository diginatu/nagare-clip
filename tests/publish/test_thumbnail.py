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
