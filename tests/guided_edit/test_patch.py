"""The deterministic edit op: one exact {{old->new}} patch on one line."""

import pytest

from nagare_clip.guided_edit.patch import apply_edit, edit_problem, locate_edit


def test_plain_line_is_patched_in_place():
    assert apply_edit("えーと今日は配管です", "えーと", "") == "{{えーと->}}今日は配管です"
    assert apply_edit("配管を付けます", "付け", "取り付け") == "配管を{{付け->取り付け}}ます"


def test_text_is_found_as_the_director_reads_it_around_markers():
    line = '<keep>今日は<speed factor="8.0">配管</speed>です</keep>'
    assert apply_edit(line, "です", "でした").endswith("{{です->でした}}</keep>")
    # touching a marker at the edge is fine
    assert apply_edit(line, "今日は", "本日は") == (
        '<keep>{{今日は->本日は}}<speed factor="8.0">配管</speed>です</keep>'
    )


def test_a_span_through_a_marker_or_patch_is_refused():
    line = '今日は<speed factor="8.0">配管</speed>です'
    assert "cuts through" in locate_edit(line, "は配")
    patched = "今日は{{はいかん->配管}}です"
    assert "cuts through" in locate_edit(patched, "配管")  # text the patch contributes
    assert "cuts through" in locate_edit(patched, "は配")


def test_old_must_occur_exactly_once():
    assert "occurs 2 times" in edit_problem("ははは", "はは", "")
    assert "does not occur" in edit_problem("今日は", "明日", "")
    assert "empty" in edit_problem("今日は", "", "x")


@pytest.mark.parametrize("bad", ["a->b", "{{", "}}", "<keep>", "a\nb"])
def test_patch_syntax_in_old_or_new_is_refused(bad):
    assert "patch text" in edit_problem("a->b {{ }} <keep> a\nb", bad, "")
    assert "patch text" in edit_problem("今日は", "今日", bad)


def test_apply_raises_on_a_problem():
    with pytest.raises(ValueError):
        apply_edit("今日は", "明日", "")
