"""The one image embed ``publish.md``, ``render.md`` and ``index.md`` share.

The alt text is the only description of a picture that reaches a screen reader
or a model reading the file; a viewer never shows it, which is exactly why it
is where a headline belongs and a visible caption line is not.
"""

from __future__ import annotations

from nagare_clip.markdown import embed_image


class TestHtml:
    def test_the_alt_is_carried(self):
        assert embed_image("a.jpg", "Set 1 — まさかの水漏れ", 480, "html") == (
            '<img src="a.jpg" alt="Set 1 — まさかの水漏れ" width="480">'
        )

    def test_a_quote_in_the_alt_cannot_break_out_of_the_attribute(self):
        out = embed_image("a.jpg", 'he said "no" & <b>', 480, "html")
        assert 'alt="he said &quot;no&quot; &amp; &lt;b&gt;"' in out

    def test_an_empty_alt_is_still_an_attribute(self):
        assert embed_image("a.jpg", "", 480, "html") == '<img src="a.jpg" alt="" width="480">'


class TestMarkdown:
    def test_the_alt_is_carried(self):
        assert embed_image("a.jpg", "Set 1 — 水漏れ", 480, "markdown") == (
            "![Set 1 — 水漏れ](a.jpg)"
        )

    def test_a_bracket_in_the_alt_cannot_close_the_image(self):
        assert embed_image("a.jpg", "a [b] c", 480, "markdown") == r"![a \[b\] c](a.jpg)"

    def test_a_newline_in_the_alt_is_flattened(self):
        assert embed_image("a.jpg", "one\ntwo", 480, "markdown") == "![one two](a.jpg)"
