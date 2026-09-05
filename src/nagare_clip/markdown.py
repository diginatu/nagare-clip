"""Markdown the reviewable artifacts share.

``publish.md``, ``render.md`` and ``index.md`` all embed images and all honour
the same ``image_markup`` choice; three functions rendering three markups is
three places for a viewer's raw-HTML setting to be half-honoured.

Deliberately dependency-free: the ``render`` stage and ``index_page`` import
this and must not end up importing the LLM transport through the back door.
"""

from __future__ import annotations


def _html_alt(alt: str) -> str:
    """Alt text that cannot break out of its own attribute."""
    return (
        alt.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", " ")
    )


def _markdown_alt(alt: str) -> str:
    """Alt text that cannot close the image early."""
    return alt.replace("[", r"\[").replace("]", r"\]").replace("\n", " ")


def embed_image(path: str, alt: str, width: int, markup: str) -> str:
    """One embedded image, in whichever markup the config asked for.

    ``html`` (default) keeps the sized ``<img>`` -- a shortlist of stills is
    unreviewable at full width.  ``markdown`` is for viewers that strip raw
    HTML; the size hint has no markdown equivalent, so it is simply dropped.

    The *alt* is carried in both, and both callers already have something worth
    saying there.  A viewer never renders it, which is what makes it the right
    place for a description the human can already see in the picture itself: it
    reaches a screen reader and a model reading the file, and costs a reader
    with eyes nothing.  A visible caption line would cost them a redundant line.
    """
    if markup == "markdown":
        return f"![{_markdown_alt(alt)}]({path})"
    return f'<img src="{path}" alt="{_html_alt(alt)}" width="{width}">'
