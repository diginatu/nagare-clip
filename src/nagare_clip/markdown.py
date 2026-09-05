"""Markdown the reviewable artifacts share.

``publish.md`` and ``render.md`` both embed images and both honour the same
``image_markup`` choice; two functions rendering two markups is two places for
a viewer's raw-HTML setting to be half-honoured.

Deliberately dependency-free: the ``render`` stage imports this and must not
end up importing the LLM transport through the back door.
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
