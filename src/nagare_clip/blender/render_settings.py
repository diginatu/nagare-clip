"""``blender.render`` config -> Blender's render RNA, forwarded 1:1.

Deliberately free of ``bpy``: every operation here is a getattr/setattr on
whatever object it is handed, so the encoder/resolution/audio contract is
testable without Blender (the same reason ``frames.py`` holds the placement
arithmetic).
"""

from __future__ import annotations

import logging

__all__ = ["apply_render_settings"]


def apply_render_settings(render: object, settings: dict, _path: str = "") -> None:
    """Forward arbitrary ``blender.render`` keys onto ``scene.render``.

    Each key is set 1:1 on the RNA object of the same name, so any render
    setting Blender exposes -- encoder, container, resolution, audio codec,
    output filepath -- is configurable with no code change here. A **dict**
    value recurses into the sub-struct of that name (``image_settings``,
    ``ffmpeg``), which is why those two need no special case.

    An unknown **key** raises ``AttributeError`` (Blender's RNA rejects unknown
    properties on assignment); it is logged and skipped, as ``apply_text_style``
    does for caption styling. An invalid **value** -- a misspelt enum member --
    is left to propagate: a typo in a setting nobody relies on is cheap, while
    rendering with a codec other than the one the config asked for is a defect
    discovered only by playing the file.

    ``fps`` given without ``fps_base`` resets ``fps_base`` to ``1.0``. The scene
    carries the first source's pulldown there (``1.001`` for 29.97), and leaving
    it in place would make ``fps: 30`` produce an effective 29.97 -- an override
    that does not override.
    """
    for key, value in settings.items():
        where = f"{_path}{key}"
        if isinstance(value, dict):
            try:
                sub = getattr(render, key)
            except AttributeError:
                logging.warning("Unknown blender.render key ignored: %s", where)
                continue
            apply_render_settings(sub, value, _path=f"{where}.")
            continue
        try:
            setattr(render, key, value)
        except AttributeError:
            logging.warning("Unknown blender.render key ignored: %s", where)

    # Top level only: no render sub-struct has an fps, so a stray nested one
    # must not reach for an fps_base that is not there.
    if not _path and "fps" in settings and "fps_base" not in settings:
        render.fps_base = 1.0
