"""Markdown the reviewable artifacts share.

``publish.md``, ``render.md`` and ``index.md`` all embed images and all honour
the same ``image_markup`` choice; three functions rendering three markups is
three places for a viewer's raw-HTML setting to be half-honoured.

Deliberately dependency-free: the ``render`` stage and ``index_page`` import
this and must not end up importing the LLM transport through the back door.
"""

from __future__ import annotations


def embed_image(path: str, alt: str, width: int, markup: str) -> str:
    """One embedded image, in whichever markup the config asked for.

    ``html`` (default) keeps the sized ``<img>`` -- a shortlist of stills is
    unreviewable at full width.  ``markdown`` is for viewers that strip raw
    HTML; the size hint has no markdown equivalent, so it is simply dropped.
    """
    if markup == "markdown":
        return f"![{alt}]({path})"
    return f'<img src="{path}" width="{width}">'
